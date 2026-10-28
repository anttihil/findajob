"""Scrape, normalize, and store job postings.

findajob search run [--dry-run]
"""

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from findajob.core.config import load_config
from findajob.core.database import Database
from findajob.core.logger import get_logger
from findajob.core.search_schedule import search_schedules
from findajob.core.status_manager import (
    add_sync_error,
    clear_stale_lock,
    is_sync_running,
    load_sync_status,
    set_sync_progress,
)
from findajob.search.enrichment import enrich_linkedin
from findajob.search.guard import (
    SourceCircuit,
)
from findajob.search.normalizer import normalize_rows
from findajob.search.policy import source_policy
from findajob.search.proxies import load_proxies
from findajob.search.scheduler import (
    ScrapeTaskPayload,
    overdue_cells,
    select_cells,
)
from findajob.search.sources.linkedin_details import LinkedInDetailsAdapter
from findajob.search.targets import load_targets
from findajob.search.traffic import SourceTraffic

if TYPE_CHECKING:
    from findajob.search.scheduler import ScrapeTask
    from findajob.search.sources.jobspy_source import JobSpySource
    from findajob.search.targets import SearchTargets

logger = get_logger()

# Share of a run's planned cells that may come back empty or errored before the run stops
# calling itself ok. Healthy runs lose 0-10% of Indeed cells to genuinely empty 24h windows;
# the runs that lost 30-50% still reported ok, which is the case this threshold catches.
LOST_CELLS_PARTIAL = 0.2


def plan_hash(targets: "SearchTargets | None", config: dict[str, Any]) -> str:
    """Identify the scrape plan, so trend queries can refuse to cross plan changes."""
    import hashlib
    import json

    scraper = config.get("scraper", {})
    schedules = search_schedules(config)
    payload = "|".join(
        [
            targets.fingerprint if targets else "default",
            ",".join(sorted(schedules)),
            json.dumps(
                {
                    source: {
                        "interval_minutes": schedule.interval_minutes,
                        "schedule": schedule.times,
                        "jitter_minutes": schedule.jitter_minutes,
                    }
                    for source, schedule in schedules.items()
                },
                sort_keys=True,
            ),
            "page-budget-v1",
            json.dumps(
                {source: source_policy(scraper, source) for source in sorted(schedules)},
                sort_keys=True,
            ),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def run_sync(
    dry_run: bool = False,
    limit: int | None = None,
    sources: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    logger.info("=" * 60)
    mode = "dry_run" if dry_run else "incremental"
    logger.info(f"Starting sync ({mode})")
    logger.info("=" * 60)

    config = load_config()
    targets = load_targets()

    proxies = load_proxies(config)
    scraper_config = dict(config.get("scraper", {}), proxies_list=proxies)

    if targets:
        problems = targets.validate()
        if problems:
            logger.error("search targets are invalid; aborting:")
            for problem in problems:
                logger.error(f"  - {problem}")
            add_sync_error("Config", f"search targets: {problems[0]}")
            return None

    if not dry_run:
        if is_sync_running() and not force:
            logger.error(
                "another sync holds the lock (pid "
                f"{load_sync_status().get('owner_pid')}, started "
                f"{load_sync_status().get('started_at')}); refusing to start. "
                "Use --force to override."
            )
            return None
        if clear_stale_lock():
            logger.warning("released a stale lock from a previous run that died")
        set_sync_progress(True)

    db = Database()
    run_id = None
    totals = {
        "cells_planned": 0,
        "cells_succeeded": 0,
        "cells_skipped": 0,
        "cells_empty": 0,
        "cells_error": 0,
        "postings_fetched": 0,
        "postings_new": 0,
        "duplicates_merged": 0,
    }
    circuits: dict[str, SourceCircuit] = {}
    any_budget_limited = False

    try:
        if not dry_run:
            run_id = db.start_sync_run(
                mode,
                plan_hash=plan_hash(targets, config),
            )

        enabled = sources or [
            name for name, on in (scraper_config.get("sources") or {}).items() if on
        ]

        from findajob.search.sources.jobspy_source import JobSpySource

        source_client = JobSpySource()

        for source in enabled:
            circuit = SourceCircuit(
                source,
                scraper_config,
                db=None if dry_run else db,
            )
            circuits[source] = circuit

            if circuit.persisted_backoff_active():
                until = circuit.backoff_until()
                logger.warning(f"[{source}] in backoff until {until}; skipping")
                add_sync_error(
                    source,
                    f"Source in backoff until {until} — skipped this run.",
                    severity="info",
                )
                continue

            cells = db.get_cells(source=source)
            if not cells:
                logger.warning(f"[{source}] no cells seeded. Run: findajob search seed-cells")
                continue

            tasks = select_cells(cells, scraper_config, source)
            if limit:
                tasks = tasks[:limit]
            totals["cells_planned"] += len(tasks)
            if run_id is not None:
                db.conn.execute(
                    "UPDATE sync_runs SET cells_planned = ? WHERE id = ?",
                    (totals["cells_planned"], run_id),
                )
                db.conn.commit()

            logger.info(f"[{source}] {len(tasks)} cells planned")

            traffic = SourceTraffic(source, scraper_config, proxies, circuit)

            empty = 0
            errored = 0
            try:
                for index, task in enumerate(tasks):
                    if not traffic.can_start_page:
                        totals["cells_skipped"] += len(tasks) - index
                        break
                    outcome = _scrape_one(
                        db,
                        source_client,
                        circuit,
                        task,
                        run_id,
                        config,
                        dry_run,
                        traffic=traffic,
                    )
                    for key in ("postings_fetched", "postings_new", "duplicates_merged"):
                        totals[key] += _LAST_CELL_STATS.get(key, 0)
                    any_budget_limited = any_budget_limited or outcome == "budget_limited"
                    if outcome in ("ok", "budget_limited"):
                        totals["cells_succeeded"] += 1
                    elif outcome == "empty":
                        empty += 1
                    else:
                        errored += 1
                    if outcome == "tripped":
                        totals["cells_skipped"] += len(tasks) - index - 1
                        break
            finally:
                traffic.close()
            logger.info("[%s] %d/%d requests spent", source, traffic.spent, traffic.limit)

            totals["cells_empty"] += empty
            totals["cells_error"] += errored
            if empty or errored:
                logger.warning(
                    f"[{source}] {empty + errored} of {len(tasks)} planned cells returned no "
                    f"postings ({empty} empty, {errored} error)."
                )

            circuit.note_clean_run()

        _report_coverage(db, scraper_config)

        lost = totals["cells_empty"] + totals["cells_error"]
        status = "ok"
        if (
            any(c.is_open for c in circuits.values())
            or totals["cells_skipped"]
            or any_budget_limited
        ):
            status = "partial"
        if totals["cells_planned"] and lost >= LOST_CELLS_PARTIAL * totals["cells_planned"]:
            status = "partial"
        if totals["cells_planned"] and not totals["cells_succeeded"]:
            status = "failed"

        logger.info("=" * 60)
        logger.info(f"Sync finished ({status})")
        for key, value in totals.items():
            logger.info(f"  {key:20} {value}")
        logger.info("=" * 60)

        if run_id is not None:
            db.finish_sync_run(
                run_id,
                status,
                error_summary={s: c.summary() for s, c in circuits.items()},
                **totals,
            )
        return totals

    except Exception as exc:
        logger.exception(f"Critical sync failure: {exc}")
        add_sync_error("Engine", f"Critical failure: {exc}")
        if run_id is not None:
            db.finish_sync_run(run_id, "failed", error_summary=str(exc))
        raise
    finally:
        db.close()
        if not dry_run:
            set_sync_progress(
                False,
                totals["postings_fetched"],
                totals["postings_new"],
            )


# Per-cell counters, kept module-level so _scrape_one can report without a return tuple.
_LAST_CELL_STATS: dict[str, int] = {}


def store_postings(
    db: Database, postings: list[dict[str, Any]], run_id: int | None
) -> tuple[list[int], int, int]:
    """Apply common search defaults, persist postings, and merge duplicates."""
    job_ids: list[int] = []
    new_count = duplicates = 0
    for posting in postings:
        posting.update(
            skills={},
            blockers=[],
            seniority=None,
            match_score=None,
            matched_skills=[],
            matched_count=0,
            required_count=0,
            pipeline_state="new",
        )
        job_id, is_new = db.upsert_posting(posting, run_id=run_id)
        assert job_id is not None, "upsert_posting always inserts or finds a row"
        job_ids.append(job_id)
        if is_new:
            new_count += 1
            canonical = db.find_duplicate(posting.get("content_hash"), exclude_id=job_id)
            if canonical:
                db.mark_duplicate(job_id, canonical)
                duplicates += 1
    return job_ids, new_count, duplicates


def run_cell(
    db: Database,
    client: "JobSpySource",
    circuit: SourceCircuit,
    payload: ScrapeTaskPayload,
    traffic: SourceTraffic,
    run_id: int | None,
    config: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Persist each page; finish that page's details before checking the allowance.

    A budget stop is a censored observation, never proof that a feed is exhausted.
    No cursor or detail backlog is persisted: the next visit starts at the front.
    """
    observed_at = datetime.now(timezone.utc)
    started, initial_requests = time.monotonic(), traffic.spent
    returned = new_count = duplicates = 0
    job_ids: list[int] = []
    detail_stats: dict[str, int] = {}
    stop_reason = "budget_limit"
    error = None
    full = 0
    pages = client.iter_pages(payload, traffic)
    try:
        while traffic.can_start_page:
            try:
                page = next(pages)
            except StopIteration:
                stop_reason = "exhausted"
                break
            rows = page.rows[: payload["results_wanted"] - returned]
            postings, stats = normalize_rows(rows, payload, observed_at, config)
            returned += stats["returned"]
            if payload["source"] == "linkedin":
                adapter = LinkedInDetailsAdapter(
                    [page.proxy] if page.proxy else [], session=traffic.bound_session(page.proxy)
                )
                try:
                    counts, _ = enrich_linkedin(
                        db,
                        postings,
                        adapter,
                        circuit,
                        config,
                        fetch=True,
                        now=observed_at,
                    )
                    for key, value in counts.items():
                        detail_stats[key] = detail_stats.get(key, 0) + value
                finally:
                    adapter.close()
                if (payload.get("indeed_country") or "usa").lower() in {"usa", "us"}:
                    from findajob.search.sources.jobspy_source import extract_description_salary

                    for posting in postings:
                        extract_description_salary(posting)
            full += sum(p["description_quality"] == "full" for p in postings)
            if not dry_run:
                ids, added, merged = store_postings(db, postings, run_id)
                job_ids.extend(ids)
                new_count += added
                duplicates += merged
            if circuit.is_open:
                stop_reason, error = "error", circuit.trip_reason
                break
            if returned >= payload["results_wanted"]:
                stop_reason = "result_limit"
                break
            if page.end_reason:
                stop_reason = page.end_reason
                break
    except Exception as exc:  # noqa: BLE001 - keep earlier persisted pages on failure
        stop_reason, error = "error", str(exc)
        circuit.on_error(exc, cell=payload.get("cell_id"))
    finally:
        pages.close()
    limited = stop_reason == "budget_limit"
    status = (
        "error"
        if error
        else "budget_limited"
        if limited
        else "empty"
        if not returned and stop_reason == "exhausted"
        else "ok"
    )
    incomplete = (
        limited or bool(error) or bool(detail_stats.get("missing") or detail_stats.get("stale"))
    )
    selection = (
        ("available" if incomplete else "complete") if payload["source"] == "linkedin" else "census"
    )
    if not dry_run and job_ids and incomplete:
        for start in range(0, len(job_ids), 400):
            chunk = job_ids[start : start + 400]
            marks = ",".join("?" for _ in chunk)
            db.conn.execute(
                f"UPDATE jobs SET desc_selection = 'available' WHERE id IN ({marks})", chunk
            )
        db.conn.commit()
    observation_payload = dict(payload, desc_selection=selection)
    saturated = int(stop_reason in ("budget_limit", "result_limit", "pagination_limit"))
    cost = {
        "requests_made": traffic.spent - initial_requests,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    if not dry_run:
        db.record_observation(
            run_id,
            observation_payload,
            observed_at.isoformat(),
            returned=returned,
            new_unique=new_count,
            saturated=saturated,
            descriptions_full=full,
            status=status,
            error=error,
            detail_stats=detail_stats or None,
            stop_reason=stop_reason,
            requests_made=cost["requests_made"],
            duration_ms=cost["duration_ms"],
        )
        if payload.get("cell_id") is not None:
            db.record_cell_attempt(
                payload["cell_id"],
                observed_at.isoformat(),
                payload["hours_old"],
                payload["results_wanted"],
                returned=returned,
                new_unique=new_count,
                saturated=saturated,
                status=status,
                error=error,
            )
    if not error and not detail_stats.get("failed"):
        circuit.on_success(returned) if returned else circuit.on_empty()
    logger.info(
        "[%s] %r: %d/%d listings, %d requests, stop=%s",
        payload["source"],
        payload["query"],
        returned,
        payload["results_wanted"],
        cost["requests_made"],
        stop_reason,
    )
    return {
        "status": status,
        "stop_reason": stop_reason,
        "job_ids": job_ids,
        "postings_fetched": returned,
        "postings_new": new_count,
        "duplicates_merged": duplicates,
        "descriptions_full": full,
        "detail_stats": detail_stats,
        "saturated": bool(saturated),
        **cost,
    }


def _scrape_one(
    db: Database,
    client: "JobSpySource",
    circuit: SourceCircuit,
    task: "ScrapeTask",
    run_id: int | None,
    config: dict[str, Any],
    dry_run: bool,
    *,
    traffic: SourceTraffic,
) -> str:
    global _LAST_CELL_STATS
    result = run_cell(
        db, client, circuit, task.to_dict(), traffic, run_id, config.get("scraper", {}), dry_run
    )
    _LAST_CELL_STATS = {
        key: result[key] for key in ("postings_fetched", "postings_new", "duplicates_merged")
    }
    return "tripped" if circuit.is_open else result["status"]


def _report_coverage(db: Database, scraper_config: dict[str, Any]) -> None:
    """Surface scheduler coverage failures as warnings."""
    overdue = overdue_cells(db.get_cells(), scraper_config)
    if not overdue:
        return
    sample = ", ".join(f"{c.location_id}/{c.query}" for c in overdue[:4])
    add_sync_error(
        "Scheduler",
        f"Coverage: {len(overdue)} cells have had no successful visit in "
        f"{scraper_config.get('max_staleness_hours', 72)}h ({sample}"
        f"{'...' if len(overdue) > 4 else ''}). Supply figures for these cells rest on "
        f"older data than the rest.",
        severity="warning",
    )
