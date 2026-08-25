"""Scrape, normalize, score, and store job postings.

Replaces the previous Gmail-IMAP pipeline, which read UNSEEN inbox mail (mutating the
mailbox, making each run non-idempotent) and produced zero rows.

    careerradar search run [--dry-run]
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.logger import get_logger
from careerradar.core.paths import ARCHIVE_DIR
from careerradar.core.status_manager import (
    add_sync_error,
    clear_stale_lock,
    is_sync_running,
    load_sync_status,
    set_sync_progress,
)
from careerradar.profile.adapter import NoActiveProfile, load_profile
from careerradar.search import repository as search_repo
from careerradar.search.guard import (
    ERROR_TRANSIENT,
    SourceCircuit,
    SourceTripped,
)
from careerradar.search.keyword_score import JobScorer
from careerradar.search.normalizer import normalize_rows
from careerradar.search.proxies import apply_proxy_budgets, is_rotating, load_proxies, pin_for
from careerradar.search.scheduler import (
    is_saturated,
    overdue_cells,
    select_cells,
    update_ewma,
    with_location_weights,
)
from careerradar.taxonomy.roles import load_roles
from careerradar.taxonomy.skills import load_taxonomy

if TYPE_CHECKING:
    from careerradar.search.scheduler import ScrapeTask
    from careerradar.search.sources.base import BaseJobSource
    from careerradar.search.sources.jobspy_source import JobSpySource
    from careerradar.taxonomy.roles import RoleTaxonomy
    from careerradar.taxonomy.skills import Taxonomy

logger = get_logger()

# Share of a run's planned cells that may come back empty or errored before the run stops
# calling itself ok. Healthy runs lose 0-10% of Indeed cells to genuinely empty 24h windows;
# the runs that lost 30-50% still reported ok, which is the case this threshold catches.
LOST_CELLS_PARTIAL = 0.2


def plan_hash(roles: "RoleTaxonomy", config: dict[str, Any]) -> str:
    """Identify the scrape plan, so trend queries can refuse to cross plan changes.

    Widening the role catalog or the location set changes what the corpus samples; a trend
    computed across such a change measures the plan, not the market.
    """
    import hashlib

    scraper = config.get("scraper", {})
    payload = "|".join(
        [
            roles.hash,
            ",".join(sorted(k for k, v in (scraper.get("sources") or {}).items() if v)),
            str(sorted((scraper.get("cadence_hours") or {}).items())),
            str(sorted((scraper.get("budgets") or {}).keys())),
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
    taxonomy = load_taxonomy()
    roles = load_roles()

    # Proxies raise the LinkedIn budget substantially, so they are resolved before the
    # scheduler sees the config.
    proxies = load_proxies(config)
    scraper_config = apply_proxy_budgets(config.get("scraper", {}), proxies)
    scraper_config["proxies_list"] = proxies
    rotating = bool(proxies) and is_rotating(config)

    scraper_config = with_location_weights(scraper_config, roles)

    for label, problems in (("skills.yaml", taxonomy.validate()), ("roles.yaml", roles.validate())):
        if problems:
            logger.error(f"{label} is invalid; aborting:")
            for problem in problems:
                logger.error(f"  - {problem}")
            add_sync_error("Config", f"{label}: {problems[0]}")
            return None

    try:
        profile = load_profile(taxonomy=taxonomy)
    except NoActiveProfile as exc:
        logger.error(str(exc))
        add_sync_error("Profile", str(exc))
        return None
    assert profile is not None, "load_profile(required=True) never returns None"
    logger.info(f"Profile: v{profile.version}, {len(profile)} skills (taxonomy {taxonomy.hash})")

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
        "off_topic": 0,
    }
    circuits: dict[str, SourceCircuit] = {}

    try:
        scorer = JobScorer(
            profile,
            roles,
            taxonomy,
            weights=(config.get("matching") or {}).get("weights"),
        )

        if not dry_run:
            run_id = db.start_sync_run(
                mode,
                taxonomy_hash=taxonomy.hash,
                plan_hash=plan_hash(roles, config),
            )

        enabled = sources or [
            name for name, on in (scraper_config.get("sources") or {}).items() if on
        ]

        from careerradar.search.sources.jobspy_source import JobSpySource, prune_archives

        source_client = JobSpySource(archive_dir=None if dry_run else ARCHIVE_DIR)
        if not dry_run:
            # Before scraping, so a run that dies partway still leaves the archive bounded.
            prune_archives(ARCHIVE_DIR, scraper_config.get("archive_retention_days", 14))
        _warn_if_taxonomy_moved(db, taxonomy)

        for source in enabled:
            circuit = SourceCircuit(
                source,
                scraper_config,
                db=None if dry_run else db,
                rotating_proxies=rotating,
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
                logger.warning(f"[{source}] no cells seeded. Run: uv run careerradar migrate")
                continue

            tasks = select_cells(cells, scraper_config, roles, source)
            if limit:
                tasks = tasks[:limit]
            totals["cells_planned"] += len(tasks)

            logger.info(f"[{source}] {len(tasks)} cells planned")

            empty = 0
            errored = 0
            for task in tasks:
                outcome = _scrape_one(
                    db,
                    source_client,
                    circuit,
                    task,
                    run_id,
                    scorer,
                    taxonomy,
                    roles,
                    config,
                    dry_run,
                )
                if outcome == "tripped":
                    remaining = len(tasks) - tasks.index(task) - 1
                    totals["cells_skipped"] += remaining
                    add_sync_error(
                        source,
                        f"{circuit.trip_reason} — {remaining} cells deferred to the next run.",
                    )
                    break
                if outcome == "ok":
                    totals["cells_succeeded"] += 1
                elif outcome == "empty":
                    empty += 1
                else:
                    errored += 1
                for key in ("postings_fetched", "postings_new", "duplicates_merged", "off_topic"):
                    totals[key] += _LAST_CELL_STATS.get(key, 0)

            totals["cells_empty"] += empty
            totals["cells_error"] += errored
            if empty or errored:
                logger.warning(
                    f"[{source}] {empty + errored} of {len(tasks)} planned cells returned no "
                    f"postings ({empty} empty, {errored} error); they contribute nothing to "
                    "the description census the skill analytics read"
                )

            circuit.note_clean_run()

        _report_coverage(db, scraper_config)

        lost = totals["cells_empty"] + totals["cells_error"]
        status = "ok"
        if any(c.is_open for c in circuits.values()) or totals["cells_skipped"]:
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
                **{k: v for k, v in totals.items() if k != "off_topic"},
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
                totals["postings_fetched"],
                totals["postings_new"],
            )


# Per-cell counters, kept module-level so _scrape_one can report without a return tuple.
_LAST_CELL_STATS: dict[str, int] = {}


def _scrape_one(
    db: Database,
    client: "JobSpySource",
    circuit: SourceCircuit,
    task: "ScrapeTask",
    run_id: int | None,
    scorer: JobScorer,
    taxonomy: "Taxonomy",
    roles: "RoleTaxonomy",
    config: dict[str, Any],
    dry_run: bool,
) -> str:
    """Scrape, normalize, score, and store one cell. Returns ok|empty|error|tripped."""
    global _LAST_CELL_STATS
    _LAST_CELL_STATS = {}

    payload = task.to_dict()
    observed_at = datetime.now(timezone.utc)
    attempts = 0
    max_attempts = 1 + (scraper_retries(config))
    pool = payload.get("proxies") or []
    cost = {"duration_ms": 0, "requests_made": 0}

    while True:
        attempts += 1
        payload["proxies"] = pin_for(pool, task.cell_id, attempts - 1)
        try:
            circuit.before_request()
        except SourceTripped:
            return "tripped"

        try:
            rows = client.fetch_for_task(payload)
            break
        except Exception as exc:  # noqa: BLE001 - classification decides what to do
            _add_cost(cost, client)
            error_class = circuit.on_error(exc, cell=task.cell_id)
            if circuit.is_open:
                _record_failure(db, run_id, payload, observed_at, exc, dry_run, cost=cost)
                return "tripped"
            if error_class == ERROR_TRANSIENT and attempts < max_attempts:
                logger.warning(
                    f"[{task.source}] transient error, retry {attempts}/{max_attempts}: {exc}"
                )
                continue
            _record_failure(
                db, run_id, payload, observed_at, exc, dry_run, circuit=circuit, cost=cost
            )
            return "error"
    _add_cost(cost, client)

    postings, stats = normalize_rows(
        rows,
        payload,
        observed_at=observed_at,
        config=config.get("scraper", {}),
        roles=roles,
        taxonomy=taxonomy,
    )
    saturated = 1 if is_saturated(stats["returned"], task.results_wanted) else 0

    if task.desc_selection == "census" and stats["returned"] and not stats["with_full_description"]:
        logger.warning(
            f"[{task.source}] {task.query!r} in {task.location_id}: {stats['returned']} "
            "postings returned, none with a description -- the description fetch is failing, "
            "and this cell contributes nothing to skill demand"
        )

    new_count = 0
    duplicates = 0
    stored = []

    for posting in postings:
        result = scorer.score(posting)
        posting["match_score"] = result["score"]
        posting["matched_skills"] = result["matched_skills"]
        posting["matched_count"] = result["matched_count"]
        posting["required_count"] = result["required_count"]
        posting["scorer_version"] = SCORER_VERSION
        posting["pipeline_state"] = "new"
        stored.append((posting, result))

        if dry_run:
            continue

        job_id, is_new = db.upsert_posting(posting, run_id=run_id, taxonomy_hash=taxonomy.hash)
        assert job_id is not None, "upsert_posting always inserts or finds a row"
        if is_new:
            new_count += 1
            canonical = db.find_duplicate(posting.get("content_hash"), exclude_id=job_id)
            if canonical:
                db.mark_duplicate(job_id, canonical)
                duplicates += 1
        db.replace_job_skills(job_id, posting.get("skills") or {})
        db.replace_job_blockers(job_id, posting.get("blockers") or [])

    if dry_run:
        _print_dry_run(task, stats, stored, saturated)
    else:
        db.record_observation(
            run_id,
            payload,
            observed_at.isoformat(),
            returned=stats["returned"],
            returned_on_topic=stats["on_topic"],
            new_unique=new_count,
            saturated=saturated,
            descriptions_full=stats["with_full_description"],
            status="ok" if stats["returned"] else "empty",
            duration_ms=cost["duration_ms"],
            requests_made=cost["requests_made"],
        )
        db.record_cell_attempt(
            task.cell_id,
            observed_at.isoformat(),
            task.hours_old,
            task.results_wanted,
            returned=stats["returned"],
            new_unique=new_count,
            saturated=saturated,
            status="ok" if stats["returned"] else "empty",
            ewma=update_ewma(task.ewma_new_per_scrape, new_count),
        )

    if stats["returned"]:
        circuit.on_success(stats["returned"])
    else:
        circuit.on_empty()

    _LAST_CELL_STATS = {
        "postings_fetched": stats["returned"],
        "postings_new": new_count,
        "duplicates_merged": duplicates,
        "off_topic": stats["usable"] - stats["on_topic"],
    }
    return "ok" if stats["returned"] else "empty"


def _add_cost(cost: dict[str, int], client: "BaseJobSource") -> None:
    """Fold one attempt's measured cost into the cell's total, then clear it."""
    measured = client.last_fetch or {}
    cost["duration_ms"] += measured.get("duration_ms", 0)
    cost["requests_made"] += measured.get("requests_made", 0)
    client.last_fetch = {}


def scraper_retries(config: dict[str, Any]) -> int:
    return ((config.get("scraper") or {}).get("circuit_breaker") or {}).get("transient_retries", 2)


def _record_failure(
    db: Database,
    run_id: int | None,
    payload: dict[str, Any],
    observed_at: datetime,
    exc: BaseException,
    dry_run: bool,
    circuit: SourceCircuit | None = None,
    cost: dict[str, int] | None = None,
) -> None:
    if dry_run:
        print(f"  ERROR {payload.get('query')!r}: {exc}")
        return
    db.record_observation(
        run_id,
        payload,
        observed_at.isoformat(),
        status="error",
        error=str(exc),
        duration_ms=(cost or {}).get("duration_ms"),
        requests_made=(cost or {}).get("requests_made"),
    )
    backoff = circuit.cell_backoff(1) if circuit else None
    db.record_cell_attempt(
        payload["cell_id"],
        observed_at.isoformat(),
        payload.get("hours_old"),
        payload.get("results_wanted") or 0,
        status="error",
        error=str(exc),
        backoff_until=backoff,
    )


def _print_dry_run(
    task: "ScrapeTask",
    stats: dict[str, Any],
    stored: list[tuple[dict[str, Any], dict[str, Any]]],
    saturated: int,
) -> None:
    """Show what would be written, without writing it."""
    print(f"\n{'=' * 78}")
    print(
        f"{task.source} | {task.query!r} | {task.location_label} "
        f"({task.location_id}) | hours_old={task.hours_old}"
    )
    print(
        f"  returned={stats['returned']}/{task.results_wanted} "
        f"on_topic={stats['on_topic']} full_desc={stats['with_full_description']} "
        f"with_salary={stats['with_salary']} saturated={bool(saturated)}"
    )
    print(f"  desc_selection={task.desc_selection}  (census feeds skill analytics; top_k does not)")

    for posting, result in sorted(stored, key=lambda p: -p[1]["score"])[:6]:
        family = posting.get("role_family") or "(unclassified — excluded from analytics)"
        print(f"\n  [{result['score']:3}] {posting['title'][:62]}")
        print(f"        {posting['company'][:40]:42} {posting['location'][:28]}")
        print(
            f"        family={family} seniority={posting.get('seniority')} "
            f"remote={posting.get('is_remote')}"
        )
        if posting.get("salary_annual_usd"):
            print(
                f"        salary=${posting['salary_annual_usd']:,.0f}/yr "
                f"({posting.get('salary_currency')} "
                f"{posting.get('salary_interval')})"
            )
        components = ", ".join(f"{k}={v:.2f}" for k, v in result["components"].items())
        print(f"        {components}")
        if result["matched_skills"]:
            print(f"        have:    {', '.join(result['matched_skills'][:10])}")
        if result["missing_skills"]:
            print(f"        MISSING: {', '.join(result['missing_skills'][:10])}")
        if posting.get("blockers"):
            print(f"        blockers: {', '.join(posting['blockers'])}")


def _report_coverage(db: Database, scraper_config: dict[str, Any]) -> None:
    """Surface scheduler coverage failures as warnings."""
    overdue = overdue_cells(db.get_cells(), scraper_config)
    if not overdue:
        return
    sample = ", ".join(f"{c.location_id}/{c.role_family}" for c in overdue[:4])
    add_sync_error(
        "Scheduler",
        f"Coverage: {len(overdue)} core cells past the "
        f"{scraper_config.get('max_staleness_hours', 72)}h floor ({sample}"
        f"{'...' if len(overdue) > 4 else ''}). Supply comparisons for these families "
        f"are suppressed this window.",
        severity="warning",
    )


SCORER_VERSION = 1


def _warn_if_taxonomy_moved(db: Database, taxonomy: "Taxonomy") -> None:
    """Say so when stored postings were scored under a different skills.yaml."""
    stale = search_repo.count_stale_taxonomy(db.conn, taxonomy.hash)
    old_scorer = search_repo.count_old_scorer(db.conn, SCORER_VERSION)
    if stale or old_scorer:
        logger.warning(
            "%s posting(s) scored under an older taxonomy and %s under an older scorer.",
            f"{stale:,}",
            f"{old_scorer:,}",
        )
