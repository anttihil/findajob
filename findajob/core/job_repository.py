"""Job repository: job feed queries, filtering, sorting, dashboard stats, and status updates."""

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from findajob.profile.models import DEFAULT_PROFILE_VERSION

# Memoized `get_stats()` results, keyed by database path: (data_version, write_generation) -> stats.
_StatsCacheEntry = tuple[tuple[int, int], dict[str, Any]]

# Memoized `get_stats()` results, keyed by database path.
_STATS_CACHE: dict[str, _StatsCacheEntry] = {}

# Memoized `job_ids_for()` results.
_FEED_IDS_CACHE: dict[tuple[Any, ...], list[int]] = {}

# Bumped by writes made on the same connection that later reads stats.
_WRITE_GENERATION = 0


def invalidate_stats() -> None:
    global _WRITE_GENERATION
    _WRITE_GENERATION += 1


def fts_match_query(query: str) -> str:
    """Turn dashboard text into an ANDed, prefix FTS5 query.

    Quoting each whitespace-delimited term prevents FTS operators in user input
    from changing query semantics. Prefix matching keeps ordinary type-ahead
    searches useful without sacrificing the index.
    """
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in query.split())


LIVENESS_CASE = """
    CASE
      WHEN jobs.last_seen_at IS NULL OR cell.last_success_at IS NULL THEN 'unknown'
      WHEN unixepoch(cell.last_success_at) - unixepoch(jobs.last_seen_at) > 43200 THEN
           CASE
             WHEN jobs.date_posted IS NOT NULL
                  AND (unixepoch(cell.last_success_at)
                       - unixepoch(jobs.date_posted))
                      <= COALESCE(cell.last_hours_old, 0) * 3600
                  THEN 'likely_closed'
             ELSE 'unknown'
           END
      WHEN unixepoch('now') - unixepoch(cell.last_success_at) > 604800 THEN 'stale'
      ELSE 'live'
    END"""

ACTIVE_PROFILE_VERSION = str(DEFAULT_PROFILE_VERSION)

FEED_FROM = f"""
      FROM jobs
      LEFT JOIN job_verdicts v
             ON v.job_id = jobs.id
            AND v.profile_version = ({ACTIVE_PROFILE_VERSION})
      LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id
"""


VERDICT_LIST_COLUMNS = (
    "fit",
    "reason_type",
    "reason_description",
)
VERDICT_DETAIL_COLUMNS = VERDICT_LIST_COLUMNS

LIST_OMITTED_JOB_COLUMNS = frozenset({"description"})
JSON_COLUMNS = ("matched_skills",)

DATE_POSTED_WINDOWS: dict[str, float] = {
    "24h": 1.0,
    "3d": 3.0,
    "7d": 7.0,
    "14d": 14.0,
    "30d": 30.0,
}

FEED_ORDER_BY = (
    "v.fit DESC NULLS LAST, "
    "COALESCE(jobs.date_posted, jobs.date_found) DESC, "
    "jobs.match_score DESC, "
    "jobs.date_found DESC, "
    "jobs.id DESC"
)

# Do not group records with a missing company or title.  A blank field is not an identity,
# and grouping it would hide unrelated imports or incomplete board rows.
_GROUP_COMPANY_KEY = "normalize_company(jobs.company)"
_GROUP_TITLE_KEY = "normalize_title(jobs.title)"
_GROUP_HAS_IDENTITY = f"({_GROUP_COMPANY_KEY} <> '' AND {_GROUP_TITLE_KEY} <> '')"
_GROUP_COMPANY_PARTITION = (
    f"CASE WHEN {_GROUP_HAS_IDENTITY} THEN {_GROUP_COMPANY_KEY} "
    "ELSE printf('__job__%d', jobs.id) END"
)
_GROUP_TITLE_PARTITION = (
    f"CASE WHEN {_GROUP_HAS_IDENTITY} THEN {_GROUP_TITLE_KEY} ELSE printf('__job__%d', jobs.id) END"
)
_GROUP_PARTITION_BY = f"{_GROUP_COMPANY_PARTITION}, {_GROUP_TITLE_PARTITION}"


def select_columns(
    conn: sqlite3.Connection,
    detail: bool,
    cached_columns: list[str] | None = None,
) -> tuple[str, list[str]]:
    """The SELECT list, minus what the caller will not read."""
    if cached_columns is None:
        cached_columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
    omitted = frozenset() if detail else LIST_OMITTED_JOB_COLUMNS
    job_columns = [f"jobs.{name}" for name in cached_columns if name not in omitted]
    verdict_columns = [
        f"v.{name} AS {name}"
        for name in (VERDICT_DETAIL_COLUMNS if detail else VERDICT_LIST_COLUMNS)
    ]
    resume_columns = [
        (
            "(SELECT gr.id FROM generated_resumes gr "
            "WHERE gr.job_id = jobs.id ORDER BY gr.id DESC LIMIT 1) AS tailored_resume_id"
        )
    ]
    columns_str = ",\n                   ".join(
        job_columns + verdict_columns + resume_columns + [f"{LIVENESS_CASE} AS liveness"]
    )
    return columns_str, cached_columns


def feed_filters(
    *,
    status: str | None = None,
    country: str | None = None,
    location: str | None = None,
    seniority: str | None = None,
    source: str | None = None,
    is_remote: bool | None = None,
    has_salary: bool | None = None,
    include_duplicates: bool = False,
    min_score: int | None = None,
    pipeline_state: str | None = None,
    liveness: str | None = None,
    job_id: int | None = None,
    fit: bool | None = None,
    reason_type: str | None = None,
    date_posted: str | None = None,
    q: str | None = None,
) -> tuple[str, list[Any]]:
    """The shared WHERE clause for feed queries, as (sql, params)."""
    sql = ""
    params: list[Any] = []

    if job_id is not None:
        sql += " AND jobs.id = ?"
        params.append(job_id)
    if not include_duplicates:
        sql += " AND duplicate_of IS NULL"
    for column, value in (
        ("jobs.status", status),
        ("jobs.country", country),
        ("jobs.seniority", seniority),
        ("jobs.source", source),
        ("jobs.pipeline_state", pipeline_state),
        (LIVENESS_CASE, liveness),
    ):
        if value:
            sql += f" AND {column} = ?"
            params.append(value)
    if location and location.strip():
        # Dashboard locations are configured search targets.  A posting's reported
        # location is free text and cannot reliably represent the radius used to find
        # it; scrape-cell provenance can.
        sql += " AND cell.location_id = ?"
        params.append(location.strip())
    if fit is not None:
        sql += " AND v.fit = ?"
        params.append(1 if fit else 0)
    if reason_type:
        sql += " AND v.reason_type = ?"
        params.append(reason_type.strip().lower())
    if date_posted and date_posted in DATE_POSTED_WINDOWS:
        hours = DATE_POSTED_WINDOWS[date_posted] * 24
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        sql += " AND COALESCE(jobs.date_posted, jobs.date_found) >= ?"
        params.append(cutoff)
    if is_remote is not None:
        sql += " AND jobs.is_remote = ?"
        params.append(1 if is_remote else 0)
    if has_salary is not None:
        sql += (
            " AND jobs.salary_annual_usd IS NOT NULL"
            if has_salary
            else " AND jobs.salary_annual_usd IS NULL"
        )
    if min_score is not None:
        sql += " AND jobs.match_score >= ?"
        params.append(min_score)
    if q and q.strip():
        sql += " AND jobs.id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)"
        params.append(fts_match_query(q.strip()))
    return sql, params


def query_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    country: str | None = None,
    location: str | None = None,
    seniority: str | None = None,
    source: str | None = None,
    is_remote: bool | None = None,
    has_salary: bool | None = None,
    include_duplicates: bool = False,
    min_score: int | None = None,
    pipeline_state: str | None = None,
    liveness: str | None = None,
    job_id: int | None = None,
    fit: bool | None = None,
    reason_type: str | None = None,
    date_posted: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    detail: bool = False,
    cached_columns: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Filtered, paginated posting list for the dashboard."""
    if job_id is not None:
        detail = True

    where, params = feed_filters(
        status=status,
        country=country,
        location=location,
        seniority=seniority,
        source=source,
        is_remote=is_remote,
        has_salary=has_salary,
        include_duplicates=include_duplicates,
        min_score=min_score,
        pipeline_state=pipeline_state,
        liveness=liveness,
        job_id=job_id,
        fit=fit,
        reason_type=reason_type,
        date_posted=date_posted,
        q=q,
    )
    cols_sql, new_cached_columns = select_columns(conn, detail, cached_columns)
    if job_id is not None:
        # Detail URLs always address a concrete posting, not its feed group.
        query = f"SELECT {cols_sql}, COUNT(*) OVER () AS _total{FEED_FROM} WHERE 1=1{where}"
    else:
        # Keep every underlying posting in the database, but return only the highest-ranked
        # representative of each normalized company + title family.  Filters run before
        # grouping, so a source/location filter never leaks hidden variants into its count.
        query = f"""
            WITH grouped AS (
                SELECT jobs.id AS job_id,
                       COUNT(*) OVER (PARTITION BY {_GROUP_PARTITION_BY}) AS listing_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY {_GROUP_PARTITION_BY}
                           ORDER BY {FEED_ORDER_BY}
                       ) AS listing_rank
                {FEED_FROM}
                WHERE 1=1{where}
            )
            SELECT {cols_sql}, grouped.listing_count, COUNT(*) OVER () AS _total
            FROM grouped
            JOIN jobs ON jobs.id = grouped.job_id
            LEFT JOIN job_verdicts v
                   ON v.job_id = jobs.id
                  AND v.profile_version = ({ACTIVE_PROFILE_VERSION})
            LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id
            WHERE grouped.listing_rank = 1
        """

    query += f" ORDER BY {FEED_ORDER_BY}"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    jobs: list[dict[str, Any]] = []
    for row in conn.execute(query, params):
        job = dict(row)
        total = job.pop("_total")
        for field in JSON_COLUMNS:
            if field in job:
                job[field] = json.loads(job[field]) if job[field] else []
        jobs.append(job)

    if not jobs:
        total = 0

    result = {
        "jobs": jobs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(jobs) < total,
    }
    return result, new_cached_columns


def job_ids_for(
    conn: sqlite3.Connection,
    db_path: str,
    status: str | None = None,
    country: str | None = None,
    location: str | None = None,
    seniority: str | None = None,
    source: str | None = None,
    is_remote: bool | None = None,
    has_salary: bool | None = None,
    include_duplicates: bool = False,
    min_score: int | None = None,
    pipeline_state: str | None = None,
    liveness: str | None = None,
    job_id: int | None = None,
    fit: bool | None = None,
    reason_type: str | None = None,
    date_posted: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    detail: bool | None = None,  # noqa: ARG001
) -> list[int]:
    """Just the IDs on this page of the feed, in feed order."""
    where, params = feed_filters(
        status=status,
        country=country,
        location=location,
        seniority=seniority,
        source=source,
        is_remote=is_remote,
        has_salary=has_salary,
        include_duplicates=include_duplicates,
        min_score=min_score,
        pipeline_state=pipeline_state,
        liveness=liveness,
        job_id=job_id,
        fit=fit,
        reason_type=reason_type,
        date_posted=date_posted,
        q=q,
    )
    if job_id is not None:
        query = f"SELECT jobs.id{FEED_FROM} WHERE 1=1{where}"
    else:
        query = f"""
            WITH grouped AS (
                SELECT jobs.id AS job_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY {_GROUP_PARTITION_BY}
                           ORDER BY {FEED_ORDER_BY}
                       ) AS listing_rank
                {FEED_FROM}
                WHERE 1=1{where}
            )
            SELECT jobs.id
            FROM grouped
            JOIN jobs ON jobs.id = grouped.job_id
            LEFT JOIN job_verdicts v
                   ON v.job_id = jobs.id
                  AND v.profile_version = ({ACTIVE_PROFILE_VERSION})
            LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id
            WHERE grouped.listing_rank = 1
        """
    query += f" ORDER BY {FEED_ORDER_BY} LIMIT ? OFFSET ?"
    args: list[Any] = [*params, limit, offset]

    key = (
        db_path,
        query,
        tuple(args),
        conn.execute("PRAGMA data_version").fetchone()[0],
        _WRITE_GENERATION,
    )
    cached = _FEED_IDS_CACHE.get(key)
    if cached is not None:
        return list(cached)

    ids = [row[0] for row in conn.execute(query, args)]
    if len(_FEED_IDS_CACHE) > 32:
        _FEED_IDS_CACHE.clear()
    _FEED_IDS_CACHE[key] = ids
    return list(ids)


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> bool:
    cursor = conn.cursor()
    now_str = datetime.now().isoformat() if status == "applied" else None

    if status == "applied":
        cursor.execute(
            "UPDATE jobs SET status = ?, date_applied = ? WHERE id = ?",
            (status, now_str, job_id),
        )
    else:
        cursor.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    invalidate_stats()
    return cursor.rowcount > 0


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Postings per status, zero-filled."""
    counts = {
        row["status"]: row["count"]
        for row in conn.execute("SELECT status, COUNT(*) as count FROM jobs GROUP BY status")
    }
    for status in ("unread", "saved", "applied", "rejected"):
        counts.setdefault(status, 0)
    return counts


def compute_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    stats: dict[str, Any] = {}

    stats["status_counts"] = status_counts(conn)

    cursor.execute("SELECT COUNT(*) FROM jobs")
    stats["total_jobs"] = cursor.fetchone()[0]

    cursor.execute(
        "SELECT SUM(matched_count), SUM(required_count) FROM jobs WHERE required_count > 0"
    )
    matched, required = cursor.fetchone()
    stats["skill_coverage"] = {
        "matched": matched or 0,
        "required": required or 0,
        "ratio": round(matched / required, 3) if required else None,
    }

    cursor.execute("SELECT country, COUNT(*) as count FROM jobs GROUP BY country")
    stats["country_counts"] = {r["country"]: r["count"] for r in cursor.fetchall() if r["country"]}

    cursor.execute("SELECT pipeline_state, COUNT(*) as count FROM jobs GROUP BY pipeline_state")
    stats["pipeline_counts"] = {r["pipeline_state"]: r["count"] for r in cursor.fetchall()}

    cursor.execute(
        f"""
        SELECT COUNT(*) FROM jobs j
          JOIN job_verdicts v ON v.job_id = j.id
           AND (
               v.profile_version = ({ACTIVE_PROFILE_VERSION})
               OR ({ACTIVE_PROFILE_VERSION}) IS NULL
           )
         WHERE j.duplicate_of IS NULL
           AND (v.fit = 1 OR v.fit IS TRUE)
        """
    )
    stats["strong_matches"] = cursor.fetchone()[0] or 0

    cursor.execute(
        f"SELECT {LIVENESS_CASE} AS liveness, COUNT(*)"
        f"  FROM jobs LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id"
        f" GROUP BY 1"
    )
    stats["liveness_counts"] = {r[0]: r[1] for r in cursor.fetchall()}

    return stats


def get_stats(conn: sqlite3.Connection, db_path: str) -> dict[str, Any]:
    """Dashboard counters, memoized between writes."""
    key = (
        conn.execute("PRAGMA data_version").fetchone()[0],
        _WRITE_GENERATION,
    )
    cached = _STATS_CACHE.get(db_path)
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1])
    stats = compute_stats(conn)
    _STATS_CACHE[db_path] = (key, stats)
    return copy.deepcopy(stats)
