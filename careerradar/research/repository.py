"""Research repository: candidate company queries, dossier persistence, runs, and openings."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from careerradar.search.normalizer import normalize_company

DEFAULT_RESEARCH_GATE: dict[str, Any] = {
    "fit": True,
}

_ACTIVE_VERDICT_JOIN = """
          job_verdicts v
                 ON v.job_id = j.id
                AND v.profile_version = (
                    SELECT version FROM profiles WHERE is_active = 1
                )
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gate_sql(gate: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if gate.get("fit") is not None:
        clauses.append("v.fit = ?")
        params.append(1 if gate.get("fit") else 0)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def get_research_candidates(
    conn: sqlite3.Connection,
    gate: dict[str, Any],
    refresh_days: int,
    limit: int | None,
    company: str | None = None,
) -> list[dict[str, Any]]:
    """Companies worth researching, best posting first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=refresh_days)).isoformat()

    params: list[Any]
    if company:
        where = "AND (j.company_normalized = ? OR j.company = ?)"
        params = [company, company]
    else:
        where, params = _gate_sql(gate)

    query = f"""
        SELECT j.company_normalized,
               MAX(COALESCE(v.fit, 0))         AS best_fit,
               COUNT(*)                        AS postings,
               MAX(j.company)                  AS company,
               MAX(j.id)                       AS job_id,
               MAX(j.role_family)              AS role_family,
               MAX(c.location_id)              AS location_id
          FROM jobs j
          JOIN job_verdicts v
                    ON v.job_id = j.id
                   AND v.profile_version = (
                       SELECT version FROM profiles WHERE is_active = 1
                   )
          LEFT JOIN scrape_cells c ON c.id = j.scrape_cell_id
          LEFT JOIN v_job_liveness l ON l.job_id = j.id
          LEFT JOIN company_dossiers d ON d.company_normalized = j.company_normalized
         WHERE j.duplicate_of IS NULL
           AND j.company_normalized IS NOT NULL
           AND COALESCE(l.liveness, 'unknown') != 'likely_closed'
           {where}
           AND (d.generated_at IS NULL OR d.generated_at < ?)
         GROUP BY j.company_normalized
         ORDER BY best_fit DESC, postings DESC
    """
    params.append(cutoff)
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(query, params)]


def save_dossier(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    dossier: dict[str, Any],
    model: str,
    profile_version: int,
    cost: float = 0.0,
) -> int:
    """Save or update a company dossier and update related jobs."""
    conn.execute(
        """
        INSERT INTO company_dossiers
            (company_normalized, company_display, generated_at, profile_version, model,
             intel_json, contacts_json, nearby_jobs_json, sources_json, cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_normalized) DO UPDATE SET
            company_display  = excluded.company_display,
            generated_at     = excluded.generated_at,
            profile_version  = excluded.profile_version,
            model            = excluded.model,
            intel_json       = excluded.intel_json,
            contacts_json    = excluded.contacts_json,
            nearby_jobs_json = excluded.nearby_jobs_json,
            sources_json     = excluded.sources_json,
            cost_usd         = excluded.cost_usd
        """,
        (
            row["company_normalized"],
            dossier.get("company") or row["company"],
            _now(),
            profile_version,
            model,
            json.dumps(
                {
                    **(dossier.get("intel") or {}),
                    "application_angle": dossier.get("application_angle"),
                }
            ),
            json.dumps(dossier.get("contacts") or []),
            json.dumps(dossier.get("nearby_jobs") or []),
            json.dumps(dossier.get("sources") or []),
            cost,
        ),
    )
    dossier_id = conn.execute(
        "SELECT id FROM company_dossiers WHERE company_normalized = ?",
        (row["company_normalized"],),
    ).fetchone()["id"]
    conn.execute(
        "UPDATE jobs SET dossier_id = ?, pipeline_state = 'researched' "
        "WHERE company_normalized = ? AND duplicate_of IS NULL",
        (dossier_id, row["company_normalized"]),
    )
    return dossier_id


def get_dossier_by_company(conn: sqlite3.Connection, company: str) -> dict[str, Any] | None:
    """Retrieve the parsed deep-research dossier for one company."""
    if not company:
        return None
    row = conn.execute(
        "SELECT * FROM company_dossiers WHERE company_normalized = ?",
        (normalize_company(company),),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM company_dossiers WHERE company_display = ?", (company,)
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    for field in ("intel_json", "contacts_json", "nearby_jobs_json", "sources_json"):
        record[field.removesuffix("_json")] = json.loads(record.pop(field) or "null")
    return record


def count_dossiers(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM company_dossiers").fetchone()[0]


def start_research_run(conn: sqlite3.Connection) -> int | None:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO research_runs (started_at, status) VALUES (?, 'running')",
        (_now(),),
    )
    conn.commit()
    return cursor.lastrowid


def finish_research_run(
    conn: sqlite3.Connection, run_id: int, companies_count: int, status: str = "ok"
) -> None:
    conn.execute(
        "UPDATE research_runs SET finished_at = ?, companies = ?, status = ? WHERE id = ?",
        (_now(), companies_count, status, run_id),
    )
    conn.commit()


def get_latest_research_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT started_at, status, companies FROM research_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_same_company_openings(
    conn: sqlite3.Connection,
    company_normalized: str,
    exclude_job_id: int | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Other postings from this company already in the corpus."""
    query = f"""
        SELECT j.id, j.title, j.company, j.location, j.url, v.fit, v.reason_type, j.role_family
          FROM jobs j
          LEFT JOIN {_ACTIVE_VERDICT_JOIN}
         WHERE j.company_normalized = ? AND j.duplicate_of IS NULL
    """
    params: list[Any] = [company_normalized]
    if exclude_job_id:
        query += " AND j.id != ?"
        params.append(exclude_job_id)
    query += " ORDER BY v.fit DESC NULLS LAST, j.date_found DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params)]


def get_nearby_company_openings(
    conn: sqlite3.Connection,
    role_family: str,
    location_id: str,
    exclude_company: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Strong postings for the same kind of role in the same market, elsewhere."""
    rows = conn.execute(
        f"""
        SELECT j.id, j.title, j.company, j.location, j.url, v.fit, v.reason_type
          FROM jobs j
          JOIN scrape_cells c ON c.id = j.scrape_cell_id
          JOIN {_ACTIVE_VERDICT_JOIN}
         WHERE j.role_family = ?
           AND c.location_id = ?
           AND (j.company_normalized IS NULL OR j.company_normalized != ?)
           AND j.duplicate_of IS NULL
         ORDER BY v.fit DESC NULLS LAST, j.date_found DESC
         LIMIT ?
        """,
        (role_family, location_id, exclude_company, limit),
    )
    return [dict(r) for r in rows]
