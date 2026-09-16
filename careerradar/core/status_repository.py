"""Status repository: operational pipeline health telemetry and status aggregation."""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.status_manager import load_sync_status
from careerradar.profile.models import DEFAULT_PROFILE_VERSION, VERDICT_SCHEMA_VERSION
from careerradar.scoring.repository import (
    count_pending_scoring,
    get_pending_scoring_stats,
)

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
    """Compile the full operational health report for the pipeline (CLI diagnostics)."""
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
            "SELECT COUNT(*) FROM sync_runs "
            "WHERE unixepoch(started_at) >= unixepoch('now', '-7 days')"
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
    profile_version = DEFAULT_PROFILE_VERSION

    try:
        backlog, oldest_new = get_pending_scoring_stats(conn, profile_version=profile_version)
        scored_count = conn.execute(
            "SELECT COUNT(*) FROM job_verdicts WHERE profile_version = ? "
            "AND COALESCE(verdict_schema_version, 1) >= ?",
            (profile_version, VERDICT_SCHEMA_VERSION),
        ).fetchone()[0]
        total_eligible = scored_count + backlog
        backlog_share = (backlog / total_eligible) if total_eligible else 0.0
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        backlog, oldest_new = conn.execute(
            "SELECT COUNT(*), MIN(date_found) FROM jobs WHERE pipeline_state = 'new'"
        ).fetchone()
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        backlog_share = (backlog / total_jobs) if total_jobs else 0.0

    report["score"] = {
        "last_verdict": verdict,
        "hours_since": hours_since(verdict, now),
        "backlog": backlog,
        "backlog_share": backlog_share,
        "oldest_unscored": oldest_new,
        "oldest_unscored_days": (hours_since(oldest_new, now) or 0) / 24.0,
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
    cadence_hours = int(cadence) if isinstance(cadence, (int, float)) else 24
    row = conn.execute(
        "SELECT COUNT(*) AS cells, "
        "SUM(CASE WHEN last_success_at IS NULL THEN 1 ELSE 0 END) AS never, "
        "MAX(last_success_at) AS newest, MIN(last_success_at) AS oldest, "
        "SUM(CASE WHEN consecutive_error > 0 THEN 1 ELSE 0 END) AS erroring, "
        "SUM(CASE WHEN backoff_until IS NOT NULL THEN 1 ELSE 0 END) AS backed_off "
        "FROM scrape_cells WHERE enabled = 1"
    ).fetchone()
    cells = []
    if row and row["cells"]:
        oldest_hours = hours_since(row["oldest"], now)
        cells.append(
            {
                "active": True,
                "cells": row["cells"],
                "never_scraped": row["never"],
                "oldest_success_hours": oldest_hours,
                "cadence_hours": cadence_hours,
                "stale": bool(oldest_hours and oldest_hours > cadence_hours * STALL_MULTIPLE),
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

    # -- search targets -----------------------------------------------------------------
    from careerradar.search.targets import load_targets

    report["search_targets"] = {"search_targets_hash": load_targets().fingerprint}
    return report


def get_pipeline_status(
    conn: sqlite3.Connection,
    sync_running: bool,
    db_instance: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Lightweight, low-latency pipeline progress report for web UI and live SSE events."""
    now = now or datetime.now(timezone.utc)

    # 1. Scrape: query the latest completed run
    last_sync = conn.execute(
        "SELECT started_at, status, cells_succeeded, cells_planned, postings_new "
        "FROM sync_runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_sync is None:
        last_sync = conn.execute(
            "SELECT started_at, status, cells_succeeded, cells_planned, postings_new "
            "FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    previous_run = (
        {
            "last_run": last_sync["started_at"],
            "hours_since": hours_since(last_sync["started_at"], now),
            "status": last_sync["status"],
            "cells": (last_sync["cells_succeeded"], last_sync["cells_planned"]),
            "postings_new": last_sync["postings_new"],
        }
        if last_sync
        else None
    )

    scrape: dict[str, Any] = {
        "in_progress": sync_running,
        "started_at": load_sync_status().get("started_at") if sync_running else None,
        "previous_run": previous_run,
    }

    if sync_running:
        run = conn.execute(
            "SELECT id, cells_planned FROM sync_runs "
            "WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run is not None:
            scrape["cells_done"] = conn.execute(
                "SELECT COUNT(*) FROM cell_observations WHERE sync_run_id = ?",
                (run["id"],),
            ).fetchone()[0]
            planned = run["cells_planned"]
            if not planned and db_instance is not None:
                from careerradar.search.scheduler import scrape_tasks

                config = load_config()
                enabled = [
                    name
                    for name, on in (config.get("scraper", {}).get("sources") or {}).items()
                    if on
                ]
                planned = sum(len(scrape_tasks(db_instance, config, source)) for source in enabled)
            scrape["cells_planned"] = planned or None

    # 2. Score: single query for latest verdict timestamp and recent 5-min verdict count
    v_row = conn.execute(
        "SELECT MAX(created_at), "
        "SUM(CASE WHEN unixepoch(created_at) >= unixepoch('now', '-5 minutes') THEN 1 ELSE 0 END) "
        "FROM job_verdicts"
    ).fetchone()
    last_verdict = v_row[0] if v_row else None
    recent_verdicts = int(v_row[1] or 0) if v_row else 0

    profile_version = DEFAULT_PROFILE_VERSION

    try:
        backlog = count_pending_scoring(conn, profile_version=profile_version)
        scored_count = conn.execute(
            "SELECT COUNT(*) FROM job_verdicts WHERE profile_version = ? "
            "AND COALESCE(verdict_schema_version, 1) >= ?",
            (profile_version, VERDICT_SCHEMA_VERSION),
        ).fetchone()[0]
        total_eligible = scored_count + backlog
        backlog_share = (backlog / total_eligible) if total_eligible else 0.0
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        backlog = conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_state = 'new'").fetchone()[
            0
        ]
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        backlog_share = (backlog / total_jobs) if total_jobs else 0.0

    score = {
        "last_verdict": last_verdict,
        "hours_since": hours_since(last_verdict, now),
        "backlog": backlog,
        "backlog_share": backlog_share,
        "recent_verdicts_5min": recent_verdicts,
    }

    return {
        "scrape": scrape,
        "score": score,
    }
