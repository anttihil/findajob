"""Drain unscored postings through the scoring graph.

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
from findajob.profile.models import JobFitVerdict
from findajob.profile.repository import load_active
from findajob.scoring import repository as scoring_repo
from findajob.scoring.graph import build_graph
from findajob.scoring.prompts import (
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
_live_clause = scoring_repo._live_clause
_ELIGIBLE = scoring_repo._ELIGIBLE
_NO_VERDICT = scoring_repo._NO_VERDICT


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


def _review_model(scoring_config: dict[str, Any]) -> str:
    review = scoring_config.get("review") or {}
    return (review.get("model") or "deepseek-v4-pro") if review.get("enabled", True) else ""


def _score_states(
    db: Database,
    jobs: list[dict[str, Any]],
    system: str,
    model: str,
    review_model: str,
    profile_version: int,
    phash: str,
    concurrency: int,
) -> Generator[tuple[dict[str, Any], dict[str, Any]], None, None]:
    """Run bounded batches, persisting LangGraph's capture boundary on the DB thread."""
    graph = build_graph()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for offset in range(0, len(jobs), concurrency):
            batch = jobs[offset : offset + concurrency]
            inputs = []
            priors = []
            for job in batch:
                prior = (
                    scoring_repo.load_pending_review(
                        db.conn, job, profile_version, phash, model, review_model
                    )
                    if review_model
                    else None
                )
                priors.append(prior or {})
                inputs.append(
                    {
                        **(prior or {}),
                        "system": system,
                        "posting": job,
                        "model": model,
                        "review_model": review_model,
                    }
                )

            def first_stage(state: dict[str, Any]) -> dict[str, Any]:
                if state.get("first_line"):
                    return state
                if review_model:
                    return graph.invoke(state, interrupt_after=["capture"])
                return graph.invoke(state)

            states = list(pool.map(first_stage, inputs))
            for job, state, prior in zip(batch, states, priors, strict=True):
                if state.get("first_line") and not prior:
                    scoring_repo.save_review_stage(
                        db.conn,
                        job,
                        state,
                        profile_version,
                        phash,
                        model,
                        review_model,
                        first_line=True,
                    )
            db.conn.commit()

            def second_stage(state: dict[str, Any]) -> dict[str, Any]:
                if review_model and state.get("first_line") and state.get("verdict") is None:
                    return graph.invoke(state)
                return state

            completed = list(pool.map(second_stage, states))
            for job, state, prior in zip(batch, completed, priors, strict=True):
                if state.get("first_line"):
                    scoring_repo.save_review_stage(
                        db.conn,
                        job,
                        state,
                        profile_version,
                        phash,
                        model,
                        review_model,
                        first_line=False,
                    )
                    db.conn.commit()
                # Persist lifetime totals, but charge only this run's calls to its budget.
                state["run_cost_usd"] = state.get("cost_usd", 0.0) - prior.get("cost_usd", 0.0)
                state["run_calls"] = state.get("attempts", 0) - prior.get("attempts", 0)
                usage = state.get("usage")
                prior_usage = prior.get("usage") or {}
                state["run_usage"] = (
                    {key: value - prior_usage.get(key, 0) for key, value in usage.items()}
                    if usage
                    else None
                )
                yield job, state
            # Finish/persist this bounded batch, but never start another during an outage.
            if any(state.get("balance_exhausted") for state in completed):
                return


def run_scoring(limit: int | None = None) -> int:
    config = load_config()
    scoring_config = config.get("scoring") or {}
    model = scoring_config.get("model") or get_model_for_role("scoring")
    concurrency = int(scoring_config.get("concurrency", 8))
    max_usd = scoring_config.get("max_usd_per_run")
    fit_threshold = int(scoring_config.get("fit_threshold", 70))
    review_model = _review_model(scoring_config)

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

        system = build_system(summary, fit_threshold=fit_threshold)
        phash = prompt_hash(summary, fit_threshold=fit_threshold, review_model=review_model)
        system_tokens = int(len(system) / CHARS_PER_TOKEN)
        print(f"profile v{profile_version} · model {model}")
        if review_model:
            print(f"secondary review: {review_model} · all verdicts")
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
            "reviewed": 0,
            "overturned": 0,
            "review_failed": 0,
        }
        first_usage = []
        provider_blocked = False
        provider_recovered = False

        states = _score_states(
            db, jobs, system, model, review_model, profile_version, phash, max(1, concurrency)
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

                if verdict is None:
                    if state.get("first_line"):
                        results["review_failed"] += 1
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
                    if state.get("attempts", 1) > (2 if state.get("first_line") else 1):
                        results["retried"] += 1
                    if state.get("first_line"):
                        results["reviewed"] += 1
                        results["overturned"] += verdict["fit"] != state["first_line"]["fit"]
                    _persist(
                        db,
                        job,
                        verdict,
                        usage,
                        state.get("cost_usd", 0.0),
                        review_model if state.get("first_line") else model,
                        profile_version,
                        phash,
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
        if review_model:
            print(f"reviewed:   {results['reviewed']:,} · overturned: {results['overturned']:,}")
            print(f"review failures: {results['review_failed']:,} (first-line verdict saved)")
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
) -> dict[str, Any]:
    """Score a single job posting by ID against the active profile."""
    loaded = load_active()
    if loaded is None:
        raise NoActiveProfile("No active profile. Build one first: findajob profile build")
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
        review_model = _review_model(scoring_config)
        phash = prompt_hash(summary, fit_threshold=fit_threshold, review_model=review_model)
        states = _score_states(
            database, [job], system, model_name, review_model, profile_version, phash, 1
        )
        try:
            _, final_state = next(states)
        finally:
            states.close()

        verdict = final_state.get("verdict")
        usage = final_state.get("usage")
        cost = final_state.get("cost_usd", 0.0)

        if verdict is None:
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
            review_model if final_state.get("first_line") else model_name,
            profile_version,
            phash,
        )
        if final_state.get("run_calls", final_state.get("attempts", 1)) > 0:
            scoring_repo.clear_provider_failure(database.conn)
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
    blocked = False
    for job_id in dict.fromkeys(job_ids):
        if blocked:
            results.append(
                {"job_id": job_id, "status": "error", "error": scoring_repo.BALANCE_ERROR_MESSAGE}
            )
            continue
        try:
            results.append(
                {"job_id": job_id, "status": "ok", "verdict": score_job(job_id, model=model)}
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
        raise NoActiveProfile("No active profile. Build one first: findajob profile build")
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
