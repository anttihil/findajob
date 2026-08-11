"""Drain unscored postings through the scoring graph.

Runs on its own timer, independent of the scraper. A rate-limited job board cannot stall
scoring of the backlog, and a bad API key cannot lose a scrape -- that separation is the
whole reason the stages hand off through `jobs.pipeline_state` rather than sharing a
process.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
from careerradar.profile.adapter import NoActiveProfile
from careerradar.profile.store import load_active
from careerradar.scoring.graph import build_graph
from careerradar.scoring.prompts import build_system, render_posting

logger = get_logger()

# Chars per token. DeepSeek publishes no token-counting endpoint, so the pre-flight gate
# uses a ratio measured against the spike's real responses (1,460-2,016 prompt tokens for
# postings of ~4,700-6,500 chars). Deliberately on the low side: an estimate that runs
# high aborts a run that would have been affordable, which is the cheaper mistake.
CHARS_PER_TOKEN = 3.0
# Measured completion length: verdicts carry quoted blockers and reasoning, and came in at
# 562-670 tokens across the spike. The plan's original 300 was half the truth.
EXPECTED_COMPLETION_TOKENS = 650


def _now():
    return datetime.now(timezone.utc).isoformat()


def _select(db, limit, rescore_all, profile_version):
    """Postings needing a verdict under the active profile.

    `rescore_all` re-scores everything; the default only picks up postings with no verdict
    for *this* profile version, which is what makes a profile rebuild resumable -- an
    interrupted re-score continues instead of starting over.
    """
    if rescore_all:
        query = """
            SELECT j.* FROM jobs j
             WHERE j.duplicate_of IS NULL
               AND j.description IS NOT NULL AND length(j.description) > 200
             ORDER BY j.date_found DESC
        """
        params = []
    else:
        query = """
            SELECT j.* FROM jobs j
             WHERE j.duplicate_of IS NULL
               AND j.description IS NOT NULL AND length(j.description) > 200
               AND NOT EXISTS (
                     SELECT 1 FROM job_verdicts v
                      WHERE v.job_id = j.id AND v.profile_version = ?
                   )
             ORDER BY j.date_found DESC
        """
        params = [profile_version]

    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in db.conn.execute(query, params)]


def _persist(db, job, verdict, usage, cost, model, profile_version):
    db.conn.execute(
        """
        INSERT INTO job_verdicts
            (job_id, profile_version, model, fit_score, verdict, seniority_fit,
             hard_blockers, key_gaps, strengths, reasoning, research_worthy,
             tokens_in, tokens_cached, tokens_out, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            created_at = excluded.created_at
        """,
        (
            job["id"], profile_version, model, verdict["fit_score"], verdict["verdict"],
            verdict["seniority_fit"], json.dumps(verdict["hard_blockers"]),
            json.dumps(verdict["key_gaps"]), json.dumps(verdict["strengths"]),
            verdict["reasoning"], 1 if verdict["research_worthy"] else 0,
            usage["prompt"] if usage else None,
            usage["cache_hit"] if usage else None,
            usage["completion"] if usage else None,
            cost, _now(),
        ),
    )
    db.conn.execute(
        "UPDATE jobs SET fit_score = ?, scored_at = ?, pipeline_state = 'scored' "
        "WHERE id = ?",
        (verdict["fit_score"], _now(), job["id"]),
    )


def run_scoring(limit=None, rescore_all=False, dry_run=False):
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
        if not jobs:
            print("Nothing to score. The backlog is drained.")
            return 0

        system = build_system(summary)
        system_tokens = int(len(system) / CHARS_PER_TOKEN)
        posting_tokens = sum(
            int(len(render_posting(j)) / CHARS_PER_TOKEN) for j in jobs
        )

        # The system half is paid uncached exactly once, then read from cache. Modelling
        # it any other way overstates the bill by ~50x on the cached portion and makes the
        # ceiling useless.
        estimate = (
            estimate_cost(model, system_tokens, 0)
            + estimate_cost(model, posting_tokens, EXPECTED_COMPLETION_TOKENS * len(jobs),
                            cached_tokens=0)
            + estimate_cost(model, system_tokens * (len(jobs) - 1), 0,
                            cached_tokens=system_tokens * (len(jobs) - 1))
        )

        print(f"profile v{profile_version} · model {model}")
        print(f"postings to score: {len(jobs):,}")
        print(f"cached prefix:     {system_tokens:,} tokens")
        print(f"estimated cost:    ${estimate:.4f}")

        if max_usd is not None and estimate > max_usd:
            # Abort rather than trim. Trimming produces a partial pass that looks complete,
            # and every coverage number downstream silently inherits the shortfall.
            print()
            print(f"ABORT: estimate ${estimate:.2f} exceeds scoring.max_usd_per_run "
                  f"${max_usd:.2f}.")
            print("Raise the ceiling in config.yaml, or scope the run with --limit.")
            return 1

        if dry_run:
            print("\n(dry run -- nothing scored, nothing written)")
            return 0

        graph = build_graph()
        spend = Spend(model, max_usd=max_usd)
        results = {"scored": 0, "failed": 0}
        first_usage = []

        def score_one(job):
            return job, graph.invoke({
                "system": system, "posting": job, "model": model,
            })

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for job, state in pool.map(score_one, jobs):
                verdict = state.get("verdict")
                usage = state.get("usage")
                if verdict is None:
                    results["failed"] += 1
                    logger.warning("No verdict for job %s (%s): %s",
                                   job["id"], job["title"], state.get("error"))
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

                _persist(db, job, verdict, usage, cost, model, profile_version)
                results["scored"] += 1

                if results["scored"] % 25 == 0:
                    db.conn.commit()
                    print(f"  {results['scored']:,}/{len(jobs):,} scored  "
                          f"${spend.usd:.4f}")

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
        print(f"tokens:     {summary_stats['tokens_in']:,} in "
              f"({summary_stats['tokens_cached']:,} cached, "
              f"{summary_stats['cache_rate']:.0%}) / "
              f"{summary_stats['tokens_out']:,} out")

        # The regression check from docs/deepseek.md. A zero cache rate across a
        # multi-posting run means something per-posting leaked into the system half, and
        # the run just cost ~50x what it should have on input.
        if len(first_usage) > 1 and summary_stats["cache_rate"] < 0.1:
            logger.warning(
                "Prefix cache hit rate is %.0f%% across %d calls. Something per-posting "
                "is leaking into the system prompt -- see docs/deepseek.md.",
                summary_stats["cache_rate"] * 100, summary_stats["calls"],
            )

        row = db.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_state = 'new'"
        ).fetchone()
        print(f"remaining:  {row[0]:,} unscored")
        return 0
    except MissingApiKey as exc:
        logger.error(str(exc))
        return 1
    except NoActiveProfile as exc:
        logger.error(str(exc))
        return 1
    finally:
        db.close()
