"""Drain unscored postings through the scoring graph.

Runs on its own timer, independent of the scraper. A rate-limited job board cannot stall
scoring of the backlog, and a bad API key cannot lose a scrape -- that separation is the
whole reason the stages hand off through `jobs.pipeline_state` rather than sharing a
process.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.llm import (
    MissingApiKey,
    Spend,
    get_model_for_role,
)
from careerradar.core.logger import get_logger
from careerradar.profile.adapter import NoActiveProfile
from careerradar.profile.models import JobFitVerdict
from careerradar.profile.repository import load_active
from careerradar.scoring import repository as scoring_repo
from careerradar.scoring.graph import build_graph
from careerradar.scoring.prompts import (
    build_system,
    prompt_hash,
    render_posting,
)

logger = get_logger()

# Chars per token.
CHARS_PER_TOKEN = 3.0
# Measured completion length for simplified JobFitVerdict (fit, reason_type, reason_description).
EXPECTED_COMPLETION_TOKENS = 60


MAX_SCORING_FAILURES = scoring_repo.MAX_SCORING_FAILURES
_seniority_clause = scoring_repo._seniority_clause
_age_clause = scoring_repo._age_clause
_live_clause = scoring_repo._live_clause
_ELIGIBLE = scoring_repo._ELIGIBLE
_NO_VERDICT = scoring_repo._NO_VERDICT


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _select(
    db: Database,
    limit: int | None,
    profile_version: int,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    return scoring_repo.select_scoring_backlog(
        db.conn,
        profile_version=profile_version,
        limit=limit,
        include_closed=include_closed,
    )


def _pending(db: Database, profile_version: int, include_closed: bool = False) -> int:
    return scoring_repo.count_pending_scoring(
        db.conn, profile_version=profile_version, include_closed=include_closed
    )


def _ineligible(db: Database) -> sqlite3.Row:
    return scoring_repo.get_ineligible_breakdown(db.conn)


def _record_failure(db: Database, job_id: int, error: str | BaseException | None) -> None:
    scoring_repo.record_scoring_failure(db.conn, job_id=job_id, error=error)


def _quarantined(db: Database) -> list[sqlite3.Row]:
    return scoring_repo.get_quarantined_jobs(db.conn, max_failures=MAX_SCORING_FAILURES)


def _persist(
    db: Database,
    job: dict[str, Any],
    verdict: dict[str, Any],
    usage: dict[str, int] | None,
    cost: float,
    model: str,
    profile_version: int,
    phash: str,
) -> None:
    scoring_repo.save_verdict(
        db.conn,
        job=job,
        verdict=verdict,
        usage=usage,
        cost=cost,
        model=model,
        profile_version=profile_version,
        phash=phash,
    )


def run_retry(job_id: int | None = None) -> int:
    """Clear the failure counter so quarantined postings are offered again."""
    db = Database()
    try:
        cleared_count = scoring_repo.reset_scoring_failures(db.conn, job_id=job_id)
        print(
            f"cleared the failure counter on {cleared_count:,} posting(s); "
            f"the next `score run` will offer them again."
        )
        return 0
    finally:
        db.close()


def run_scoring(limit: int | None = None) -> int:
    config = load_config()
    scoring_config = config.get("scoring") or {}
    model = scoring_config.get("model") or get_model_for_role("scoring")
    concurrency = int(scoring_config.get("concurrency", 8))
    max_usd = scoring_config.get("max_usd_per_run")
    fit_threshold = int(scoring_config.get("fit_threshold", 70))

    loaded = load_active()
    if loaded is None:
        logger.error("No active profile. Build one first: careerradar profile build")
        return 1
    profile_version, _profile, summary = loaded

    db = Database()
    try:
        jobs = _select(db, limit, profile_version)
        if not jobs:
            print("Nothing to score. The backlog is drained.")
            return 0

        system = build_system(summary, fit_threshold=fit_threshold)
        phash = prompt_hash(summary, fit_threshold=fit_threshold)
        system_tokens = int(len(system) / CHARS_PER_TOKEN)
        print(f"profile v{profile_version} · model {model}")
        print(f"postings to score: {len(jobs):,}")
        print(f"cached prefix:     {system_tokens:,} tokens")
        print("cost:              measured from DeepSeek balance after each API call")

        graph = build_graph()
        spend = Spend(model, max_usd=max_usd)
        results = {"scored": 0, "failed": 0, "retried": 0, "retry_usd": 0.0}
        first_usage = []

        def score_one(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            return job, graph.invoke(
                {
                    "system": system,
                    "posting": job,
                    "model": model,
                }
            )

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for job, state in pool.map(score_one, jobs):
                verdict = state.get("verdict")
                usage = state.get("usage")
                cost = state.get("cost_usd", 0.0)
                spend.usd += cost
                spend.calls += state.get("attempts", 0)
                if usage:
                    spend.tokens_in += usage["prompt"]
                    spend.tokens_cached += usage["cache_hit"]
                    spend.tokens_out += usage["completion"]
                    if len(first_usage) < 4:
                        first_usage.append(usage)

                if verdict is None:
                    results["failed"] += 1
                    results["retry_usd"] += cost
                    _record_failure(db, job["id"], state.get("error"))
                    db.conn.commit()
                    logger.warning(
                        "No verdict for job %s (%s): %s",
                        job["id"],
                        job["title"],
                        state.get("error"),
                    )
                else:
                    if state.get("attempts", 1) > 1:
                        results["retried"] += 1
                    _persist(db, job, verdict, usage, cost, model, profile_version, phash)
                    db.conn.commit()
                    results["scored"] += 1

                    if results["scored"] % 25 == 0:
                        print(f"  {results['scored']:,}/{len(jobs):,} scored  ${spend.usd:.4f}")

                if spend.exhausted():
                    print(f"\nStopping: spent ${spend.usd:.2f}, ceiling is ${max_usd:.2f}.")
                    break

        db.conn.commit()

        summary_stats = spend.summary()
        print()
        print(f"scored:     {results['scored']:,}")
        if results["failed"]:
            print(f"failed:     {results['failed']:,}  (left as 'new'; the next run retries)")
        print(f"cost:       ${summary_stats['usd']:.4f}")
        if results["retried"] or results["retry_usd"]:
            print(
                f"  of which: {results['retried']:,} posting(s) needed a retry; "
                f"${results['retry_usd']:.4f} bought no verdict at all"
            )
        print(
            f"tokens:     {summary_stats['tokens_in']:,} in "
            f"({summary_stats['tokens_cached']:,} cached, "
            f"{summary_stats['cache_rate']:.0%}) / "
            f"{summary_stats['tokens_out']:,} out"
        )

        if len(first_usage) > 1 and summary_stats["cache_rate"] < 0.1:
            logger.warning(
                "Prefix cache hit rate is %.0f%% across %d calls. Something per-posting "
                "is leaking into the system prompt -- see docs/deepseek.md.",
                summary_stats["cache_rate"] * 100,
                summary_stats["calls"],
            )

        print(f"remaining:  {_pending(db, profile_version):,} unscored")
        skipped = _ineligible(db)
        if skipped["total"]:
            print(
                f"excluded:   {skipped['total']:,} never scoreable  "
                f"({skipped['duplicate']:,} duplicate, "
                f"{skipped['thin']:,} no description, "
                f"{skipped['closed']:,} closed, "
                f"{skipped['seniority'] or 0:,} seniority)"
            )

        stuck = _quarantined(db)
        if stuck:
            total = sum(row["total"] for row in stuck)
            print(
                f"quarantined: {total:,} withdrawn after {MAX_SCORING_FAILURES} failed "
                f"runs  (`careerradar score retry` to re-offer)"
            )
            for row in stuck:
                print(f"  {row['total']:>4}x  job {row['example']}: {row['reason']}")
        return 0
    except MissingApiKey as exc:
        logger.error(str(exc))
        return 1
    except NoActiveProfile as exc:
        logger.error(str(exc))
        return 1
    finally:
        db.close()


def score_job(
    job_id: int,
    db: Database | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Score a single job posting by ID against the active profile."""
    loaded = load_active()
    if loaded is None:
        raise NoActiveProfile("No active profile. Build one first: careerradar profile build")
    profile_version, _profile, summary = loaded

    config = load_config()
    scoring_config = config.get("scoring") or {}
    model_name = model or scoring_config.get("model") or get_model_for_role("scoring")
    fit_threshold = int(scoring_config.get("fit_threshold", 70))

    owned = db is None
    database = db or Database()
    try:
        job_res = database.query_jobs(job_id=job_id, detail=True)
        jobs = job_res.get("jobs") or []
        if not jobs:
            raise ValueError(f"Job with id {job_id} not found.")
        job = jobs[0]

        system = build_system(summary, fit_threshold=fit_threshold)
        phash = prompt_hash(summary, fit_threshold=fit_threshold)

        graph = build_graph()
        final_state: dict[str, Any] = graph.invoke(
            {
                "system": system,
                "posting": job,
                "model": model_name,
            }
        )

        verdict = final_state.get("verdict")
        usage = final_state.get("usage")
        cost = final_state.get("cost_usd", 0.0)

        if verdict is None:
            _record_failure(database, job_id, final_state.get("error"))
            database.conn.commit()
            raise RuntimeError(f"Scoring failed for job {job_id}: {final_state.get('error')}")

        _persist(database, job, verdict, usage, cost, model_name, profile_version, phash)
        database.conn.commit()
        return verdict
    finally:
        if owned:
            database.close()


def score_selected(job_ids: list[int], model: str | None = None) -> dict[str, Any]:
    """Score explicit posting IDs without draining the normal backlog."""
    if not job_ids:
        raise ValueError("provide at least one --job-id")
    results: list[dict[str, Any]] = []
    for job_id in dict.fromkeys(job_ids):
        try:
            results.append(
                {"job_id": job_id, "status": "ok", "verdict": score_job(job_id, model=model)}
            )
        except Exception as exc:  # noqa: BLE001 - report every requested ID.
            results.append({"job_id": job_id, "status": "error", "error": str(exc)})
    failed = sum(result["status"] == "error" for result in results)
    return {
        "status": "ok" if not failed else ("partial" if failed < len(results) else "error"),
        "requested": len(results),
        "scored": len(results) - failed,
        "failed": failed,
        "results": results,
    }


def review_packet(job_ids: list[int]) -> dict[str, Any]:
    """Return active-profile instructions and selected postings for external review."""
    if not job_ids:
        raise ValueError("provide at least one --job-id")
    loaded = load_active()
    if loaded is None:
        raise NoActiveProfile("No active profile. Build one first: careerradar profile build")
    profile_version, _profile, summary = loaded
    threshold = int((load_config().get("scoring") or {}).get("fit_threshold", 70))
    db = Database()
    try:
        jobs, missing = [], []
        for job_id in dict.fromkeys(job_ids):
            found = db.query_jobs(job_id=job_id, detail=True)["jobs"]
            (jobs if found else missing).append(
                {"job_id": job_id, "posting": render_posting(found[0])} if found else job_id
            )
        return {
            "status": "ok" if not missing else "partial",
            "profile_version": profile_version,
            "instructions": build_system(summary, fit_threshold=threshold),
            "jobs": jobs,
            "missing_job_ids": missing,
        }
    finally:
        db.close()


def save_reviewed_verdict(args: Any) -> dict[str, Any]:
    """Validate and persist a human or external review verdict."""
    loaded = load_active()
    if loaded is None:
        raise NoActiveProfile("No active profile. Build one first: careerradar profile build")
    profile_version, _profile, summary = loaded
    verdict = JobFitVerdict(
        fit=args.fit, reason_type=args.reason_type, reason_description=args.reason_description
    ).model_dump()
    threshold = int((load_config().get("scoring") or {}).get("fit_threshold", 70))
    db = Database()
    try:
        jobs = db.query_jobs(job_id=args.job_id, detail=True)["jobs"]
        if not jobs:
            raise ValueError(f"job {args.job_id} not found")
        scoring_repo.save_verdict(
            db.conn,
            job=jobs[0],
            verdict=verdict,
            usage=None,
            cost=0.0,
            model="external-review",
            profile_version=profile_version,
            phash=prompt_hash(summary, fit_threshold=threshold),
        )
        db.conn.commit()
        return {
            "status": "ok",
            "job_id": args.job_id,
            "verdict": verdict,
            "model": "external-review",
        }
    finally:
        db.close()
