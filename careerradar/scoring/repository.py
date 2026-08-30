"""Scoring repository: backlog queries, verdict persistence, and observability analytics."""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from careerradar.core.config import load_config
from careerradar.profile.models import DEFAULT_PROFILE_VERSION, VERDICT_SCHEMA_VERSION
from careerradar.taxonomy.roles import SENIORITY_UNSPECIFIED

MAX_SCORING_FAILURES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seniority_clause() -> str:
    import sys

    worker = sys.modules.get("careerradar.scoring.worker")
    cfg_fn = getattr(worker, "load_config", load_config) if worker else load_config
    levels = (cfg_fn().get("scoring") or {}).get("skip_seniority") or []
    if not levels:
        return ""
    quoted = ", ".join("'" + level.replace("'", "''") + "'" for level in levels)
    return f"AND COALESCE(j.seniority, '{SENIORITY_UNSPECIFIED}') NOT IN ({quoted})"


def _age_clause() -> str:
    import sys

    worker = sys.modules.get("careerradar.scoring.worker")
    cfg_fn = getattr(worker, "load_config", load_config) if worker else load_config
    max_days = (cfg_fn().get("scoring") or {}).get("max_posting_age_days")
    if not max_days:
        return ""
    max_seconds = int(max_days * 86400)
    return (
        f"AND (COALESCE(j.date_posted, j.date_found) IS NULL "
        f"OR unixepoch('now') - unixepoch(COALESCE(j.date_posted, j.date_found)) <= {max_seconds})"
    )


_ELIGIBLE = """
      FROM jobs j
      LEFT JOIN v_job_liveness l ON l.job_id = j.id
     WHERE j.duplicate_of IS NULL
       AND j.description IS NOT NULL AND length(j.description) > 200
       AND COALESCE(j.scoring_failures, 0) < {max_failures}
       {live_clause}
       {seniority_clause}
       {age_clause}
"""

_NO_VERDICT = """
       AND NOT EXISTS (
             SELECT 1 FROM job_verdicts v
              WHERE v.job_id = j.id AND v.profile_version = ?
                AND COALESCE(v.verdict_schema_version, 1) >= ?
            )
"""


def _live_clause(include_closed: bool) -> str:
    return "" if include_closed else "AND COALESCE(l.liveness, 'unknown') != 'likely_closed'"


def select_scoring_backlog(
    conn: sqlite3.Connection,
    profile_version: int,
    limit: int | None = None,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    """Postings needing a verdict under the active profile."""
    where = _ELIGIBLE.format(
        live_clause=_live_clause(include_closed),
        max_failures=MAX_SCORING_FAILURES,
        seniority_clause=_seniority_clause(),
        age_clause=_age_clause(),
    )

    query = "SELECT j.*" + where + _NO_VERDICT + " ORDER BY j.date_found DESC"
    params: list[Any] = [profile_version, VERDICT_SCHEMA_VERSION]

    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params)]


def get_pending_scoring_stats(
    conn: sqlite3.Connection, profile_version: int, include_closed: bool = False
) -> tuple[int, str | None]:
    """How many postings are pending scoring and the date found of the oldest one."""
    sql = (
        "SELECT COUNT(*), MIN(j.date_found)"
        + _ELIGIBLE.format(
            live_clause=_live_clause(include_closed),
            max_failures=MAX_SCORING_FAILURES,
            seniority_clause=_seniority_clause(),
            age_clause=_age_clause(),
        )
        + _NO_VERDICT
    )
    row = conn.execute(sql, [profile_version, VERDICT_SCHEMA_VERSION]).fetchone()
    if row is None:
        return 0, None
    return row[0], row[1]


def count_pending_scoring(
    conn: sqlite3.Connection, profile_version: int, include_closed: bool = False
) -> int:
    """How many postings a next run would select."""
    sql = (
        "SELECT COUNT(*)"
        + _ELIGIBLE.format(
            live_clause=_live_clause(include_closed),
            max_failures=MAX_SCORING_FAILURES,
            seniority_clause=_seniority_clause(),
            age_clause=_age_clause(),
        )
        + _NO_VERDICT
    )
    return conn.execute(sql, [profile_version, VERDICT_SCHEMA_VERSION]).fetchone()[0]


def get_oldest_pending_date(
    conn: sqlite3.Connection, profile_version: int, include_closed: bool = False
) -> str | None:
    """Date found of the oldest eligible posting waiting to be scored."""
    _, oldest = get_pending_scoring_stats(
        conn, profile_version=profile_version, include_closed=include_closed
    )
    return oldest


def get_ineligible_breakdown(conn: sqlite3.Connection) -> sqlite3.Row:
    """Postings left in 'new' that no run will ever select, by reason."""
    seniority = _seniority_clause()
    seniority_hit = f"NOT ({seniority[4:]})" if seniority else "0"
    return conn.execute(f"""
        SELECT
          COUNT(*) AS total,
          SUM(j.duplicate_of IS NOT NULL) AS duplicate,
          SUM(j.duplicate_of IS NULL
              AND (j.description IS NULL OR length(j.description) <= 200)) AS thin,
          SUM(j.duplicate_of IS NULL
              AND j.description IS NOT NULL AND length(j.description) > 200
              AND COALESCE(l.liveness, 'unknown') = 'likely_closed') AS closed,
          SUM(j.duplicate_of IS NULL
              AND j.description IS NOT NULL AND length(j.description) > 200
              AND COALESCE(l.liveness, 'unknown') != 'likely_closed'
              AND {seniority_hit}) AS seniority
          FROM jobs j
          LEFT JOIN v_job_liveness l ON l.job_id = j.id
         WHERE j.pipeline_state = 'new'
           AND (j.duplicate_of IS NOT NULL
                OR j.description IS NULL OR length(j.description) <= 200
                OR COALESCE(l.liveness, 'unknown') = 'likely_closed'
                OR {seniority_hit})
    """).fetchone()


def record_scoring_failure(
    conn: sqlite3.Connection, job_id: int, error: str | BaseException | None
) -> None:
    """Count a failed run against the posting, with the reason that failed it."""
    conn.execute(
        "UPDATE jobs SET scoring_failures = COALESCE(scoring_failures, 0) + 1, "
        "last_scoring_error = ?, last_scoring_failure_at = ? WHERE id = ?",
        (str(error)[:500] if error else None, _now(), job_id),
    )


def get_quarantined_jobs(
    conn: sqlite3.Connection, max_failures: int = MAX_SCORING_FAILURES
) -> list[sqlite3.Row]:
    """Postings withdrawn from the queue after repeated failures."""
    return conn.execute(
        "SELECT COUNT(*) AS total, "
        "       MIN(id) AS example, "
        "       COALESCE(substr(last_scoring_error, 1, 60), '(unrecorded)') AS reason "
        "  FROM jobs WHERE COALESCE(scoring_failures, 0) >= ? "
        " GROUP BY reason ORDER BY total DESC",
        (max_failures,),
    ).fetchall()


def reset_scoring_failures(conn: sqlite3.Connection, job_id: int | None = None) -> int:
    """Clear the failure counter so quarantined postings are offered again."""
    cursor = conn.cursor()
    if job_id is not None:
        cursor.execute(
            "UPDATE jobs SET scoring_failures = 0, last_scoring_error = NULL WHERE id = ?",
            (job_id,),
        )
    else:
        cursor.execute(
            "UPDATE jobs SET scoring_failures = 0, last_scoring_error = NULL "
            "WHERE COALESCE(scoring_failures, 0) > 0"
        )
    conn.commit()
    return cursor.rowcount


def save_verdict(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    verdict: dict[str, Any],
    usage: dict[str, int] | None,
    cost: float,
    model: str,
    profile_version: int,
    phash: str,
) -> None:
    """Persist scoring verdict and update job state."""
    fit_val = 1 if verdict.get("fit") else 0
    reason_type = verdict.get("reason_type") or "unknown"
    reason_desc = verdict.get("reason_description") or ""

    conn.execute(
        """
        INSERT INTO job_verdicts
            (job_id, profile_version, model, fit, reason_type, reason_description,
             tokens_in, tokens_cached, tokens_out, cost_usd, created_at,
             verdict_schema_version, prompt_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, profile_version) DO UPDATE SET
            model = excluded.model,
            fit = excluded.fit,
            reason_type = excluded.reason_type,
            reason_description = excluded.reason_description,
            tokens_in = excluded.tokens_in,
            tokens_cached = excluded.tokens_cached,
            tokens_out = excluded.tokens_out,
            cost_usd = excluded.cost_usd,
            created_at = excluded.created_at,
            verdict_schema_version = excluded.verdict_schema_version,
            prompt_hash = excluded.prompt_hash
        """,
        (
            job["id"],
            profile_version,
            model,
            fit_val,
            reason_type,
            reason_desc,
            usage["prompt"] if usage else None,
            usage["cache_hit"] if usage else None,
            usage["completion"] if usage else None,
            cost,
            _now(),
            VERDICT_SCHEMA_VERSION,
            phash,
        ),
    )
    conn.execute(
        "UPDATE jobs SET scored_at = ?, pipeline_state = 'scored', "
        "scoring_failures = 0, last_scoring_error = NULL WHERE id = ?",
        (_now(), job["id"]),
    )
    if job.get("scrape_cell_id"):
        from careerradar.search.repository import update_cell_quality

        update_cell_quality(conn, job["scrape_cell_id"], 100.0 if verdict.get("fit") else 0.0)


# =====================================================================================
# Observability and Distribution Analytics
# =====================================================================================


def get_verdict_distribution_stats(
    conn: sqlite3.Connection, profile_version: int | None = None
) -> dict[str, Any] | None:
    """Distribution observability for stored verdicts."""
    if profile_version is None:
        profile_version = DEFAULT_PROFILE_VERSION

    query = "SELECT fit, reason_type, cost_usd FROM job_verdicts WHERE profile_version = ?"
    params: list[Any] = [profile_version]
    rows = conn.execute(query, params).fetchall()
    if not rows:
        return {"profile_version": profile_version, "total": 0}

    fit_count = 0
    no_fit_count = 0
    unscored_fit_count = 0
    reasons: dict[str, int] = {}
    cost = 0.0

    for row in rows:
        fit_val = row["fit"]
        if fit_val == 1:
            fit_count += 1
        elif fit_val == 0:
            no_fit_count += 1
        else:
            unscored_fit_count += 1

        rtype = (row["reason_type"] or "unspecified").strip().lower()
        reasons[rtype] = reasons.get(rtype, 0) + 1
        cost += row["cost_usd"] or 0.0

    return {
        "profile_version": profile_version,
        "total": len(rows),
        "fit_count": fit_count,
        "no_fit_count": no_fit_count,
        "unscored_fit_count": unscored_fit_count,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "cost": cost,
    }


def get_observability_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate token counts, costs, and reason distributions across all scored verdicts."""
    cursor = conn.cursor()
    has_table = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_verdicts'"
    ).fetchone()
    if not has_table:
        return {
            "total_verdicts": 0,
            "total_tokens_in": 0,
            "total_tokens_cached": 0,
            "total_tokens_out": 0,
            "cache_hit_rate": 0.0,
            "avg_tokens_out": 0.0,
            "min_tokens_out": 0,
            "max_tokens_out": 0,
            "avg_tokens_in": 0.0,
            "total_cost_usd": 0.0,
            "avg_cost_usd": 0.0,
            "fit_count": 0,
            "no_fit_count": 0,
            "fit_rate": 0.0,
            "reasons": [],
            "models": [],
        }

    stats_sql = """
        SELECT
            COUNT(*) AS total_verdicts,
            COALESCE(SUM(v.tokens_in), 0) AS total_tokens_in,
            COALESCE(SUM(v.tokens_cached), 0) AS total_tokens_cached,
            COALESCE(SUM(v.tokens_out), 0) AS total_tokens_out,
            COALESCE(ROUND(AVG(v.tokens_out), 1), 0.0) AS avg_tokens_out,
            COALESCE(MIN(v.tokens_out), 0) AS min_tokens_out,
            COALESCE(MAX(v.tokens_out), 0) AS max_tokens_out,
            COALESCE(ROUND(AVG(v.tokens_in), 1), 0.0) AS avg_tokens_in,
            COALESCE(SUM(v.cost_usd), 0.0) AS total_cost_usd,
            COALESCE(ROUND(AVG(v.cost_usd), 6), 0.0) AS avg_cost_usd,
            SUM(CASE WHEN v.fit = 1 THEN 1 ELSE 0 END) AS fit_count,
            SUM(CASE WHEN v.fit = 0 THEN 1 ELSE 0 END) AS no_fit_count
        FROM job_verdicts v
    """
    row = cursor.execute(stats_sql).fetchone()
    total_verdicts = row["total_verdicts"] or 0
    total_in = row["total_tokens_in"] or 0
    total_cached = row["total_tokens_cached"] or 0
    cache_hit_rate = round(total_cached / total_in, 4) if total_in > 0 else 0.0
    fit_count = row["fit_count"] or 0
    no_fit_count = row["no_fit_count"] or 0
    fit_rate = round(fit_count / total_verdicts, 4) if total_verdicts > 0 else 0.0

    reasons_sql = """
        SELECT
            COALESCE(v.reason_type, 'unspecified') AS reason_type,
            COUNT(*) AS count,
            COALESCE(ROUND(AVG(v.tokens_out), 1), 0.0) AS avg_tokens_out
        FROM job_verdicts v
        GROUP BY COALESCE(v.reason_type, 'unspecified')
        ORDER BY count DESC
    """
    reasons = []
    for r in cursor.execute(reasons_sql).fetchall():
        rtype = r["reason_type"] or "unspecified"
        rcnt = r["count"]
        reasons.append(
            {
                "reason_type": rtype,
                "count": rcnt,
                "avg_tokens_out": r["avg_tokens_out"],
                "percentage": round(rcnt / total_verdicts, 4) if total_verdicts > 0 else 0.0,
            }
        )

    models_sql = """
        SELECT
            COALESCE(v.model, 'unknown') AS model,
            COUNT(*) AS count,
            COALESCE(SUM(v.tokens_out), 0) AS total_tokens_out,
            COALESCE(ROUND(AVG(v.tokens_out), 1), 0.0) AS avg_tokens_out,
            COALESCE(SUM(v.cost_usd), 0.0) AS total_cost_usd
        FROM job_verdicts v
        GROUP BY COALESCE(v.model, 'unknown')
        ORDER BY count DESC
    """
    models = [
        {
            "model": m["model"],
            "count": m["count"],
            "total_tokens_out": m["total_tokens_out"],
            "avg_tokens_out": m["avg_tokens_out"],
            "total_cost_usd": round(m["total_cost_usd"], 6),
        }
        for m in cursor.execute(models_sql).fetchall()
    ]

    return {
        "total_verdicts": total_verdicts,
        "total_tokens_in": total_in,
        "total_tokens_cached": total_cached,
        "total_tokens_out": row["total_tokens_out"] or 0,
        "cache_hit_rate": cache_hit_rate,
        "avg_tokens_out": row["avg_tokens_out"] or 0.0,
        "min_tokens_out": row["min_tokens_out"] or 0,
        "max_tokens_out": row["max_tokens_out"] or 0,
        "avg_tokens_in": row["avg_tokens_in"] or 0.0,
        "total_cost_usd": round(row["total_cost_usd"] or 0.0, 4),
        "avg_cost_usd": row["avg_cost_usd"] or 0.0,
        "fit_count": fit_count,
        "no_fit_count": no_fit_count,
        "fit_rate": fit_rate,
        "reasons": reasons,
        "models": models,
    }


def query_observability_verdicts(
    conn: sqlite3.Connection,
    q: str | None = None,
    fit: str | None = None,
    reason_type: str | None = None,
    model: str | None = None,
    sort: str = "tokens_out_desc",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Query verdicts with token usage details and search filters."""
    cursor = conn.cursor()
    has_table = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_verdicts'"
    ).fetchone()
    if not has_table:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    cols = {row[1] for row in cursor.execute("PRAGMA table_info(job_verdicts)").fetchall()}
    has_fit = "fit" in cols
    has_reason_type = "reason_type" in cols
    has_reason_desc = "reason_description" in cols
    has_verdict = "verdict" in cols
    has_reasoning = "reasoning" in cols

    fit_expr = (
        "v.fit"
        if has_fit
        else (
            "(CASE WHEN v.verdict IN ('strong', 'worth_applying') "
            "OR (v.fit_score IS NOT NULL AND v.fit_score >= 60) THEN 1 ELSE 0 END)"
            if has_verdict
            else "0"
        )
    )

    reason_expr = (
        "COALESCE(v.reason_type, v.verdict, 'unspecified')"
        if (has_reason_type and has_verdict)
        else (
            "COALESCE(v.reason_type, 'unspecified')"
            if has_reason_type
            else ("COALESCE(v.verdict, 'unspecified')" if has_verdict else "'unspecified'")
        )
    )

    desc_expr = (
        "COALESCE(v.reason_description, v.reasoning, '')"
        if (has_reason_desc and has_reasoning)
        else (
            "COALESCE(v.reason_description, '')"
            if has_reason_desc
            else ("COALESCE(v.reasoning, '')" if has_reasoning else "''")
        )
    )

    where_clauses: list[str] = ["1=1"]
    params: list[Any] = []

    if q and q.strip():
        term = f"%{q.strip()}%"
        where_clauses.append(f"(j.title LIKE ? OR j.company LIKE ? OR {desc_expr} LIKE ?)")
        params.extend([term, term, term])

    if fit in ("true", "1", "fit"):
        where_clauses.append(f"({fit_expr} = 1)")
    elif fit in ("false", "0", "no_fit"):
        where_clauses.append(f"({fit_expr} = 0)")

    if reason_type and reason_type.lower() not in ("all", ""):
        where_clauses.append(f"LOWER({reason_expr}) = LOWER(?)")
        params.append(reason_type.strip())

    if model and model.lower() not in ("all", ""):
        where_clauses.append("COALESCE(v.model, 'unknown') = ?")
        params.append(model.strip())

    where_sql = " AND ".join(where_clauses)

    count_sql = f"""
        SELECT COUNT(*)
        FROM job_verdicts v
        JOIN jobs j ON j.id = v.job_id
        WHERE {where_sql}
    """
    total = cursor.execute(count_sql, params).fetchone()[0]

    if sort == "tokens_out_desc":
        order_by = "v.tokens_out DESC NULLS LAST, v.created_at DESC"
    elif sort == "tokens_out_asc":
        order_by = "v.tokens_out ASC NULLS LAST, v.created_at DESC"
    elif sort == "cost_desc":
        order_by = "v.cost_usd DESC NULLS LAST, v.created_at DESC"
    elif sort == "date_asc":
        order_by = "v.created_at ASC"
    else:
        order_by = "v.created_at DESC"

    query_sql = f"""
        SELECT
            v.id,
            v.job_id,
            j.title,
            j.company,
            j.location,
            j.url,
            {fit_expr} AS fit,
            {reason_expr} AS reason_type,
            {desc_expr} AS reason_description,
            COALESCE(v.tokens_in, 0) AS tokens_in,
            COALESCE(v.tokens_cached, 0) AS tokens_cached,
            COALESCE(v.tokens_out, 0) AS tokens_out,
            COALESCE(v.cost_usd, 0.0) AS cost_usd,
            COALESCE(v.model, 'unknown') AS model,
            COALESCE(v.created_at, '') AS created_at
        FROM job_verdicts v
        JOIN jobs j ON j.id = v.job_id
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    query_params = [*list(params), limit, offset]
    rows = cursor.execute(query_sql, query_params).fetchall()

    items = [
        {
            "id": r["id"],
            "job_id": r["job_id"],
            "title": r["title"] or "Untitled Role",
            "company": r["company"] or "Unknown Company",
            "location": r["location"],
            "url": r["url"] or "",
            "fit": bool(r["fit"]) if r["fit"] is not None else False,
            "reason_type": r["reason_type"] or "unknown",
            "reason_description": r["reason_description"] or "",
            "tokens_in": r["tokens_in"],
            "tokens_cached": r["tokens_cached"],
            "tokens_out": r["tokens_out"],
            "cost_usd": round(r["cost_usd"], 6),
            "model": r["model"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def count_recent_verdicts(conn: sqlite3.Connection, minutes: int = 5) -> int:
    """Count verdicts produced in the last N minutes."""
    return conn.execute(
        "SELECT COUNT(*) FROM job_verdicts WHERE unixepoch(created_at) >= unixepoch('now', ?)",
        (f"-{minutes} minutes",),
    ).fetchone()[0]
