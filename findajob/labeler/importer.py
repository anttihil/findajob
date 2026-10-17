"""Read-only snapshot import and sampling from a CareerRadar production database."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from findajob.labeler.database import LabelerDatabase, utc_now

TARGET_REJECT_REASONS = ("skills", "tech_stack", "domain", "seniority")
GAP_PATTERN = re.compile(
    r"\b(gap|gaps|lack|lacks|missing|limited|adjacent|stretch|learn|learning|"
    r"not demonstrated|no evidence|without)\b",
    re.IGNORECASE,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _source_connection(path: str) -> sqlite3.Connection:
    resolved = str(Path(path).expanduser().resolve())
    if not Path(resolved).is_file():
        raise FileNotFoundError(f"Source database does not exist: {resolved}")
    uri = f"file:{quote(resolved)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if not _table_exists(conn, "jobs") or not _table_exists(conn, "job_verdicts"):
        conn.close()
        raise ValueError("Source must contain jobs and job_verdicts tables.")
    return conn


def _profile_snapshot(conn: sqlite3.Connection) -> tuple[int | None, dict[str, Any], str]:
    if _table_exists(conn, "profile"):
        row = conn.execute("SELECT * FROM profile ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return 1, {}, ""
        snapshot = dict(row)
        return 1, snapshot, str(snapshot.get("summary_text") or "")
    if _table_exists(conn, "profiles"):
        columns = _columns(conn, "profiles")
        order = "is_active DESC, version DESC" if "is_active" in columns else "version DESC"
        row = conn.execute(f"SELECT * FROM profiles ORDER BY {order} LIMIT 1").fetchone()
        if row is None:
            return None, {}, ""
        snapshot = dict(row)
        return (
            snapshot.get("version"),
            snapshot,
            str(snapshot.get("summary_text") or snapshot.get("profile_json") or ""),
        )
    raise ValueError("Source database has no profile snapshot to import.")


def _normalize(value: Any) -> str:
    text = str(value or "").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _family_key(job: dict[str, Any]) -> str:
    company = _normalize(job.get("company_normalized") or job.get("company"))
    title = _normalize(job.get("title"))
    if company and title:
        return f"company-title:{company}|{title}"
    content_hash = str(job.get("content_hash") or "").strip()
    if content_hash:
        return f"content:{content_hash}"
    return f"source-job:{job.get('id')}"


def _rank(seed: int, job_id: int) -> str:
    return hashlib.sha256(f"{seed}:{job_id}".encode()).hexdigest()


def _candidate_pool(
    conn: sqlite3.Connection, profile_version: int | None, pool_size: int
) -> list[dict[str, Any]]:
    job_columns = _columns(conn, "jobs")
    verdict_columns = _columns(conn, "job_verdicts")
    duplicate_clause = "AND j.duplicate_of IS NULL" if "duplicate_of" in job_columns else ""
    description_clause = (
        "AND COALESCE(TRIM(j.description), '') <> ''" if "description" in job_columns else ""
    )
    scrape_columns = (
        _columns(conn, "scrape_cells") if _table_exists(conn, "scrape_cells") else set()
    )
    can_filter_live = {
        "scrape_cell_id",
        "last_seen_at",
    }.issubset(job_columns) and "last_success_at" in scrape_columns
    live_join = "LEFT JOIN scrape_cells sc ON sc.id = j.scrape_cell_id" if can_filter_live else ""
    live_clause = ""
    if can_filter_live:
        live_clause = """
            AND j.last_seen_at IS NOT NULL
            AND sc.last_success_at IS NOT NULL
            AND unixepoch(sc.last_success_at) - unixepoch(j.last_seen_at) <= 43200
            AND unixepoch('now') - unixepoch(sc.last_success_at) <= 604800
        """
    date_columns = [
        f"j.{name}" for name in ("last_seen_at", "date_posted", "date_found") if name in job_columns
    ]
    order_expr = f"COALESCE({', '.join(date_columns)})" if date_columns else "j.id"
    reason_type = "v.reason_type" if "reason_type" in verdict_columns else "NULL"
    reason_description = (
        "v.reason_description" if "reason_description" in verdict_columns else "NULL"
    )
    created_at = "v.created_at" if "created_at" in verdict_columns else "''"
    profile_filter = ""
    # IDs are monotonically assigned at ingestion, so this bounded materialized CTE is a
    # cheap recent-window scan even when production contains hundreds of thousands of rows.
    # The final date ordering happens only inside that window instead of sorting the corpus.
    recent_window = pool_size * 5
    params: list[Any] = [recent_window]
    if profile_version is not None and "profile_version" in verdict_columns:
        profile_filter = "AND v.profile_version = ?"
        params.append(profile_version)
    params.append(pool_size)
    rows = conn.execute(
        f"""
        WITH recent_jobs AS MATERIALIZED (
            SELECT j0.* FROM jobs j0
             WHERE 1 = 1
                   {duplicate_clause.replace("j.", "j0.")}
                   {description_clause.replace("j.", "j0.")}
             ORDER BY j0.id DESC
             LIMIT ?
        )
        SELECT j.id, j.title, j.company,
               {"j.company_normalized" if "company_normalized" in job_columns else "NULL"}
                   AS company_normalized,
               {"j.content_hash" if "content_hash" in job_columns else "NULL"} AS content_hash,
               v.fit, {reason_type} AS reason_type,
               {reason_description} AS reason_description,
               {created_at} AS verdict_created_at
          FROM recent_jobs j
          JOIN job_verdicts v ON v.job_id = j.id {profile_filter}
          {live_join}
         WHERE v.fit IN (0, 1)
               {live_clause}
         ORDER BY {order_expr} DESC, j.id DESC
         LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _take_ranked(
    candidates: list[dict[str, Any]], count: int, seed: int, used: set[int]
) -> list[dict[str, Any]]:
    available = [row for row in candidates if int(row["id"]) not in used]
    available.sort(key=lambda row: _rank(seed, int(row["id"])))
    selected = available[:count]
    used.update(int(row["id"]) for row in selected)
    return selected


def sample_candidates(
    conn: sqlite3.Connection,
    *,
    profile_version: int | None,
    count: int,
    seed: int,
) -> list[tuple[dict[str, Any], str]]:
    """Build the planned 50/25/25 audit mix from recent deduplicated jobs."""
    if count < 1:
        raise ValueError("Count must be positive.")
    pool = _candidate_pool(conn, profile_version, max(2500, count * 60))
    if not pool:
        return []

    family_fits: dict[str, set[int]] = defaultdict(set)
    for row in pool:
        family_fits[_family_key(row)].add(int(row["fit"]))

    borderline = [
        row
        for row in pool
        if len(family_fits[_family_key(row)]) > 1
        or (int(row["fit"]) == 1 and GAP_PATTERN.search(str(row.get("reason_description") or "")))
    ]
    rejects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        if int(row["fit"]) == 0:
            reason = str(row.get("reason_type") or "unknown").casefold()
            key = reason if reason in TARGET_REJECT_REASONS else "other"
            rejects[key].append(row)

    reject_count = round(count * 0.50)
    match_count = round(count * 0.25)
    borderline_count = count - reject_count - match_count
    used: set[int] = set()
    selected: list[tuple[dict[str, Any], str]] = []

    # Take the borderline cases first so ordinary match sampling cannot consume them.
    for row in _take_ranked(borderline, borderline_count, seed + 11, used):
        selected.append((row, "borderline"))

    per_reason, remainder = divmod(reject_count, len(TARGET_REJECT_REASONS))
    for index, reason in enumerate(TARGET_REJECT_REASONS):
        target = per_reason + int(index < remainder)
        for row in _take_ranked(rejects[reason], target, seed + 100 + index, used):
            selected.append((row, f"reject:{reason}"))

    # Sparse strata are backfilled from all rejects while retaining the category in the
    # snapshot metadata, rather than silently shrinking the requested queue.
    missing_rejects = reject_count - sum(bucket.startswith("reject:") for _, bucket in selected)
    all_rejects = [row for row in pool if int(row["fit"]) == 0]
    for row in _take_ranked(all_rejects, missing_rejects, seed + 199, used):
        reason = str(row.get("reason_type") or "other").casefold()
        selected.append((row, f"reject:{reason}"))

    matches = [row for row in pool if int(row["fit"]) == 1]
    for row in _take_ranked(matches, match_count, seed + 211, used):
        selected.append((row, "match"))

    if len(selected) < count:
        for row in _take_ranked(pool, count - len(selected), seed + 307, used):
            selected.append((row, "fallback"))

    # Deterministically shuffle the mixed buckets so reviewers cannot infer the model label
    # from queue position.
    selected.sort(key=lambda item: _rank(seed + 401, int(item[0]["id"])))
    return selected[:count]


def _selected_candidates(
    conn: sqlite3.Connection, profile_version: int | None, job_ids: list[int]
) -> list[tuple[dict[str, Any], str]]:
    verdict_columns = _columns(conn, "job_verdicts")
    result: list[tuple[dict[str, Any], str]] = []
    profile_filter = ""
    for job_id in job_ids:
        params: list[Any] = [job_id]
        if profile_version is not None and "profile_version" in verdict_columns:
            profile_filter = "AND profile_version = ?"
            params.append(profile_version)
        row = conn.execute(
            f"""
            SELECT job_id AS id, fit,
                   {"reason_type" if "reason_type" in verdict_columns else "NULL"}
                       AS reason_type,
                   {"reason_description" if "reason_description" in verdict_columns else "NULL"}
                       AS reason_description
              FROM job_verdicts
             WHERE job_id = ? {profile_filter}
             ORDER BY {"created_at DESC," if "created_at" in verdict_columns else ""} id DESC
             LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} has no verdict for the active profile.")
        candidate = dict(row)
        if candidate.get("fit") not in {0, 1}:
            raise ValueError(f"Job {job_id} does not have a completed binary verdict.")
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job is not None
        candidate.update(dict(job))
        # Building a list preserves the caller's explicit job order.
        result.append((candidate, "selected"))
    return result


def import_examples(
    *,
    source_db_path: str,
    labeler_db_path: str | None = None,
    count: int = 75,
    seed: int = 0,
    job_ids: list[int] | None = None,
    queue_name: str = "initial",
) -> dict[str, Any]:
    """Snapshot sampled or explicitly selected production examples into the labeler."""
    source_path = str(Path(source_db_path).expanduser().resolve())
    source_stat = Path(source_path).stat()
    source_identity = f"{source_path}:{source_stat.st_size}:{source_stat.st_mtime_ns}"
    source = _source_connection(source_path)
    try:
        profile_version, profile_snapshot, profile_text = _profile_snapshot(source)
        candidates = (
            _selected_candidates(source, profile_version, job_ids)
            if job_ids
            else sample_candidates(source, profile_version=profile_version, count=count, seed=seed)
        )
        source_schema_version = int(source.execute("PRAGMA user_version").fetchone()[0])
        imported = 0
        skipped = 0
        buckets: dict[str, int] = defaultdict(int)
        with LabelerDatabase(labeler_db_path) as labeler:
            for candidate, bucket in candidates:
                job_id = int(candidate["id"])
                job_row = source.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if job_row is None:
                    continue
                job = dict(job_row)
                verdict_params: list[Any] = [job_id]
                profile_clause = ""
                verdict_columns = _columns(source, "job_verdicts")
                if profile_version is not None and "profile_version" in verdict_columns:
                    profile_clause = "AND profile_version = ?"
                    verdict_params.append(profile_version)
                verdict_order = (
                    "created_at DESC, id DESC" if "created_at" in verdict_columns else "id DESC"
                )
                verdict_row = source.execute(
                    f"""
                    SELECT * FROM job_verdicts
                     WHERE job_id = ? {profile_clause}
                     ORDER BY {verdict_order}
                     LIMIT 1
                    """,
                    verdict_params,
                ).fetchone()
                if verdict_row is None:
                    continue
                verdict = dict(verdict_row)
                snapshot_hash = hashlib.sha256(
                    _canonical_json(
                        {"job": job, "profile": profile_snapshot, "verdict": verdict}
                    ).encode()
                ).hexdigest()
                fit = verdict.get("fit")
                model_outcome = "match" if fit == 1 else "reject" if fit == 0 else None
                metadata = {
                    "source_schema_version": source_schema_version,
                    "sampling_seed": seed,
                    "queue_name": queue_name,
                    "queue_bucket": bucket,
                }
                example = {
                    "snapshot_hash": snapshot_hash,
                    "source_job_id": str(job_id),
                    "source_db_path": source_path,
                    "source_db_identity": source_identity,
                    "source_profile_version": profile_version,
                    "imported_at": utc_now(),
                    "family_key": _family_key(job),
                    "queue_bucket": bucket,
                    "title": str(job.get("title") or "Untitled role"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "url": job.get("url"),
                    "description": str(job.get("description") or ""),
                    "date_posted": job.get("date_posted"),
                    "job_snapshot_json": _canonical_json(job),
                    "profile_text": profile_text,
                    "profile_snapshot_json": _canonical_json(profile_snapshot),
                    "rubric_version": verdict.get("prompt_hash"),
                    "model": verdict.get("model"),
                    "model_outcome": model_outcome,
                    "model_reason_type": verdict.get("reason_type"),
                    "model_reason_description": verdict.get("reason_description"),
                    "model_verdict_json": _canonical_json(verdict),
                    "source_metadata_json": _canonical_json(metadata),
                }
                if labeler.insert_example(example, queue_name=queue_name) is None:
                    skipped += 1
                else:
                    imported += 1
                    buckets[bucket] += 1
            labeler.conn.commit()
        return {
            "requested": len(candidates),
            "imported": imported,
            "skipped_existing": skipped,
            "queue": queue_name,
            "buckets": dict(sorted(buckets.items())),
            "source_profile_version": profile_version,
        }
    finally:
        source.close()
