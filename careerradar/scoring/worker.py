"""Drain unscored postings through the scoring graph.

Runs on its own timer, independent of the scraper. A rate-limited job board cannot stall
scoring of the backlog, and a bad API key cannot lose a scrape -- that separation is the
whole reason the stages hand off through `jobs.pipeline_state` rather than sharing a
process.
"""

import json
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
from careerradar.profile.models import VERDICT_SCHEMA_VERSION
from careerradar.profile.store import load_active
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

# Chars per token. DeepSeek publishes no token-counting endpoint, so the pre-flight gate
# uses a ratio measured against the spike's real responses (1,460-2,016 prompt tokens for
# postings of ~4,700-6,500 chars). Deliberately on the low side: an estimate that runs
# high aborts a run that would have been affordable, which is the cheaper mistake.
CHARS_PER_TOKEN = 3.0
# Measured completion length. The v1 schema returned a score, a band and three short
# lists at 562-670 tokens. The v2 schema also returns a role summary, five to eight
# quoted requirements and an assessment for each, which roughly doubles it.
#
# Understating this is the dangerous direction, not the expensive one: the pre-flight
# gate would pass, the run would start, and the live `Spend` ceiling would trip halfway
# through -- a partial pass, which is exactly what abort-don't-trim exists to prevent.
EXPECTED_COMPLETION_TOKENS = 1300


# Consecutive failed runs before a posting stops being offered.
#
# 3, not 1: the graph already spends MAX_ATTEMPTS within a run, so one failed run is
# usually a real transient -- a 429 storm hits every posting in flight and would quarantine
# the whole batch at 1. Three separate runs failing the same posting is not a draw.
#
# Each run costs up to MAX_ATTEMPTS calls, so this bounds a permanently-broken posting at
# ~9 calls total rather than 3 a run forever.
MAX_SCORING_FAILURES = 3


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


# The eligibility half of the backlog predicate, in one place.
#
# `_select` and `_pending` must ask the same question. They did not: the closing counter
# read `pipeline_state = 'new'`, which counts rows `_select` can never return -- a
# duplicate, a stub description, a posting a re-scrape could not find. So it reported a
# backlog no run could drain and that never reached zero, while the postings actually
# queued (a stale `verdict_schema_version` on a row already marked 'scored') went unnamed.
_ELIGIBLE = """
      FROM jobs j
      LEFT JOIN v_job_liveness l ON l.job_id = j.id
     WHERE j.duplicate_of IS NULL
       AND j.description IS NOT NULL AND length(j.description) > 200
       AND COALESCE(j.scoring_failures, 0) < {max_failures}
       {live_clause}
"""

# The "needs a verdict under this profile" half. Separate because --rescore-all drops it.
_NO_VERDICT = """
       AND NOT EXISTS (
             SELECT 1 FROM job_verdicts v
              WHERE v.job_id = j.id AND v.profile_version = ?
                AND COALESCE(v.verdict_schema_version, 1) >= ?
           )
"""


def _live_clause(include_closed: bool) -> str:
    return "" if include_closed else "AND COALESCE(l.liveness, 'unknown') != 'likely_closed'"


def _select(
    db: Database,
    limit: int | None,
    rescore_all: bool,
    profile_version: int,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    """Postings needing a verdict under the active profile.

    `rescore_all` re-scores everything; the default picks up postings with no verdict for
    *this* profile version, which is what makes a profile rebuild resumable -- an
    interrupted re-score continues instead of starting over.

    A verdict stored under an older `verdict_schema_version` counts as missing, so raising
    VERDICT_SCHEMA_VERSION drains the backlog through the same resumable path instead of
    needing `--rescore-all`, which starts over on every interruption.

    Postings the cell has been re-scraped without finding are skipped. Measured on the
    cells scraped in two windows without saturating, roughly half of postings vanish within
    a week; scoring them spends money to rank dead listings at the top of the dashboard.
    """
    where = _ELIGIBLE.format(
        live_clause=_live_clause(include_closed), max_failures=MAX_SCORING_FAILURES
    )

    if rescore_all:
        query = "SELECT j.*" + where + " ORDER BY j.date_found DESC"
        params: list[Any] = []
    else:
        query = "SELECT j.*" + where + _NO_VERDICT + " ORDER BY j.date_found DESC"
        params = [profile_version, VERDICT_SCHEMA_VERSION]

    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in db.conn.execute(query, params)]


def _pending(db: Database, profile_version: int, include_closed: bool = False) -> int:
    """How many postings a *next* run would actually select.

    Always the incremental predicate, even after --rescore-all: "what is left to do" is
    the backlog, not whatever this run chose to redo.
    """
    sql = (
        "SELECT COUNT(*)"
        + _ELIGIBLE.format(
            live_clause=_live_clause(include_closed), max_failures=MAX_SCORING_FAILURES
        )
        + _NO_VERDICT
    )
    return db.conn.execute(sql, [profile_version, VERDICT_SCHEMA_VERSION]).fetchone()[0]


def _ineligible(db: Database) -> sqlite3.Row:
    """Postings left in 'new' that no run will ever select, by reason.

    Reported rather than hidden, on the same principle as the analytics gates: suppress
    with a reason, never silently omit. Otherwise they read as pending work forever.

    The reasons are precedence-ordered -- duplicate, then thin, then closed -- so they sum
    to the total. Counted independently they would not: a posting is routinely two of the
    three at once. Under this order `closed` means "unique, substantive, and dead", which
    is the only one of the three that a future scrape can change.
    """
    return db.conn.execute("""
        SELECT
          COUNT(*) AS total,
          SUM(j.duplicate_of IS NOT NULL) AS duplicate,
          SUM(j.duplicate_of IS NULL
              AND (j.description IS NULL OR length(j.description) <= 200)) AS thin,
          SUM(j.duplicate_of IS NULL
              AND j.description IS NOT NULL AND length(j.description) > 200
              AND COALESCE(l.liveness, 'unknown') = 'likely_closed') AS closed
          FROM jobs j
          LEFT JOIN v_job_liveness l ON l.job_id = j.id
         WHERE j.pipeline_state = 'new'
           -- Only the rows that are actually ineligible. `pipeline_state = 'new'` alone
           -- also holds freshly scraped postings that ARE queued, and counting those here
           -- would report them twice: once as remaining, once as never scoreable.
           AND (j.duplicate_of IS NOT NULL
                OR j.description IS NULL OR length(j.description) <= 200
                OR COALESCE(l.liveness, 'unknown') = 'likely_closed')
    """).fetchone()


def _record_failure(db: Database, job_id: int, error: str | BaseException | None) -> None:
    """Count a failed run against the posting, with the reason that failed it.

    Counts RUNS, not attempts: the graph's three tries inside one run are one draw at the
    same conditions, so counting each would quarantine after a single rate-limit storm.
    """
    db.conn.execute(
        "UPDATE jobs SET scoring_failures = COALESCE(scoring_failures, 0) + 1, "
        "last_scoring_error = ?, last_scoring_failure_at = ? WHERE id = ?",
        (str(error)[:500] if error else None, _now(), job_id),
    )


def _quarantined(db: Database) -> list[sqlite3.Row]:
    """Postings withdrawn from the queue after failing MAX_SCORING_FAILURES runs.

    Grouped by the error that stopped them, because the useful question is never "which
    posting" but "what keeps breaking" -- one bad prompt rule and one unparseable posting
    look identical in a count and nothing alike in a listing.
    """
    return db.conn.execute(
        "SELECT COUNT(*) AS total, "
        "       MIN(id) AS example, "
        "       COALESCE(substr(last_scoring_error, 1, 60), '(unrecorded)') AS reason "
        "  FROM jobs WHERE COALESCE(scoring_failures, 0) >= ? "
        " GROUP BY reason ORDER BY total DESC",
        (MAX_SCORING_FAILURES,),
    ).fetchall()


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
    """Write the ordinals AND the projection.

    Both, deliberately. The ordinals are the record -- `careerradar score rescale`
    recomputes the number from them without an API call, which is what makes the scale
    cheap to change. The projection is stored alongside so that SQL can sort and threshold
    without importing Python, and `scale_version` says which table produced it.
    """
    db.conn.execute(
        """
        INSERT INTO job_verdicts
            (job_id, profile_version, model, fit_score, verdict, seniority_fit,
             hard_blockers, key_gaps, strengths, reasoning, research_worthy,
             tokens_in, tokens_cached, tokens_out, cost_usd, created_at,
             role_summary, eligibility, role_match, capability_match, seniority_gap,
             evidence_quality, core_requirements, requirement_assessments, audit_flags,
             scale_version, verdict_schema_version, pareto_tier, prompt_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, profile_version) DO UPDATE SET
            model = excluded.model,
            fit_score = excluded.fit_score,
            verdict = excluded.verdict,
            seniority_fit = excluded.seniority_fit,
            hard_blockers = excluded.hard_blockers,
            key_gaps = excluded.key_gaps,
            strengths = excluded.strengths,
            reasoning = excluded.reasoning,
            research_worthy = excluded.research_worthy,
            tokens_in = excluded.tokens_in,
            tokens_cached = excluded.tokens_cached,
            tokens_out = excluded.tokens_out,
            cost_usd = excluded.cost_usd,
            created_at = excluded.created_at,
            role_summary = excluded.role_summary,
            eligibility = excluded.eligibility,
            role_match = excluded.role_match,
            capability_match = excluded.capability_match,
            seniority_gap = excluded.seniority_gap,
            evidence_quality = excluded.evidence_quality,
            core_requirements = excluded.core_requirements,
            requirement_assessments = excluded.requirement_assessments,
            audit_flags = excluded.audit_flags,
            scale_version = excluded.scale_version,
            verdict_schema_version = excluded.verdict_schema_version,
            pareto_tier = excluded.pareto_tier,
            prompt_hash = excluded.prompt_hash
        """,
        (
            job["id"],
            profile_version,
            model,
            verdict["fit_score"],
            verdict["verdict"],
            # `seniority_fit` is the v1 column name and keeps its meaning; the new value is
            # explicit about whose level it describes.
            verdict["seniority_gap"],
            json.dumps(verdict["hard_blockers"]),
            json.dumps(verdict["key_gaps"]),
            json.dumps(verdict["strengths"]),
            verdict["reasoning"],
            1 if verdict["research_worthy"] else 0,
            usage["prompt"] if usage else None,
            usage["cache_hit"] if usage else None,
            usage["completion"] if usage else None,
            cost,
            _now(),
            verdict["role_summary"],
            verdict["eligibility"],
            verdict["role_match"],
            verdict["capability_match"],
            verdict["seniority_gap"],
            verdict["evidence_quality"],
            json.dumps(verdict["core_requirements"]),
            json.dumps(verdict["requirement_assessments"]),
            json.dumps(verdict.get("audit_flags") or []),
            verdict["scale_version"],
            VERDICT_SCHEMA_VERSION,
            verdict["pareto_tier"],
            phash,
        ),
    )
    # The failure counter is cleared here rather than by the caller: a posting that just
    # produced a verdict is not failing, and tying the reset to the write that proves it
    # means no future success path can forget to do it.
    db.conn.execute(
        "UPDATE jobs SET fit_score = ?, scored_at = ?, pipeline_state = 'scored', "
        "scoring_failures = 0, last_scoring_error = NULL WHERE id = ?",
        (verdict["fit_score"], _now(), job["id"]),
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
        if job_id is not None:
            cursor = db.conn.execute(
                "UPDATE jobs SET scoring_failures = 0, last_scoring_error = NULL WHERE id = ?",
                (job_id,),
            )
        else:
            cursor = db.conn.execute(
                "UPDATE jobs SET scoring_failures = 0, last_scoring_error = NULL "
                "WHERE COALESCE(scoring_failures, 0) > 0"
            )
        db.conn.commit()
        print(
            f"cleared the failure counter on {cursor.rowcount:,} posting(s); "
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

        system = build_system(summary)
        phash = prompt_hash()
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
        results = {"scored": 0, "failed": 0}
        first_usage = []
        flagged = {}

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
                usage = state.get("usage")
                if verdict is None:
                    results["failed"] += 1
                    _record_failure(db, job["id"], state.get("error"))
                    logger.warning(
                        "No verdict for job %s (%s): %s",
                        job["id"],
                        job["title"],
                        state.get("error"),
                    )
                    continue

                cost = usage_cost(model, usage) if usage else 0.0
                spend.usd += cost
                spend.calls += 1
                if usage:
                    spend.tokens_in += usage["prompt"]
                    spend.tokens_cached += usage["cache_hit"]
                    spend.tokens_out += usage["completion"]
                    if len(first_usage) < 4:
                        first_usage.append(usage)

                _persist(db, job, verdict, usage, cost, model, profile_version, phash)
                results["scored"] += 1
                for flag in verdict.get("audit_flags") or []:
                    flagged[flag["flag"]] = flagged.get(flag["flag"], 0) + 1

                if results["scored"] % 25 == 0:
                    db.conn.commit()
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
        print(
            f"tokens:     {summary_stats['tokens_in']:,} in "
            f"({summary_stats['tokens_cached']:,} cached, "
            f"{summary_stats['cache_rate']:.0%}) / "
            f"{summary_stats['tokens_out']:,} out"
        )

        # The regression check from docs/deepseek.md. A zero cache rate across a
        # multi-posting run means something per-posting leaked into the system half, and
        # the run just cost ~50x what it should have on input.
        if len(first_usage) > 1 and summary_stats["cache_rate"] < 0.1:
            logger.warning(
                "Prefix cache hit rate is %.0f%% across %d calls. Something per-posting "
                "is leaking into the system prompt -- see docs/deepseek.md.",
                summary_stats["cache_rate"] * 100,
                summary_stats["calls"],
            )

        if flagged:
            print()
            print("audit flags (see `careerradar score audit` for detail)")
            for name, count in sorted(flagged.items(), key=lambda kv: -kv[1]):
                marker = (
                    "  <-- a blocker the candidate does not actually have"
                    if name == "blocker_contradicts_profile"
                    else ""
                )
                print(f"  {name:<32} {count:>6}{marker}")

        print(f"remaining:  {_pending(db, profile_version):,} unscored")
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
