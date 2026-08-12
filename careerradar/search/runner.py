"""Scrape, normalize, score, and store job postings.

Replaces the previous Gmail-IMAP pipeline, which read UNSEEN inbox mail (mutating the
mailbox, making each run non-idempotent) and produced zero rows.

    uv run python sync.py --dry-run --limit 5     # writes nothing; verifies the scrapers
    uv run python sync.py --backfill --limit 25   # deep first pass, Indeed only
    uv run python sync.py                         # incremental, both sources

Run this from the CLI rather than the dashboard's Sync button during development: run.py
starts uvicorn with reload=True and the sync runs in-process, so saving a file mid-scrape
kills it.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.logger import get_logger
from careerradar.search.normalizer import normalize_rows
from careerradar.profile.adapter import NoActiveProfile, load_profile
from careerradar.search.proxies import apply_proxy_budgets, is_rotating, load_proxies, pin_for
from careerradar.taxonomy.roles import load_roles
from careerradar.search.scheduler import (
    is_saturated,
    overdue_cells,
    select_cells,
    with_location_weights,
    update_ewma,
)
from careerradar.search.keyword_score import JobScorer
from careerradar.search.guard import (
    ERROR_TRANSIENT,
    SourceCircuit,
    SourceTripped,
)
from careerradar.core.status_manager import (
    add_sync_error,
    clear_stale_lock,
    is_sync_running,
    load_sync_status,
    set_sync_progress,
)
from careerradar.taxonomy.skills import load_taxonomy

logger = get_logger()

from careerradar.core.paths import ARCHIVE_DIR  # noqa: E402


def plan_hash(roles, config):
    """Identify the scrape plan, so trend queries can refuse to cross plan changes.

    Widening the role catalog or the location set changes what the corpus samples; a trend
    computed across such a change measures the plan, not the market.
    """
    import hashlib

    scraper = config.get("scraper", {})
    payload = "|".join([
        roles.hash,
        ",".join(sorted(k for k, v in (scraper.get("sources") or {}).items() if v)),
        str(sorted((scraper.get("cadence_hours") or {}).items())),
        str(sorted((scraper.get("budgets") or {}).keys())),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def run_sync(dry_run=False, backfill=False, limit=None, sources=None,
             rescore_only=False, force=False):
    logger.info("=" * 60)
    mode = "dry_run" if dry_run else ("backfill" if backfill else "incremental")
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

    for label, problems in (("skills.yaml", taxonomy.validate()),
                            ("roles.yaml", roles.validate())):
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
    logger.info(
        f"Profile: v{profile.version}, {len(profile)} skills (taxonomy {taxonomy.hash})"
    )

    if not dry_run:
        # The dashboard's /api/sync has always checked this; the CLI never did, which was
        # survivable while every run was launched by hand. Under a timer it is not: a
        # scheduled run landing on top of a manual one double-scrapes the same cells,
        # races both writers on cell rotation state, and leaves whichever finishes second
        # owning sync_status.json. systemd only guarantees a unit is not started twice --
        # it knows nothing about a run started from a terminal.
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
        "cells_planned": 0, "cells_succeeded": 0, "cells_skipped": 0,
        "postings_fetched": 0, "postings_new": 0, "duplicates_merged": 0,
        "off_topic": 0,
    }
    circuits = {}

    try:
        scorer = JobScorer(
            profile, roles, taxonomy,
            weights=(config.get("matching") or {}).get("weights"),
        )

        if rescore_only:
            return _rescore(db, scorer, taxonomy)

        if not dry_run:
            run_id = db.start_sync_run(
                mode, taxonomy_hash=taxonomy.hash,
                plan_hash=plan_hash(roles, config),
            )

        enabled = sources or [
            name for name, on in (scraper_config.get("sources") or {}).items() if on
        ]
        # A 429 during a backfill would poison the incremental phase too, and LinkedIn
        # contributes little to a deep first pass.
        if backfill:
            enabled = [s for s in enabled if s == "indeed"] or ["indeed"]
            logger.info("Backfill mode: Indeed only")

        from careerradar.search.sources.jobspy_source import JobSpySource, prune_archives

        source_client = JobSpySource(
            archive_dir=None if dry_run else ARCHIVE_DIR
        )
        if not dry_run:
            # Before scraping, so a run that dies partway still leaves the archive bounded.
            prune_archives(
                ARCHIVE_DIR, scraper_config.get("archive_retention_days", 14)
            )
        _warn_if_taxonomy_moved(db, taxonomy)

        for source in enabled:
            circuit = SourceCircuit(
                source, scraper_config, db=None if dry_run else db,
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
                logger.warning(
                    f"[{source}] no cells seeded. Run: "
                    f"uv run python -m scripts.seed_cells"
                )
                continue

            tasks = select_cells(
                cells, scraper_config, roles, source, backfill=backfill
            )
            if limit:
                tasks = tasks[:limit]
            totals["cells_planned"] += len(tasks)

            logger.info(f"[{source}] {len(tasks)} cells planned")

            for task in tasks:
                outcome = _scrape_one(
                    db, source_client, circuit, task, run_id, scorer, taxonomy,
                    roles, config, dry_run,
                )
                if outcome == "tripped":
                    remaining = len(tasks) - tasks.index(task) - 1
                    totals["cells_skipped"] += remaining
                    add_sync_error(
                        source,
                        f"{circuit.trip_reason} — {remaining} cells deferred "
                        f"to the next run.",
                    )
                    break
                if outcome == "ok":
                    totals["cells_succeeded"] += 1
                for key in ("postings_fetched", "postings_new", "duplicates_merged",
                            "off_topic"):
                    totals[key] += _LAST_CELL_STATS.get(key, 0)

            circuit.note_clean_run()

        _report_coverage(db, scraper_config)


        status = "ok"
        if any(c.is_open for c in circuits.values()) or totals["cells_skipped"]:
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
                run_id, status,
                error_summary={s: c.summary() for s, c in circuits.items()},
                **{k: v for k, v in totals.items() if k != "off_topic"},
            )
        return totals

    except Exception as exc:
        logger.error(f"Critical sync failure: {exc}", exc_info=True)
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
_LAST_CELL_STATS = {}


def _scrape_one(db, client, circuit, task, run_id, scorer, taxonomy, roles, config,
                dry_run):
    """Scrape, normalize, score, and store one cell. Returns ok|empty|error|tripped."""
    global _LAST_CELL_STATS
    _LAST_CELL_STATS = {}

    payload = task.to_dict()
    observed_at = datetime.now(timezone.utc)
    attempts = 0
    max_attempts = 1 + (scraper_retries(config))
    # The whole pool travels on the task; one endpoint is pinned per attempt so the cell
    # keeps a warm connection, and a retry moves to a different exit IP.
    pool = payload.get("proxies") or []

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
            error_class = circuit.on_error(exc, cell=task.cell_id)
            if circuit.is_open:
                _record_failure(db, run_id, payload, observed_at, exc, dry_run)
                return "tripped"
            if error_class == ERROR_TRANSIENT and attempts < max_attempts:
                logger.warning(
                    f"[{task.source}] transient error, retry "
                    f"{attempts}/{max_attempts}: {exc}"
                )
                continue
            _record_failure(db, run_id, payload, observed_at, exc, dry_run,
                            circuit=circuit)
            return "error"

    postings, stats = normalize_rows(
        rows, payload, observed_at=observed_at,
        config=config.get("scraper", {}), roles=roles, taxonomy=taxonomy,
    )
    saturated = 1 if is_saturated(stats["returned"], task.results_wanted) else 0

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

        # Store on relevance, not on keyword score. The old gate dropped anything scoring
        # below matching.min_match_score, which meant a keyword heuristic decided what the
        # judgement layer was ever allowed to see -- and keyword coverage is exactly the
        # thing that misjudges a career change or an unusual title. `role_family is None`
        # means the title did not classify as software work at all (roles.yaml exclusions:
        # RF engineer, HVAC, stationary engineer, recruiters), which is a different and
        # much safer claim than "few of the user's keywords appear".
        if dry_run or not posting.get("role_family"):
            continue

        job_id, is_new = db.upsert_posting(
            posting, run_id=run_id, taxonomy_hash=taxonomy.hash
        )
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
            run_id, payload, observed_at.isoformat(),
            returned=stats["returned"],
            returned_on_topic=stats["on_topic"],
            new_unique=new_count,
            saturated=saturated,
            descriptions_full=stats["with_full_description"],
            status="ok" if stats["returned"] else "empty",
        )
        db.record_cell_attempt(
            task.cell_id, observed_at.isoformat(), task.hours_old,
            task.results_wanted, returned=stats["returned"], new_unique=new_count,
            saturated=saturated, status="ok" if stats["returned"] else "empty",
            # `task.ewma_new_per_scrape` carries the cell's persisted value. Passing
            # None here (as this did) makes update_ewma return the raw observation, so the
            # stored EWMA was overwritten with the latest count on every attempt and the
            # smoothing in cell_priority's productivity term never actually smoothed.
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


def scraper_retries(config):
    return ((config.get("scraper") or {}).get("circuit_breaker") or {}).get(
        "transient_retries", 2
    )


def _record_failure(db, run_id, payload, observed_at, exc, dry_run, circuit=None):
    if dry_run:
        print(f"  ERROR {payload.get('query')!r}: {exc}")
        return
    db.record_observation(
        run_id, payload, observed_at.isoformat(), status="error", error=str(exc)
    )
    backoff = circuit.cell_backoff(1) if circuit else None
    db.record_cell_attempt(
        payload.get("cell_id"), observed_at.isoformat(), payload.get("hours_old"),
        payload.get("results_wanted") or 0, status="error", error=str(exc),
        backoff_until=backoff,
    )


def _print_dry_run(task, stats, stored, saturated):
    """Show what would be written, without writing it."""
    print(f"\n{'=' * 78}")
    print(f"{task.source} | {task.query!r} | {task.location_label} "
          f"({task.location_id}) | hours_old={task.hours_old}")
    print(f"  returned={stats['returned']}/{task.results_wanted} "
          f"on_topic={stats['on_topic']} full_desc={stats['with_full_description']} "
          f"with_salary={stats['with_salary']} saturated={bool(saturated)}")
    print(f"  desc_selection={task.desc_selection}  "
          f"(census feeds skill analytics; top_k does not)")

    for posting, result in sorted(stored, key=lambda p: -p[1]["score"])[:6]:
        family = posting.get("role_family") or "(unclassified — excluded from analytics)"
        print(f"\n  [{result['score']:3}] {posting['title'][:62]}")
        print(f"        {posting['company'][:40]:42} {posting['location'][:28]}")
        print(f"        family={family} seniority={posting.get('seniority')} "
              f"remote={posting.get('is_remote')}")
        if posting.get("salary_annual_usd"):
            print(f"        salary=${posting['salary_annual_usd']:,.0f}/yr "
                  f"({posting.get('salary_currency')} "
                  f"{posting.get('salary_interval')})")
        components = ", ".join(
            f"{k}={v:.2f}" for k, v in result["components"].items()
        )
        print(f"        {components}")
        if result["matched_skills"]:
            print(f"        have:    {', '.join(result['matched_skills'][:10])}")
        if result["missing_skills"]:
            print(f"        MISSING: {', '.join(result['missing_skills'][:10])}")
        if posting.get("blockers"):
            print(f"        blockers: {', '.join(posting['blockers'])}")


def _report_coverage(db, scraper_config):
    """Surface scheduler coverage failures as warnings.

    A coverage gap must become a visible caveat on the affected families rather than a
    quietly wrong bar in the supply chart.
    """
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


# Which scorer wrote `match_score`. 1 is the coverage scorer without BM25; 0 means a row
# predates the column and its score is not comparable with anything.
SCORER_VERSION = 1


def _warn_if_taxonomy_moved(db, taxonomy):
    """Say so when stored postings were scored under a different skills.yaml.

    Editing the taxonomy silently changes what `match_score` and `job_skills` mean, and
    nothing previously noticed: `jobs.taxonomy_hash` went stale and the rows kept being
    compared with fresh ones. The remedy is one flag, so the warning names it.
    """
    stale = db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE taxonomy_hash IS NOT NULL AND taxonomy_hash != ?",
        (taxonomy.hash,),
    ).fetchone()[0]
    old_scorer = db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(scorer_version, 0) < ?",
        (SCORER_VERSION,),
    ).fetchone()[0]
    if stale or old_scorer:
        logger.warning(
            "%s posting(s) scored under an older taxonomy and %s under an older scorer. "
            "Run `careerradar search run --rescore-only` to bring them current.",
            f"{stale:,}", f"{old_scorer:,}",
        )


def _rescore(db, scorer, taxonomy):
    """Re-derive scores and skills for stored postings under the current taxonomy.

    Editing skills.yaml changes what a run would have measured, so history recorded under an
    older taxonomy is not comparable. This makes the choice explicit: keep snapshots as
    recorded (reproducible), or rescore everything (consistent).
    """
    # Not `description_quality = 'full'`. The v2 migration only set that flag where the
    # description was already >= 400 chars, so the shorter rows were permanently
    # unreachable by rescore -- which is part of why 121 postings still carried display
    # labels in `matched_skills` from a scorer two versions old.
    rows = db.conn.execute(
        "SELECT id, title, description, role_family, seniority FROM jobs "
        "WHERE description IS NOT NULL AND length(description) > 0"
    ).fetchall()
    logger.info(f"Rescoring {len(rows)} postings under taxonomy {taxonomy.hash}")

    for row in rows:
        posting = dict(row)
        posting["skills"] = taxonomy.extract(
            posting["description"], title=posting["title"]
        )
        result = scorer.score(posting)
        # The failure that produced the 121 stale rows was silent for the whole life of the
        # column: a scorer wrote display labels where canonical keys belong and nothing
        # noticed. One set lookup is cheap insurance.
        unknown = [k for k in result["matched_skills"] if k not in taxonomy.skills]
        if unknown:
            raise ValueError(
                f"matched_skills for job {row['id']} contains non-canonical keys: {unknown}"
            )
        db.conn.execute(
            "UPDATE jobs SET match_score = ?, matched_skills = ?, matched_count = ?, "
            "required_count = ?, scorer_version = ?, taxonomy_hash = ? WHERE id = ?",
            (result["score"], json.dumps(result["matched_skills"]),
             result["matched_count"], result["required_count"], SCORER_VERSION,
             taxonomy.hash, row["id"]),
        )
        db.replace_job_skills(row["id"], posting["skills"])
        db.replace_job_blockers(
            row["id"], taxonomy.extract_blockers(posting["description"])
        )
    db.conn.commit()
    logger.info("Rescore complete")
    return {"rescored": len(rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape a small sample, print what would be stored, "
                             "write nothing")
    parser.add_argument("--backfill", action="store_true",
                        help="deep first pass: Indeed only, max results, widest window")
    parser.add_argument("--limit", type=int,
                        help="cap cells per source (use with --dry-run)")
    parser.add_argument("--source", action="append", dest="sources",
                        choices=["indeed", "linkedin"],
                        help="restrict to one source (repeatable)")
    parser.add_argument("--rescore-only", action="store_true",
                        help="re-derive scores and skills for stored postings under the "
                             "current taxonomy; no scraping")
    parser.add_argument("--force", action="store_true",
                        help="start even if another sync holds the lock (only when you "
                             "know the other run is gone)")
    args = parser.parse_args(argv)

    if args.dry_run and not args.limit:
        args.limit = 3

    result = run_sync(
        dry_run=args.dry_run, backfill=args.backfill, limit=args.limit,
        sources=args.sources, rescore_only=args.rescore_only, force=args.force,
    )
    # run_sync returns its totals on every path that actually ran, and None only when it
    # refused to start (invalid taxonomy, or the lock is held). Propagate that as an exit
    # code so a timer-launched unit records a failure rather than reporting success for a
    # sync that never happened.
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
