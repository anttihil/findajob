"""Drain unscored postings through the scoring graph.

Runs on its own timer, independent of the scraper. A rate-limited job board cannot stall
scoring of the backlog, and a bad API key cannot lose a scrape -- that separation is the
whole reason the stages hand off through `jobs.pipeline_state` rather than sharing a
process.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.llm import (
    DEFAULT_SCORING_MODEL,
    MissingApiKey,
    Spend,
    estimate_cost,
    usage_cost,
)
from careerradar.core.logger import get_logger
from careerradar.profile.adapter import NoActiveProfile, ProfileAdapter, load_profile
from careerradar.profile.repository import load_active
from careerradar.scoring import repository as scoring_repo
from careerradar.scoring.graph import build_graph
from careerradar.scoring.prompts import (
    build_system,
    format_matched,
    prompt_hash,
    render_posting,
    render_skill_hint,
)
from careerradar.taxonomy.skills import Taxonomy, load_taxonomy

if TYPE_CHECKING:
    from careerradar.search.keyword_score import JobScorer

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


def _build_scorer(
    adapter: ProfileAdapter, taxonomy: Taxonomy, config: dict[str, Any]
) -> "JobScorer | None":
    """The deterministic scorer, used here only to produce the prompt's skill hint.

    Its number is not consulted. What the scoring agent gets is the extracted requirement
    list split into evidenced and not, which is the part of the keyword layer that was
    always worth having -- an anchor so the model does not re-derive the requirements from
    scratch and quietly miss one.
    """
    try:
        from careerradar.search.keyword_score import JobScorer
        from careerradar.taxonomy.roles import load_roles

        return JobScorer(
            adapter, load_roles(), taxonomy, weights=(config.get("matching") or {}).get("weights")
        )
    except Exception:
        logger.warning(
            "Deterministic scorer unavailable; scoring without a skill hint", exc_info=True
        )
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _select(
    db: Database,
    limit: int | None,
    rescore_all: bool,
    profile_version: int,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    return scoring_repo.select_scoring_backlog(
        db.conn,
        limit=limit,
        rescore_all=rescore_all,
        profile_version=profile_version,
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


def _skill_hint(scorer: "JobScorer | None", job: dict[str, Any]) -> str:
    """The extractor's read on one posting, rendered for the prompt.

    Returns "" when there is nothing to say. An empty hint block would still cost tokens
    and would teach the model that the absence of a signal means the absence of a
    requirement.
    """
    if scorer is None:
        return ""
    try:
        result = scorer.score(job)
    except Exception:  # a scorer failure must not cost the posting its verdict
        logger.debug("Skill hint unavailable for job %s", job.get("id"), exc_info=True)
        return ""
    matched = [
        (scorer.taxonomy.label(key), scorer.profile.level(key)) for key in result["matched_skills"]
    ]
    missing = [scorer.taxonomy.label(key) for key in result["missing_skills"]]
    return render_skill_hint(matched=format_matched(matched), missing=missing)


def run_retry(job_id: int | None = None) -> int:
    """Clear the failure counter so quarantined postings are offered again.

    The counter records that the last MAX_SCORING_FAILURES runs failed, which is evidence
    about the pipeline as it was then -- not a property of the posting. Fix the prompt, fix
    a validator, add a language the extractor could not handle, and the same posting may
    score fine. Without this the quarantine is permanent and every such fix is invisible.
    """
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


def run_scoring(limit: int | None = None, rescore_all: bool = False, dry_run: bool = False) -> int:
    config = load_config()
    scoring_config = config.get("scoring") or {}
    model = scoring_config.get("model", DEFAULT_SCORING_MODEL)
    concurrency = int(scoring_config.get("concurrency", 8))
    max_usd = scoring_config.get("max_usd_per_run")
    fit_threshold = int(scoring_config.get("fit_threshold", 90))

    loaded = load_active()
    if loaded is None:
        logger.error("No active profile. Build one first: careerradar profile build")
        return 1
    profile_version, _profile, summary = loaded

    db = Database()
    try:
        jobs = _select(db, limit, rescore_all, profile_version)
        taxonomy = load_taxonomy()
        adapter = load_profile(db=db, taxonomy=taxonomy)
        # `required=True` (the default) means load_profile never actually returns None --
        # it raises NoActiveProfile instead. The signature just doesn't say so yet.
        assert adapter is not None
        scorer = _build_scorer(adapter, taxonomy, config)
        if not jobs:
            print("Nothing to score. The backlog is drained.")
            return 0

        system = build_system(summary, fit_threshold=fit_threshold)
        phash = prompt_hash(summary, fit_threshold=fit_threshold)
        system_tokens = int(len(system) / CHARS_PER_TOKEN)
        hints = {job["id"]: _skill_hint(scorer, job) for job in jobs}
        posting_tokens = sum(
            int(len(render_posting(j, skill_hint=hints[j["id"]])) / CHARS_PER_TOKEN) for j in jobs
        )

        # The system half is paid uncached exactly once, then read from cache. Modelling
        # it any other way overstates the bill by ~50x on the cached portion and makes the
        # ceiling useless.
        estimate = (
            estimate_cost(model, system_tokens, 0)
            + estimate_cost(
                model, posting_tokens, EXPECTED_COMPLETION_TOKENS * len(jobs), cached_tokens=0
            )
            + estimate_cost(
                model,
                system_tokens * (len(jobs) - 1),
                0,
                cached_tokens=system_tokens * (len(jobs) - 1),
            )
        )

        print(f"profile v{profile_version} · model {model}")
        print(f"postings to score: {len(jobs):,}")
        print(f"cached prefix:     {system_tokens:,} tokens")
        print(f"estimated cost:    ${estimate:.4f}")

        if max_usd is not None and estimate > max_usd:
            # Abort rather than trim. Trimming produces a partial pass that looks complete,
            # and every coverage number downstream silently inherits the shortfall.
            print()
            print(
                f"ABORT: estimate ${estimate:.2f} exceeds scoring.max_usd_per_run ${max_usd:.2f}."
            )
            print("Raise the ceiling in config.yaml, or scope the run with --limit.")
            return 1

        if dry_run:
            print("\n(dry run -- nothing scored, nothing written)")
            return 0

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
                    "skill_hint": hints.get(job["id"], ""),
                    "profile": adapter,
                    "taxonomy": taxonomy,
                }
            )

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for job, state in pool.map(score_one, jobs):
                verdict = state.get("verdict")
                # Every attempt the graph made on this posting, retries included. Charged
                # before the verdict is examined, because a posting that never produced
                # one still spent its tokens: accounting only for successes hid the whole
                # retry volume from the run total and from `job_verdicts.cost_usd`.
                usage = state.get("usage")
                cost = usage_cost(model, usage) if usage else 0.0
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
                    # Commit per posting, not per 25. Python opens a deferred transaction
                    # on the first write and holds SQLite's single writer slot until the
                    # commit, so batching 25 verdicts held that slot for 25 LLM
                    # round-trips -- longer than the 30s busy_timeout, which is how a
                    # concurrent `sync` died with `database is locked`. One WAL append per
                    # posting costs nothing next to the API call that produced it.
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
