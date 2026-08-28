"""Market repository: analytics observations, postings by family, and gap analysis."""

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


def get_cell_observations_in_window(
    conn: sqlite3.Connection,
    window_start: str,
    location_id: str | None = None,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Retrieve successful/empty cell observations within the time window."""
    query = """
        SELECT role_family, location_id, source, hours_old, requested, returned,
               returned_on_topic, saturated, window_start, window_end, status
          FROM cell_observations
         WHERE status IN ('ok', 'empty')
           AND observed_at >= ?
           AND (? IS NULL OR location_id = ?)
           AND (? IS NULL OR source = ?)
    """
    return conn.execute(
        query,
        (window_start, location_id, location_id, source, source),
    ).fetchall()


def get_postings_by_family(
    conn: sqlite3.Connection,
    eligibility_sql: str,
    window_start: str,
    location_id: str | None = None,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Retrieve postings grouped by role family within the time window."""
    where_conj = " AND " if " WHERE " in eligibility_sql.upper() else " WHERE "
    query = eligibility_sql + where_conj + "date_found >= ?"
    params: list[Any] = [window_start]
    if source:
        query += " AND source = ?"
        params.append(source)
    if location_id:
        query += " AND scrape_cell_id IN (SELECT id FROM scrape_cells WHERE location_id = ?)"
        params.append(location_id)

    return conn.execute(query, params).fetchall()


def get_coverage_report_cells(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Retrieve per-cell health and scrape statistics."""
    rows = conn.execute(
        """
        SELECT source, location_id, role_family, tier, enabled, query,
               last_scraped_at, last_success_at, last_result_count,
               last_saturated, consecutive_empty, consecutive_error,
               total_scrapes, backoff_until,
               ewma_new_per_scrape, ewma_fit_score, quality_samples
          FROM scrape_cells
         ORDER BY source, location_id, role_family
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
        out.append(record)
    return out


# =====================================================================================
# Gap Analysis queries
# =====================================================================================


def load_gap_analysis_corpus(
    conn: sqlite3.Connection,
    eligibility_sql: str,
    window_start: str,
    location_id: str | None = None,
    role_family: str | None = None,
    exclude_agencies: bool = True,
) -> list[dict[str, Any]]:
    """Load postings for gap analysis matching filters."""
    where_conj = " AND " if " WHERE " in eligibility_sql.upper() else " WHERE "
    query = eligibility_sql + where_conj + "date_found >= ?"
    params: list[Any] = [window_start]
    if exclude_agencies:
        query += " AND COALESCE(is_agency, 0) = 0"
    if location_id:
        query += " AND scrape_cell_id IN (SELECT id FROM scrape_cells WHERE location_id = ?)"
        params.append(location_id)
    if role_family:
        query += " AND role_family = ?"
        params.append(role_family)

    return [dict(row) for row in conn.execute(query, params)]


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


def get_cell_locations(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        row["id"]: row["location_id"]
        for row in conn.execute("SELECT id, location_id FROM scrape_cells")
    }


def get_skill_drilldown_postings(
    conn: sqlite3.Connection, skill: str, window_start: str, limit: int = 40
) -> list[dict[str, Any]]:
    """Postings requiring a specific skill for audit and drilldown."""
    query = """
        SELECT j.id, j.title, j.company, j.location, j.url, j.match_score,
               j.role_family, j.seniority, j.salary_annual_usd, j.date_posted,
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


def get_skill_by_family(
    conn: sqlite3.Connection, skill: str, window_start: str
) -> list[sqlite3.Row]:
    """Breakdown of skill demand by role family."""
    query = """
        SELECT j.role_family, COUNT(*) n
          FROM job_skills js JOIN v_skill_eligible j ON j.id = js.job_id
         WHERE js.skill = ? AND j.date_found >= ?
         GROUP BY j.role_family ORDER BY n DESC
    """
    return conn.execute(query, (skill, window_start)).fetchall()
