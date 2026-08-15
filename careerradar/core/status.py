"""One operational health report for the whole pipeline. Reads the database, calls nothing.

Exists because the four stages fail independently and silently. Each has its own timer, so
"scoring stopped a week ago" looks exactly like "scoring is keeping up" from anywhere else
in the system -- the dashboard shows postings either way. The failure that motivated this
was precisely that: careerradar-score.timer sat disabled on the production host while 62%
of the corpus went unjudged, and every hit rate computed off that corpus was quietly
skewed toward whatever HAD been scored.

So the report answers the questions that were unanswerable without an SSH session and a
sqlite prompt:

    is each stage still running, and how long since it last did anything
    how much work is queued, and how old is the oldest piece of it
    is verdict coverage even across sources, or are the analytics reading one board
    are cells being revisited inside their tier's cadence
    is anything quarantined after repeated failures
    do the stored postings agree with the taxonomy on disk

VERDICT COVERAGE IS THE ONE TO READ FIRST. Every hit-rate figure in the project -- the tier
table in roles.yaml included -- is a ratio over SCORED postings. When one source is scored
at 67% and another at 27%, those ratios describe the scored subset and not the market, and
no amount of care downstream repairs it.
"""

from datetime import datetime, timezone
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.database import Database

# A stage quiet for longer than this many times its expected interval is called stalled.
# Two rather than one, because a single missed timer firing is normal and a report that
# cries about it gets ignored.
STALL_MULTIPLE = 2.0

# Verdict coverage this far apart between two sources means family hit rates are describing
# the better-scored source. Chosen off the observed 67%/27% split, which was enough to move
# ai_engineer's apparent hit rate by 4x.
COVERAGE_SKEW = 0.20


def _hours_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 3600.0


def collect(db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    owned = db is None
    db = db or Database()
    now = now or datetime.now(timezone.utc)
    config = load_config()
    scraper = config.get("scraper", {}) or {}
    cadence = scraper.get("cadence_hours") or {}
    try:
        conn = db.conn
        report: dict[str, Any] = {"generated_at": now.isoformat()}

        # -- stages ---------------------------------------------------------------------
        run = conn.execute(
            "SELECT started_at, mode, status, cells_planned, cells_succeeded, postings_new "
            "FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        runs_per_day = (
            conn.execute(
                "SELECT COUNT(*) FROM sync_runs WHERE started_at >= datetime('now', '-7 days')"
            ).fetchone()[0]
            / 7.0
        )
        report["search"] = {
            "last_run": run["started_at"] if run else None,
            "hours_since": _hours_since(run["started_at"], now) if run else None,
            "status": run["status"] if run else None,
            "cells": (run["cells_succeeded"], run["cells_planned"]) if run else None,
            "postings_new": run["postings_new"] if run else None,
            "runs_per_day_7d": round(runs_per_day, 1),
        }

        verdict = conn.execute("SELECT MAX(created_at) FROM job_verdicts").fetchone()[0]
        backlog, oldest_new = conn.execute(
            "SELECT COUNT(*), MIN(date_found) FROM jobs WHERE pipeline_state = 'new'"
        ).fetchone()
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        report["score"] = {
            "last_verdict": verdict,
            "hours_since": _hours_since(verdict, now),
            "backlog": backlog,
            "backlog_share": (backlog / total_jobs) if total_jobs else 0.0,
            "oldest_unscored": oldest_new,
            "oldest_unscored_days": (_hours_since(oldest_new, now) or 0) / 24.0,
        }

        research = conn.execute(
            "SELECT started_at, status, companies FROM research_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        report["research"] = {
            "last_run": research["started_at"] if research else None,
            "hours_since": _hours_since(research["started_at"], now) if research else None,
            "status": research["status"] if research else None,
            "dossiers": conn.execute("SELECT COUNT(*) FROM company_dossiers").fetchone()[0],
        }

        # -- verdict coverage, by source ---------------------------------------------------
        report["coverage"] = [
            {
                "source": row["source"] or "unknown",
                "postings": row["postings"],
                "scored": row["scored"],
                "share": (row["scored"] / row["postings"]) if row["postings"] else 0.0,
            }
            for row in conn.execute(
                "SELECT j.source, COUNT(*) AS postings, "
                "SUM(CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END) AS scored "
                "FROM jobs j LEFT JOIN job_verdicts v ON v.job_id = j.id "
                "GROUP BY j.source ORDER BY postings DESC"
            )
        ]

        # -- cells ------------------------------------------------------------------------
        cells = []
        for row in conn.execute(
            "SELECT tier, COUNT(*) AS cells, "
            "SUM(CASE WHEN last_success_at IS NULL THEN 1 ELSE 0 END) AS never, "
            "MAX(last_success_at) AS newest, MIN(last_success_at) AS oldest, "
            "SUM(CASE WHEN consecutive_error > 0 THEN 1 ELSE 0 END) AS erroring, "
            "SUM(CASE WHEN backoff_until IS NOT NULL THEN 1 ELSE 0 END) AS backed_off "
            "FROM scrape_cells WHERE enabled = 1 GROUP BY tier"
        ):
            oldest_hours = _hours_since(row["oldest"], now)
            tier_cadence = cadence.get(row["tier"], 168)
            cells.append(
                {
                    "tier": row["tier"],
                    "cells": row["cells"],
                    "never_scraped": row["never"],
                    "oldest_success_hours": oldest_hours,
                    "cadence_hours": tier_cadence,
                    "stale": bool(oldest_hours and oldest_hours > tier_cadence * STALL_MULTIPLE),
                    "erroring": row["erroring"],
                    "backed_off": row["backed_off"],
                }
            )
        report["cells"] = cells

        # -- quarantine ---------------------------------------------------------------------
        report["quarantined"] = [
            {"id": row["id"], "title": row["title"], "error": row["last_scoring_error"]}
            for row in conn.execute(
                "SELECT id, title, last_scoring_error FROM jobs WHERE scoring_failures >= 3 "
                "ORDER BY last_scoring_failure_at DESC LIMIT 10"
            )
        ]

        # -- taxonomy drift -----------------------------------------------------------------
        from careerradar.taxonomy.roles import load_roles
        from careerradar.taxonomy.skills import load_taxonomy

        stored = [
            (row["taxonomy_hash"], row["n"])
            for row in conn.execute(
                "SELECT taxonomy_hash, COUNT(*) AS n FROM jobs WHERE taxonomy_hash IS NOT NULL "
                "GROUP BY taxonomy_hash ORDER BY n DESC"
            )
        ]
        report["taxonomy"] = {
            "skills_hash": load_taxonomy().hash,
            "roles_hash": load_roles().hash,
            "stored": stored,
        }
        return report
    finally:
        if owned:
            db.close()


def render(report: dict[str, Any]) -> str:
    out: list[str] = ["CareerRadar status", ""]

    search = report["search"]
    since = search["hours_since"]
    out.append("SEARCH")
    if search["last_run"] is None:
        out.append("  never run")
    else:
        cells = search["cells"] or (0, 0)
        out.append(
            f"  last run {since:.1f}h ago ({search['status']}), "
            f"{cells[0]}/{cells[1]} cells, {search['postings_new']} new"
        )
        out.append(f"  {search['runs_per_day_7d']} runs/day over the last 7 days")

    score = report["score"]
    out += ["", "SCORE"]
    if score["last_verdict"] is None:
        out.append("  no verdicts stored")
    else:
        out.append(f"  last verdict {score['hours_since']:.1f}h ago")
    out.append(
        f"  backlog {score['backlog']} unscored ({score['backlog_share']:.0%} of the corpus)"
    )
    if score["oldest_unscored"]:
        out.append(f"  oldest unscored posting is {score['oldest_unscored_days']:.0f} days old")
    # The stall test is on the SCORER, not on the queue: a large backlog with a recent
    # verdict is a budget question, a small backlog with no recent verdict is a broken timer.
    if score["hours_since"] is None or score["hours_since"] > 24 * STALL_MULTIPLE:
        out.append("  ! scoring looks stalled -- check: systemctl status careerradar-score.timer")

    research = report["research"]
    out += ["", "RESEARCH"]
    if research["last_run"] is None:
        out.append("  never run")
    else:
        out.append(f"  last run {research['hours_since']:.1f}h ago ({research['status']})")
    out.append(f"  {research['dossiers']} dossiers stored")

    out += ["", "VERDICT COVERAGE  (every hit rate in this project is a ratio over these)"]
    shares = []
    for row in report["coverage"]:
        out.append(
            f"  {row['source'][:14]:<14} {row['scored']:>6} /{row['postings']:>6} scored"
            f"   {row['share']:>4.0%}"
        )
        if row["postings"] >= 100:
            shares.append(row["share"])
    if shares and (max(shares) - min(shares)) >= COVERAGE_SKEW:
        out.append(
            f"  ! {max(shares) - min(shares):.0%} coverage spread between sources -- "
            "family hit rates describe the better-scored source, not the market"
        )

    out += ["", "CELLS"]
    for row in report["cells"]:
        oldest = row["oldest_success_hours"]
        age = f"{oldest / 24:.1f}d" if oldest is not None else "never"
        flags = []
        if row["never_scraped"]:
            flags.append(f"{row['never_scraped']} never scraped")
        if row["erroring"]:
            flags.append(f"{row['erroring']} erroring")
        if row["backed_off"]:
            flags.append(f"{row['backed_off']} backed off")
        if row["stale"]:
            flags.append(f"oldest is past {row['cadence_hours']}h cadence")
        suffix = f"   {', '.join(flags)}" if flags else ""
        out.append(f"  {row['tier']:<9} {row['cells']:>4} cells, oldest success {age}{suffix}")

    if report["quarantined"]:
        out += ["", f"QUARANTINED  ({len(report['quarantined'])} shown, 3+ failures)"]
        for row in report["quarantined"]:
            out.append(f"  #{row['id']} {row['title'][:48]}")
            out.append(f"      {(row['error'] or '')[:96]}")
        out.append("  retry with:  careerradar score retry")

    taxonomy = report["taxonomy"]
    out += ["", "TAXONOMY"]
    out.append(f"  skills.yaml {taxonomy['skills_hash']}   roles.yaml {taxonomy['roles_hash']}")
    for stored_hash, count in taxonomy["stored"][:3]:
        mark = "" if stored_hash == taxonomy["skills_hash"] else "  (stale)"
        out.append(f"  {count:>6} postings classified under {stored_hash}{mark}")
    # Worth stating plainly, because the column name invites the wrong reading: jobs
    # record which SKILLS taxonomy classified them and nothing about which roles.yaml did.
    # A role-pattern edit is therefore invisible here, and `--rescore-only` does not
    # re-derive role_family either, so pattern edits only ever reach postings scraped after
    # the edit landed.
    out.append("  jobs.taxonomy_hash tracks skills.yaml only; roles.yaml drift is not recorded")
    return "\n".join(out)


def run_status(as_json: bool = False) -> int:
    report = collect()
    if as_json:
        import json

        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0
