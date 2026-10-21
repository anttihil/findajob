"""Assess postings without a priority through the scoring graph.

Runs on its own timer, independent of the scraper. A rate-limited job board cannot stall
scoring of the backlog, and a bad API key cannot lose a scrape -- that separation is the
whole reason the stages hand off through `jobs.pipeline_state` rather than sharing a
process.
"""

import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from findajob.core.config import load_config
from findajob.core.database import Database
from findajob.core.llm import (
    LLMUnavailableError,
    MissingApiKey,
    Spend,
    get_llm_provider,
    get_model_for_role,
    is_deepseek_peak_hour,
    next_deepseek_off_peak_utc,
)
from findajob.core.logger import get_logger
from findajob.profile.adapter import NoActiveProfile
from findajob.profile.models import JobFitVerdict, PriorityVerdict
from findajob.profile.repository import load_active
from findajob.scoring import repository as scoring_repo
from findajob.scoring.graph import CallBudget, build_graph
from findajob.scoring.prompts import (
    build_system,
    profile_hash,
    prompt_hash,
    render_posting,
)

logger = get_logger()

# Chars per token.
CHARS_PER_TOKEN = 3.0


MAX_SCORING_FAILURES = scoring_repo.MAX_SCORING_FAILURES
_live_clause = scoring_repo._live_clause
_ELIGIBLE = scoring_repo._ELIGIBLE
_NO_PRIORITY = scoring_repo._NO_PRIORITY


class ScoringBalanceError(RuntimeError):
    """Scoring cannot continue until the provider has API credits."""


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _using_deepseek() -> bool:
    try:
        return get_llm_provider().name == "deepseek"
    except LLMUnavailableError:
        return False


def _should_postpone_peak(
    scoring_config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    if not scoring_config.get("postpone_deepseek_peak_hours", True):
        return False, None
    if not _using_deepseek():
        return False, None
    current = now if now is not None else _now_dt()
    if not is_deepseek_peak_hour(current):
        return False, None
    return True, next_deepseek_off_peak_utc(current)


def _select(
    db: Database,
    limit: int | None,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    return scoring_repo.select_scoring_backlog(
        db.conn,
        limit=limit,
        include_closed=include_closed,
    )


def _pending(db: Database, include_closed: bool = False) -> int:
    return scoring_repo.count_pending_scoring(db.conn, include_closed=include_closed)


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
    profile_content_hash: str = "",
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
        profile_content_hash=profile_content_hash,
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


def _score_states(
    db: Database,
    jobs: list[dict[str, Any]],
    system: str,
    model: str,
    review_model: str,
    profile_version: int,
    phash: str,
    concurrency: int,
    budget: CallBudget | None = None,
    profile_content_hash: str = "",
    review_only: bool = False,
    force: bool = False,
) -> Generator[tuple[dict[str, Any], dict[str, Any]], None, None]:
    """Checkpoint complete single-stage calls before publishing; never resume old screens.

    review_model/review_only are ignored compatibility arguments for older callers.
    Explicit reassessment forces a fresh call. Automatic recovery reuses matching work.
    """
    del review_model, review_only
    try:
        provider_identity = get_llm_provider().name
    except LLMUnavailableError:
        provider_identity = "unavailable"
    cache_hash = f"{provider_identity}:{phash}"
    graph = build_graph()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for offset in range(0, len(jobs), concurrency):
            batch = jobs[offset : offset + concurrency]
            priors = [
                scoring_repo.load_assessment(db.conn, job, profile_version, cache_hash, model)
                if not force
                else {}
                for job in batch
            ]

            def assess(pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
                job, prior = pair
                if prior.get("verdict"):
                    return {**prior, "run_calls": 0, "run_cost_usd": 0, "run_usage": None}
                state = graph.invoke(
                    {
                        "system": system,
                        "posting": job,
                        "model": model,
                        "budget": budget,
                        "usage": prior.get("usage"),
                        "cost_usd": prior.get("cost_usd", 0),
                        "cost_recorded": prior.get("cost_recorded", 0),
                        "profile_hash": profile_content_hash,
                    }
                )
                state["run_calls"] = state.get("attempts", 0)
                state["attempts"] = state.get("attempts", 0) + prior.get("attempts", 0)
                state["run_cost_usd"] = state.get("cost_usd", 0) - prior.get("cost_usd", 0)
                state["run_usage"] = (
                    {
                        key: value - (prior.get("usage") or {}).get(key, 0)
                        for key, value in state["usage"].items()
                    }
                    if state.get("usage")
                    else None
                )
                return state

            completed = list(pool.map(assess, zip(batch, priors, strict=True)))
            for job, state in zip(batch, completed, strict=True):
                if state.get("run_calls", 0):
                    scoring_repo.save_assessment(
                        db.conn, job, profile_version, cache_hash, model, state
                    )
            db.conn.commit()
            for job, state in zip(batch, completed, strict=True):
                yield job, state
            if any(state.get("balance_exhausted") for state in completed) or (
                budget and budget.exhausted()
            ):
                return


def run_scoring(limit: int | None = None) -> int:
    config = load_config()
    scoring_config = config.get("scoring") or {}
    model = scoring_config.get("model") or get_model_for_role("scoring")
    concurrency = int(scoring_config.get("concurrency", 8))
    max_usd = scoring_config.get("max_usd_per_run")

    postponed, resume_at = _should_postpone_peak(scoring_config)
    if postponed and resume_at is not None:
        logger.info(
            "Postponing DeepSeek scoring during peak hours; off-peak resumes at %s.",
            resume_at.isoformat(),
        )
        print(
            f"Postponing DeepSeek scoring during peak hours (01:00–04:00 and 06:00–10:00 UTC, "
            f"Mon–Fri). Off-peak resumes at {resume_at.isoformat()}."
        )
        return 0

    loaded = load_active()
    if loaded is None:
        logger.error("No active profile. Build one first: findajob profile build")
        return 1
    profile_version, _profile, summary = loaded

    db = Database()
    try:
        jobs = _select(db, limit)
        if not jobs:
            failure = scoring_repo.get_provider_failure(db.conn)
            if failure:
                logger.error(failure["message"])
                return 1
            print("Nothing to score. The backlog is drained.")
            return 0

        system = build_system(summary)
        phash = prompt_hash(summary)
        system_tokens = int(len(system) / CHARS_PER_TOKEN)
        print(f"profile v{profile_version} · model {model}")
        print(f"postings to score: {len(jobs):,}")
        print(f"cached prefix:     {system_tokens:,} tokens")
        print("cost:              measured from DeepSeek balance after each API call")

        spend = Spend(model, max_usd=max_usd)
        if spend.exhausted():
            print("Scoring budget is exhausted; no model calls made.")
            return 0
        results = {
            "scored": 0,
            "failed": 0,
            "retried": 0,
            "retry_usd": 0.0,
        }
        first_usage = []
        provider_blocked = False
        provider_recovered = False

        states = _score_states(
            db,
            jobs,
            system,
            model,
            "",
            profile_version,
            phash,
            max(1, concurrency),
            budget=CallBudget(max_usd),
            profile_content_hash=profile_hash(summary),
        )
        try:
            for job, state in states:
                verdict = state.get("verdict")
                usage = state.get("usage")
                cost = state.get("run_cost_usd", state.get("cost_usd", 0.0))
                spend.usd += cost
                spend.calls += state.get("run_calls", state.get("attempts", 0))
                run_usage = state.get("run_usage", usage)
                if run_usage:
                    spend.tokens_in += run_usage["prompt"]
                    spend.tokens_cached += run_usage["cache_hit"]
                    spend.tokens_out += run_usage["completion"]
                    if len(first_usage) < 4:
                        first_usage.append(usage)

                if state.get("budget_exhausted"):
                    continue
                if verdict is None:
                    results["failed"] += 1
                    results["retry_usd"] += cost
                    if state.get("balance_exhausted"):
                        scoring_repo.record_balance_failure(db.conn)
                        provider_blocked = True
                    else:
                        _record_failure(db, job["id"], state.get("error"))
                    db.conn.commit()
                    logger.warning(
                        "No verdict for job %s (%s): %s",
                        job["id"],
                        job["title"],
                        state.get("error"),
                    )
                else:
                    if state.get("run_calls", 1) > 1:
                        results["retried"] += 1
                    _persist(
                        db,
                        job,
                        verdict,
                        usage,
                        state.get("cost_usd", 0.0),
                        model,
                        profile_version,
                        phash,
                        profile_hash(summary),
                    )
                    db.conn.commit()
                    results["scored"] += 1
                    provider_recovered |= state.get("run_calls", state.get("attempts", 1)) > 0

                    if results["scored"] % 25 == 0:
                        print(f"  {results['scored']:,}/{len(jobs):,} scored  ${spend.usd:.4f}")

                # Finish accounting/persisting the current bounded batch before stopping.
                processed = results["scored"] + results["failed"]
                if processed % max(1, concurrency) == 0:
                    if spend.exhausted():
                        print(f"\nStopping: spent ${spend.usd:.2f}, ceiling is ${max_usd:.2f}.")
                        break
                    postponed, resume_at = _should_postpone_peak(scoring_config)
                    if postponed and resume_at is not None:
                        logger.info(
                            "Pausing DeepSeek scoring at peak-hour boundary; "
                            "off-peak resumes at %s.",
                            resume_at.isoformat(),
                        )
                        print(
                            f"\nStopping: entered DeepSeek peak hours; "
                            f"off-peak resumes at {resume_at.isoformat()}."
                        )
                        break
        finally:
            states.close()

        if provider_recovered and not provider_blocked:
            scoring_repo.clear_provider_failure(db.conn)
        db.conn.commit()

        summary_stats = spend.summary()
        print()
        print(f"scored:     {results['scored']:,}")
        if results["failed"]:
            print(f"failed:     {results['failed']:,}  (left pending; the next run retries)")
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

        print(f"remaining:  {_pending(db):,} unscored")
        skipped = _ineligible(db)
        if skipped["total"]:
            print(
                f"excluded:   {skipped['total']:,} never scoreable  "
                f"({skipped['duplicate']:,} duplicate, "
                f"{skipped['thin']:,} no description, "
                f"{skipped['closed']:,} closed)"
            )

        stuck = _quarantined(db)
        if stuck:
            total = sum(row["total"] for row in stuck)
            print(
                f"quarantined: {total:,} withdrawn after {MAX_SCORING_FAILURES} failed "
                f"runs  (`findajob score retry` to re-offer)"
            )
            for row in stuck:
                print(f"  {row['total']:>4}x  job {row['example']}: {row['reason']}")
        if provider_blocked:
            logger.error(scoring_repo.BALANCE_ERROR_MESSAGE)
            return 1
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
    *,
    review_only: bool = False,
    budget: CallBudget | None = None,
) -> dict[str, Any]:
    """Score a single job posting by ID against the active profile."""
    loaded = load_active()
    if loaded is None:
        raise NoActiveProfile("No active profile. Build one first: findajob profile build")
    profile_version, _profile, summary = loaded

    config = load_config()
    scoring_config = config.get("scoring") or {}
    model_name = model or scoring_config.get("model") or get_model_for_role("scoring")

    owned = db is None
    database = db or Database()
    try:
        job_res = database.query_jobs(job_id=job_id, detail=True)
        jobs = job_res.get("jobs") or []
        if not jobs:
            raise ValueError(f"Job with id {job_id} not found.")
        job = jobs[0]

        system = build_system(summary)
        phash = prompt_hash(summary)
        states = _score_states(
            database,
            [job],
            system,
            model_name,
            "",
            profile_version,
            phash,
            1,
            budget=budget or CallBudget(scoring_config.get("max_usd_per_run")),
            profile_content_hash=profile_hash(summary),
            review_only=review_only,
            force=True,
        )
        try:
            _, final_state = next(states)
        finally:
            states.close()

        verdict = final_state.get("verdict")
        usage = final_state.get("usage")
        cost = final_state.get("cost_usd", 0.0)

        if verdict is None:
            if final_state.get("budget_exhausted"):
                database.conn.commit()
                raise RuntimeError(f"{final_state.get('error')}; assessment can resume")
            if final_state.get("balance_exhausted"):
                scoring_repo.record_balance_failure(database.conn)
                database.conn.commit()
                raise ScoringBalanceError(scoring_repo.BALANCE_ERROR_MESSAGE)
            _record_failure(database, job_id, final_state.get("error"))
            database.conn.commit()
            raise RuntimeError(f"Scoring failed for job {job_id}: {final_state.get('error')}")

        _persist(
            database,
            job,
            verdict,
            usage,
            cost,
            model_name,
            profile_version,
            phash,
            profile_hash(summary),
        )
        if final_state.get("run_calls", final_state.get("attempts", 1)) > 0:
            scoring_repo.clear_provider_failure(database.conn)
        database.conn.commit()
        return {"stage": "assessed", **verdict}
    finally:
        if owned:
            database.close()


def score_selected(
    job_ids: list[int], model: str | None = None, *, review_only: bool = False
) -> dict[str, Any]:
    """Score explicit posting IDs without draining the normal backlog."""
    if not job_ids:
        raise ValueError("provide at least one --job-id")
    budget = CallBudget((load_config().get("scoring") or {}).get("max_usd_per_run"))
    results: list[dict[str, Any]] = []
    blocked = False
    for job_id in dict.fromkeys(job_ids):
        if blocked:
            results.append(
                {"job_id": job_id, "status": "error", "error": scoring_repo.BALANCE_ERROR_MESSAGE}
            )
            continue
        try:
            results.append(
                {
                    "job_id": job_id,
                    "status": "ok",
                    "verdict": score_job(
                        job_id, model=model, review_only=review_only, budget=budget
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - report every requested ID.
            blocked = isinstance(exc, ScoringBalanceError)
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
        raise NoActiveProfile("No active profile. Build one first: findajob profile build")
    profile_version, _profile, summary = loaded
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
            "instructions": build_system(summary),
            "jobs": jobs,
            "missing_job_ids": missing,
        }
    finally:
        db.close()


def save_reviewed_verdict(args: Any) -> dict[str, Any]:
    """Validate and persist a human or external review verdict."""
    loaded = load_active()
    if loaded is None:
        raise NoActiveProfile("No active profile. Build one first: findajob profile build")
    profile_version, _profile, summary = loaded
    if getattr(args, "priority", None):
        verdict = PriorityVerdict(
            priority=args.priority, reason=args.reason_description
        ).model_dump()
    else:
        if args.fit is None:
            raise ValueError("Provide --priority or a legacy --fit/--no-fit judgment")
        verdict = JobFitVerdict(
            fit=args.fit,
            reason_type=args.reason_type or "unknown",
            reason_description=args.reason_description,
        ).model_dump()
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
            phash=prompt_hash(summary),
            profile_content_hash=profile_hash(summary),
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
