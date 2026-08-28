"""Status repository: operational pipeline health telemetry and status aggregation."""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.status_manager import load_sync_status
from careerradar.scoring.repository import count_recent_verdicts

STALL_MULTIPLE = 2.0
COVERAGE_SKEW = 0.20


def hours_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 3600.0


def collect_status_report(
    conn: sqlite3.Connection,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compile the operational health report for the pipeline."""
    now = now or datetime.now(timezone.utc)
    config = load_config()
    scraper = config.get("scraper", {}) or {}
    cadence = scraper.get("cadence_hours") or {}

    report: dict[str, Any] = {"generated_at": now.isoformat()}

    # -- stages ---------------------------------------------------------------------
    run = conn.execute(
        "SELECT started_at, mode, status, cells_planned, cells_succeeded, postings_new "
        "FROM sync_runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        run = conn.execute(
            "SELECT started_at, mode, status, cells_planned, cells_succeeded, postings_new "
            "FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    runs_per_day = (
        conn.execute(
            "SELECT COUNT(*) FROM sync_runs WHERE started_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        / 7.0
    )
    report["search"] = {
        "last_run": run["started_at"] if run else None,
        "hours_since": hours_since(run["started_at"], now) if run else None,
        "status": run["status"] if run else None,
        "cells": (run["cells_succeeded"], run["cells_planned"]) if run else None,
        "postings_new": run["postings_new"] if run else None,
        "runs_per_day_7d": round(runs_per_day, 1),
    }

    verdict = conn.execute("SELECT MAX(created_at) FROM job_verdicts").fetchone()[0]
    backlog, oldest_new = conn.execute(
        "SELECT COUNT(*), MIN(date_found) FROM jobs WHERE pipeline_state = 'new'"
    ).fetchone()
    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    report["score"] = {
        "last_verdict": verdict,
        "hours_since": hours_since(verdict, now),
        "backlog": backlog,
        "backlog_share": (backlog / total_jobs) if total_jobs else 0.0,
        "oldest_unscored": oldest_new,
        "oldest_unscored_days": (hours_since(oldest_new, now) or 0) / 24.0,
    }

    research = conn.execute(
        "SELECT started_at, status, companies FROM research_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    report["research"] = {
        "last_run": research["started_at"] if research else None,
        "hours_since": hours_since(research["started_at"], now) if research else None,
        "status": research["status"] if research else None,
        "dossiers": conn.execute("SELECT COUNT(*) FROM company_dossiers").fetchone()[0],
    }

    # -- verdict coverage, by source ---------------------------------------------------
    report["coverage"] = [
        {
            "source": row["source"] or "unknown",
            "postings": row["postings"],
            "scored": row["scored"],
            "share": (row["scored"] / row["postings"]) if row["postings"] else 0.0,
        }
        for row in conn.execute(
            "SELECT j.source, COUNT(*) AS postings, "
            "SUM(CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END) AS scored "
            "FROM jobs j LEFT JOIN job_verdicts v ON v.job_id = j.id "
            "GROUP BY j.source ORDER BY postings DESC"
        )
    ]

    # -- cells ------------------------------------------------------------------------
    cells = []
    for row in conn.execute(
        "SELECT tier, COUNT(*) AS cells, "
        "SUM(CASE WHEN last_success_at IS NULL THEN 1 ELSE 0 END) AS never, "
        "MAX(last_success_at) AS newest, MIN(last_success_at) AS oldest, "
        "SUM(CASE WHEN consecutive_error > 0 THEN 1 ELSE 0 END) AS erroring, "
        "SUM(CASE WHEN backoff_until IS NOT NULL THEN 1 ELSE 0 END) AS backed_off "
        "FROM scrape_cells WHERE enabled = 1 GROUP BY tier"
    ):
        oldest_hours = hours_since(row["oldest"], now)
        tier_cadence = cadence.get(row["tier"], 168)
        cells.append(
            {
                "tier": row["tier"],
                "cells": row["cells"],
                "never_scraped": row["never"],
                "oldest_success_hours": oldest_hours,
                "cadence_hours": tier_cadence,
                "stale": bool(oldest_hours and oldest_hours > tier_cadence * STALL_MULTIPLE),
                "erroring": row["erroring"],
                "backed_off": row["backed_off"],
            }
        )
    report["cells"] = cells

    # -- quarantine ---------------------------------------------------------------------
    report["quarantined"] = [
        {"id": row["id"], "title": row["title"], "error": row["last_scoring_error"]}
        for row in conn.execute(
            "SELECT id, title, last_scoring_error FROM jobs WHERE scoring_failures >= 3 "
            "ORDER BY last_scoring_failure_at DESC LIMIT 10"
        )
    ]

    # -- taxonomy drift -----------------------------------------------------------------
    from careerradar.taxonomy.roles import load_roles
    from careerradar.taxonomy.skills import load_taxonomy

    stored = [
        (row["taxonomy_hash"], row["n"])
        for row in conn.execute(
            "SELECT taxonomy_hash, COUNT(*) AS n FROM jobs WHERE taxonomy_hash IS NOT NULL "
            "GROUP BY taxonomy_hash ORDER BY n DESC"
        )
    ]
    report["taxonomy"] = {
        "skills_hash": load_taxonomy().hash,
        "roles_hash": load_roles().hash,
        "stored": stored,
    }
    return report


def get_pipeline_status(
    conn: sqlite3.Connection,
    sync_running: bool,
    db_instance: Any = None,
) -> dict[str, Any]:
    """Unified pipeline status report for web endpoints and live event hubs."""
    from careerradar.search.scheduler import scrape_tasks
    from careerradar.taxonomy.roles import load_roles

    report = collect_status_report(conn)

    scrape: dict[str, Any] = {
        "in_progress": sync_running,
        "started_at": load_sync_status().get("started_at") if sync_running else None,
        "previous_run": report["search"],
    }
    if sync_running:
        run = conn.execute(
            "SELECT id, cells_planned FROM sync_runs "
            "WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run is not None:
            config = load_config()
            roles = load_roles()
            enabled = [
                name for name, on in (config.get("scraper", {}).get("sources") or {}).items() if on
            ]
            scrape["cells_done"] = conn.execute(
                "SELECT COUNT(*) FROM cell_observations WHERE sync_run_id = ?",
                (run["id"],),
            ).fetchone()[0]
            planned = run["cells_planned"]
            if not planned and db_instance is not None:
                planned = sum(
                    len(scrape_tasks(db_instance, config, roles, source)) for source in enabled
                )
            scrape["cells_planned"] = planned or None

    recent_verdicts = count_recent_verdicts(conn, minutes=5)

    return {
        "scrape": scrape,
        "score": {**report["score"], "recent_verdicts_5min": recent_verdicts},
    }
