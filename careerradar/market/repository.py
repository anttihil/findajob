"""Market repository: analytics observations, postings by query, and gap analysis."""

import sqlite3
from datetime import datetime, timezone
from typing import Any


def _parse_date(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# =====================================================================================
# Market Analytics queries
# =====================================================================================


def get_coverage_report_cells(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Retrieve per-cell health and scrape statistics."""
    rows = conn.execute(
        """
        SELECT source, location_id, query, enabled,
               last_scraped_at, last_success_at, last_result_count,
               last_saturated, consecutive_empty, consecutive_error,
               total_scrapes, backoff_until,
               last_new_count, quality_fits, quality_samples
          FROM scrape_cells
         ORDER BY source, location_id, query
        """
    ).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        record = dict(row)
        success = _parse_date(row["last_success_at"])
        record["hours_since_success"] = (
            round((now - success).total_seconds() / 3600, 1) if success else None
        )
        # The signal for curating the query list: which cells actually return fits.
        samples = row["quality_samples"] or 0
        record["fit_rate"] = round(row["quality_fits"] / samples, 4) if samples else None
        out.append(record)
    return out


def get_query_yield_cells(
    conn: sqlite3.Connection,
    window_start: str | None = None,
    source: str | None = None,
    location_id: str | None = None,
    query: str | None = None,
    min_postings: int = 0,
) -> list[dict[str, Any]]:
    """Retrieve posting counts and strong fit verdicts for each
    (source, query, location) search cell.
    """
    sql = """
        SELECT
            sc.id AS cell_id,
            sc.source,
            sc.query,
            sc.location_id,
            sc.enabled,
            sq.id AS search_query_id,
            COUNT(DISTINCT j.id) AS total_postings,
            COUNT(DISTINCT CASE WHEN j.duplicate_of IS NULL THEN j.id END) AS unique_postings,
            COUNT(DISTINCT v.job_id) AS scored_postings,
            COUNT(DISTINCT CASE WHEN v.fit = 1 THEN v.job_id END) AS strong_fits,
            COUNT(DISTINCT CASE WHEN v.fit = 0 THEN v.job_id END) AS no_fits,
            sc.total_scrapes,
            sc.last_scraped_at,
            sc.last_success_at
        FROM scrape_cells sc
        -- `query` is the unique key of search_queries, so this recovers the row the
        -- market page's "disable this query" action needs to target.
        LEFT JOIN search_queries sq ON sq.query = sc.query
        LEFT JOIN jobs j ON j.scrape_cell_id = sc.id
             AND (? IS NULL OR j.date_found >= ?)
        LEFT JOIN job_verdicts v ON v.job_id = j.id
        WHERE (? IS NULL OR sc.source = ?)
          AND (? IS NULL OR sc.location_id = ?)
          AND (? IS NULL OR sc.query = ?)
        GROUP BY sc.id, sc.source, sc.query, sc.location_id
        HAVING COUNT(DISTINCT j.id) >= ? OR sc.total_scrapes > 0
        ORDER BY strong_fits DESC, total_postings DESC, sc.query ASC
    """
    params: list[Any] = [
        window_start,
        window_start,
        source,
        source,
        location_id,
        location_id,
        query,
        query,
        min_postings,
    ]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# =====================================================================================
# Gap Analysis queries
# =====================================================================================


def load_gap_analysis_corpus(
    conn: sqlite3.Connection,
    eligibility_sql: str,
    window_start: str,
    location_id: str | None = None,
    query: str | None = None,
    exclude_agencies: bool = True,
) -> list[dict[str, Any]]:
    """Load postings for gap analysis matching filters."""
    where_conj = " AND " if " WHERE " in eligibility_sql.upper() else " WHERE "
    sql = eligibility_sql + where_conj + "date_found >= ?"
    params: list[Any] = [window_start]
    if exclude_agencies:
        sql += " AND COALESCE(is_agency, 0) = 0"
    if location_id:
        sql += " AND scrape_cell_id IN (SELECT id FROM scrape_cells WHERE location_id = ?)"
        params.append(location_id)
    if query:
        sql += " AND scrape_cell_id IN (SELECT id FROM scrape_cells WHERE query = ?)"
        params.append(query)

    return [dict(row) for row in conn.execute(sql, params)]


def load_job_skills_chunked(
    conn: sqlite3.Connection, job_ids: list[int]
) -> dict[int, dict[str, bool]]:
    """Load job skills for a batch of jobs, chunked to stay under SQLite variable limits."""
    skills_by_job: dict[int, dict[str, bool]] = {}
    for start in range(0, len(job_ids), 500):
        chunk = job_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT job_id, skill, in_title FROM job_skills WHERE job_id IN ({placeholders})",
            chunk,
        ):
            skills_by_job.setdefault(row["job_id"], {})[row["skill"]] = bool(row["in_title"])
    return skills_by_job


def get_skill_drilldown_postings(
    conn: sqlite3.Connection, skill: str, window_start: str, limit: int = 40
) -> list[dict[str, Any]]:
    """Postings requiring a specific skill for audit and drilldown."""
    query = """
        SELECT j.id, j.title, j.company, j.location, j.url, j.match_score,
               j.seniority, j.salary_annual_usd, j.date_posted,
               js.in_title
          FROM v_skill_eligible j
          JOIN job_skills js ON js.job_id = j.id
         WHERE js.skill = ? AND j.date_found >= ?
         ORDER BY j.match_score DESC
         LIMIT ?
    """
    return [dict(r) for r in conn.execute(query, (skill, window_start, limit))]


def get_skill_cooccurring(
    conn: sqlite3.Connection, skill: str, window_start: str, limit: int = 15
) -> list[sqlite3.Row]:
    """Co-occurring skills for a target skill."""
    query = """
        SELECT other.skill, COUNT(*) n
          FROM job_skills js
          JOIN job_skills other ON other.job_id = js.job_id AND other.skill != js.skill
          JOIN v_skill_eligible j ON j.id = js.job_id
         WHERE js.skill = ? AND j.date_found >= ?
         GROUP BY other.skill
         ORDER BY n DESC
         LIMIT ?
    """
    return conn.execute(query, (skill, window_start, limit)).fetchall()


def get_skill_by_query(
    conn: sqlite3.Connection, skill: str, window_start: str
) -> list[sqlite3.Row]:
    """Breakdown of skill demand by the search query that found the posting."""
    sql = """
        SELECT sc.query, COUNT(*) n
          FROM job_skills js
          JOIN v_skill_eligible j ON j.id = js.job_id
          JOIN scrape_cells sc ON sc.id = j.scrape_cell_id
         WHERE js.skill = ? AND j.date_found >= ?
         GROUP BY sc.query ORDER BY n DESC
    """
    return conn.execute(sql, (skill, window_start)).fetchall()


def skill_exists(conn: sqlite3.Connection, skill: str) -> bool:
    """Return True if the skill has any occurrences in the job_skills table."""
    row = conn.execute("SELECT 1 FROM job_skills WHERE skill = ? LIMIT 1", (skill,)).fetchone()
    return row is not None
