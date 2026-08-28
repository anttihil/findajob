"""Search repository: scrape cells, source circuit breakers, sync runs, and posting ingestion."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from careerradar.search.scheduler import CellState


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


# =====================================================================================
# Scrape cells
# =====================================================================================


def seed_cells(conn: sqlite3.Connection, specs: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert any missing cells, preserving the state of existing ones.

    Returns (inserted, updated).
    """
    cursor = conn.cursor()
    now = _utcnow()
    before = cursor.execute("SELECT COUNT(*) FROM scrape_cells").fetchone()[0]
    touched = 0
    for spec in specs:
        cursor.execute(
            """
            INSERT INTO scrape_cells
                (source, role_family, location_id, query, tier, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, role_family, location_id, query) DO UPDATE
                SET tier = excluded.tier, enabled = 1
              WHERE tier != excluded.tier OR enabled != 1
            """,
            (
                spec["source"],
                spec["role_family"],
                spec["location_id"],
                spec["query"],
                spec.get("tier", 1),
                now,
            ),
        )
        touched += cursor.rowcount
    after = cursor.execute("SELECT COUNT(*) FROM scrape_cells").fetchone()[0]
    conn.commit()
    inserted = after - before
    return inserted, touched - inserted


def prune_cells(conn: sqlite3.Connection, specs: list[dict[str, Any]]) -> int:
    """Disable cells no longer present in the taxonomy."""
    wanted = {(s["source"], s["role_family"], s["location_id"], s["query"]) for s in specs}
    cursor = conn.cursor()
    disabled = 0
    for row in cursor.execute(
        "SELECT id, source, role_family, location_id, query FROM scrape_cells WHERE enabled = 1"
    ).fetchall():
        key = (row["source"], row["role_family"], row["location_id"], row["query"])
        if key not in wanted:
            conn.execute("UPDATE scrape_cells SET enabled = 0 WHERE id = ?", (row["id"],))
            disabled += 1
    conn.commit()
    return disabled


def get_cells(
    conn: sqlite3.Connection, source: str | None = None, enabled_only: bool = True
) -> list["CellState"]:
    """Load cells as scheduler.CellState objects."""
    from careerradar.search.scheduler import CellState

    query = "SELECT * FROM scrape_cells WHERE 1=1"
    params: list[Any] = []
    if source:
        query += " AND source = ?"
        params.append(source)
    if enabled_only:
        query += " AND enabled = 1"

    cells: list[CellState] = []
    for row in conn.execute(query, params):
        cells.append(
            CellState(
                id=row["id"],
                source=row["source"],
                role_family=row["role_family"],
                location_id=row["location_id"],
                query=row["query"],
                active=bool(row["enabled"]),
                enabled=row["enabled"],
                last_scraped_at=row["last_scraped_at"],
                last_success_at=row["last_success_at"],
                last_result_count=row["last_result_count"] or 0,
                last_saturated=row["last_saturated"] or 0,
                last_hours_old=row["last_hours_old"],
                ewma_new_per_scrape=row["ewma_new_per_scrape"],
                ewma_fit_score=row["ewma_fit_score"],
                quality_samples=row["quality_samples"] or 0,
                consecutive_empty=row["consecutive_empty"] or 0,
                consecutive_error=row["consecutive_error"] or 0,
                total_scrapes=row["total_scrapes"] or 0,
                backoff_until=row["backoff_until"],
            )
        )
    return cells


def record_cell_attempt(
    conn: sqlite3.Connection,
    cell_id: int,
    observed_at: str,
    hours_old: int | None,
    requested: int,
    returned: int = 0,
    new_unique: int = 0,
    saturated: int = 0,
    status: str = "ok",
    error: str | None = None,
    backoff_until: str | None = None,
    ewma: float | None = None,
) -> None:
    """Update a cell after an attempt."""
    cursor = conn.cursor()
    succeeded = status in ("ok", "empty")

    fields = [
        "last_scraped_at = ?",
        "last_requested = ?",
        "last_result_count = ?",
        "last_new_count = ?",
        "last_saturated = ?",
        "last_hours_old = ?",
        "total_scrapes = total_scrapes + 1",
        "total_postings = total_postings + ?",
    ]
    params: list[Any] = [
        observed_at,
        requested,
        returned,
        new_unique,
        saturated,
        hours_old,
        returned,
    ]

    if succeeded:
        fields.append("last_success_at = ?")
        params.append(observed_at)
        fields.append("consecutive_error = 0")
        if returned == 0:
            fields.append("consecutive_empty = consecutive_empty + 1")
        else:
            fields.append("consecutive_empty = 0")
    else:
        fields.append("consecutive_error = consecutive_error + 1")

    if ewma is not None:
        fields.append("ewma_new_per_scrape = ?")
        params.append(ewma)
    if backoff_until is not None:
        fields.append("backoff_until = ?")
        params.append(backoff_until)
    if error is not None:
        fields.append("last_error = ?")
        params.append(error[:500])

    params.append(cell_id)
    cursor.execute(f"UPDATE scrape_cells SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()


def update_cell_quality(conn: sqlite3.Connection, cell_id: int, fit_score: float) -> None:
    """Fold one posting's LLM fit_score into its scrape cell's quality EWMA."""
    from careerradar.search.scheduler import update_ewma

    row = conn.execute(
        "SELECT ewma_fit_score FROM scrape_cells WHERE id = ?", (cell_id,)
    ).fetchone()
    if row is None:
        return
    new_ewma = update_ewma(row["ewma_fit_score"], fit_score)
    conn.execute(
        "UPDATE scrape_cells SET ewma_fit_score = ?, quality_samples = quality_samples + 1 "
        "WHERE id = ?",
        (new_ewma, cell_id),
    )


# =====================================================================================
# Source circuit-breaker state
# =====================================================================================


def get_source_backoff(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT backoff_until FROM source_state WHERE source = ?", (source,)
    ).fetchone()
    return row["backoff_until"] if row else None


def get_source_trips(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT consecutive_trips FROM source_state WHERE source = ?", (source,)
    ).fetchone()
    return (row["consecutive_trips"] if row else 0) or 0


def set_source_backoff(
    conn: sqlite3.Connection,
    source: str,
    until: str,
    reason: str | None = None,
    escalate: bool = False,
) -> None:
    now = _utcnow()
    conn.execute(
        """
        INSERT INTO source_state
            (source, backoff_until, consecutive_trips, last_trip_at,
             last_trip_reason, total_429)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (source) DO UPDATE SET
            backoff_until = excluded.backoff_until,
            consecutive_trips = source_state.consecutive_trips + ?,
            last_trip_at = excluded.last_trip_at,
            last_trip_reason = excluded.last_trip_reason,
            total_429 = source_state.total_429 + ?
        """,
        (
            source,
            until,
            1 if escalate else 0,
            now,
            reason,
            1 if escalate else 0,
            1 if escalate else 0,
            1 if escalate else 0,
        ),
    )
    conn.commit()


def reset_source_trips(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        "UPDATE source_state SET consecutive_trips = 0, backoff_until = NULL WHERE source = ?",
        (source,),
    )
    conn.commit()


# =====================================================================================
# Sync runs and cell observations
# =====================================================================================


def start_sync_run(
    conn: sqlite3.Connection,
    mode: str,
    taxonomy_hash: str | None = None,
    plan_hash: str | None = None,
) -> int | None:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sync_runs (started_at, mode, status, taxonomy_hash, plan_hash) "
        "VALUES (?, ?, 'running', ?, ?)",
        (_utcnow(), mode, taxonomy_hash, plan_hash),
    )
    conn.commit()
    return cursor.lastrowid


def finish_sync_run(conn: sqlite3.Connection, run_id: int, status: str, **counters: Any) -> None:
    allowed = {
        "cells_planned",
        "cells_succeeded",
        "cells_skipped",
        "postings_fetched",
        "postings_new",
        "duplicates_merged",
        "llm_cost_usd",
        "error_summary",
    }
    fields = ["finished_at = ?", "status = ?"]
    params: list[Any] = [_utcnow(), status]
    for key, value in counters.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            params.append(
                json.dumps(value)
                if key == "error_summary" and not isinstance(value, str)
                else value
            )
    params.append(run_id)
    conn.execute(f"UPDATE sync_runs SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()


def record_observation(
    conn: sqlite3.Connection,
    run_id: int | None,
    task: dict[str, Any],
    observed_at: str,
    returned: int = 0,
    returned_on_topic: int = 0,
    new_unique: int = 0,
    saturated: int = 0,
    descriptions_full: int = 0,
    status: str = "ok",
    error: str | BaseException | None = None,
    duration_ms: int | None = None,
    requests_made: int | None = None,
) -> None:
    """Write the sampling denominator for one cell visit."""
    window_start = None
    if task.get("hours_old"):
        window_start = (_parse_iso(observed_at) - timedelta(hours=task["hours_old"])).isoformat()

    conn.execute(
        """
        INSERT INTO cell_observations
            (sync_run_id, cell_id, source, role_family, location_id, query,
             observed_at, hours_old, window_start, window_end,
             requested, returned, returned_on_topic, new_unique, saturated,
             desc_selection, descriptions_full, status, error,
             duration_ms, requests_made)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            task.get("cell_id"),
            task.get("source"),
            task.get("role_family") or "",
            task.get("location_id") or "",
            task.get("query") or "",
            observed_at,
            task.get("hours_old"),
            window_start,
            observed_at,
            task.get("results_wanted") or 0,
            returned,
            returned_on_topic,
            new_unique,
            saturated,
            task.get("desc_selection", "none"),
            descriptions_full,
            status,
            (error or None) and str(error)[:500],
            duration_ms,
            requests_made,
        ),
    )
    conn.commit()


# =====================================================================================
# Enriched posting upsert & deduplication
# =====================================================================================

POSTING_COLUMNS: list[str] = [
    "job_key",
    "title",
    "company",
    "company_normalized",
    "location",
    "city",
    "region",
    "country",
    "url",
    "url_direct",
    "description",
    "source",
    "site_job_id",
    "role_family",
    "role_family_hint",
    "seniority",
    "is_remote",
    "date_posted",
    "date_precision",
    "posted_window_start",
    "posted_window_end",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_interval",
    "salary_annual_usd",
    "salary_currency_inferred",
    "salary_source",
    "description_quality",
    "desc_selection",
    "content_hash",
    "is_agency",
    "company_num_employees",
    "company_industry",
    "access",
    "scrape_cell_id",
    "sync_run_id",
    "taxonomy_hash",
    "match_score",
    "matched_skills",
    "matched_count",
    "required_count",
    "scorer_version",
    "pipeline_state",
]


def upsert_posting(
    conn: sqlite3.Connection,
    posting: dict[str, Any],
    run_id: int | None = None,
    taxonomy_hash: str | None = None,
) -> tuple[int | None, bool]:
    """Insert or refresh one posting. Returns (job_id, is_new)."""
    cursor = conn.cursor()
    record = dict(posting)
    record["sync_run_id"] = run_id
    record["taxonomy_hash"] = taxonomy_hash
    if isinstance(record.get("matched_skills"), (list, dict)):
        record["matched_skills"] = json.dumps(record["matched_skills"])

    existing = None
    if record.get("job_key"):
        existing = cursor.execute(
            "SELECT id FROM jobs WHERE job_key = ?", (record["job_key"],)
        ).fetchone()
    if existing is None and record.get("url"):
        existing = cursor.execute("SELECT id FROM jobs WHERE url = ?", (record["url"],)).fetchone()

    if existing is not None:
        job_id = existing["id"]
        updatable = [c for c in POSTING_COLUMNS if c in record and c not in ("job_key", "url")]
        cursor.execute(
            f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in updatable)}"
            f"{', ' if updatable else ' '}"
            "last_seen_at = ?, times_seen = COALESCE(times_seen, 1) + 1 "
            "WHERE id = ?",
            [record[c] for c in updatable] + [_utcnow(), job_id],
        )
        conn.commit()
        return job_id, False

    columns = [c for c in POSTING_COLUMNS if c in record]
    record["last_seen_at"] = _utcnow()
    columns.append("last_seen_at")
    columns.append("date_found")
    values = [record[c] for c in columns[:-1]]
    values.append(_utcnow())

    cursor.execute(
        f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    conn.commit()
    return cursor.lastrowid, True


def find_duplicate(
    conn: sqlite3.Connection, content_hash: str | None, exclude_id: int | None = None
) -> int | None:
    """Earliest non-duplicate posting sharing a content hash."""
    if not content_hash:
        return None
    query = "SELECT id FROM jobs WHERE content_hash = ? AND duplicate_of IS NULL"
    params: list[Any] = [content_hash]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    query += " ORDER BY id LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return row["id"] if row else None


def mark_duplicate(conn: sqlite3.Connection, job_id: int, canonical_id: int) -> None:
    conn.execute("UPDATE jobs SET duplicate_of = ? WHERE id = ?", (canonical_id, job_id))
    conn.commit()


def replace_job_skills(
    conn: sqlite3.Connection, job_id: int, skills: dict[str, dict[str, Any]]
) -> None:
    """Rewrite a posting's skill rows. `skills` is {key: {"in_title": bool}}."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM job_skills WHERE job_id = ?", (job_id,))
    if skills:
        cursor.executemany(
            "INSERT OR REPLACE INTO job_skills (job_id, skill, in_title) VALUES (?, ?, ?)",
            [(job_id, key, 1 if info.get("in_title") else 0) for key, info in skills.items()],
        )
    conn.commit()


def replace_job_blockers(conn: sqlite3.Connection, job_id: int, blockers: list[str]) -> None:
    cursor = conn.cursor()
    cursor.execute("DELETE FROM job_blockers WHERE job_id = ?", (job_id,))
    if blockers:
        cursor.executemany(
            "INSERT OR REPLACE INTO job_blockers (job_id, blocker) VALUES (?, ?)",
            [(job_id, b) for b in blockers],
        )
    conn.commit()


def count_stale_taxonomy(conn: sqlite3.Connection, taxonomy_hash: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE taxonomy_hash IS NOT NULL AND taxonomy_hash != ?",
        (taxonomy_hash,),
    ).fetchone()[0]


def count_old_scorer(conn: sqlite3.Connection, scorer_version: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(scorer_version, 0) < ?",
        (scorer_version,),
    ).fetchone()[0]
