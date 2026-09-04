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

VERDICT COVERAGE IS THE ONE TO READ FIRST. Every hit-rate figure in the project -- the target
roles table included -- is a ratio over SCORED postings. When one source is scored
at 67% and another at 27%, those ratios describe the scored subset and not the market, and
no amount of care downstream repairs it.
"""

from datetime import datetime
from typing import Any

from careerradar.core import status_repository as status_repo
from careerradar.core.database import Database

STALL_MULTIPLE = status_repo.STALL_MULTIPLE
COVERAGE_SKEW = status_repo.COVERAGE_SKEW
_hours_since = status_repo.hours_since


def collect(db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    owned = db is None
    db = db or Database()
    try:
        return status_repo.collect_status_report(db.conn, now=now)
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
        out.append("  ! scoring looks stalled -- check: systemctl status careerradar")

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
        label = "active" if row.get("active") else (row.get("tier") or "active")
        out.append(f"  {label:<9} {row['cells']:>4} cells, oldest success {age}{suffix}")

    if report["quarantined"]:
        out += ["", f"QUARANTINED  ({len(report['quarantined'])} shown, 3+ failures)"]
        for row in report["quarantined"]:
            out.append(f"  #{row['id']} {row['title'][:48]}")
            out.append(f"      {(row['error'] or '')[:96]}")
        out.append("  retry with:  careerradar score retry")

    taxonomy = report["taxonomy"]
    out += ["", "TAXONOMY"]
    out.append(f"  skills {taxonomy['skills_hash']}   roles {taxonomy['roles_hash']}")
    for stored_hash, count in taxonomy["stored"][:3]:
        mark = "" if stored_hash == taxonomy["skills_hash"] else "  (stale)"
        out.append(f"  {count:>6} postings classified under {stored_hash}{mark}")
    out.append("  jobs.taxonomy_hash is deprecated (tracks profile_version and prompt_hash)")
    return "\n".join(out)


def run_status(as_json: bool = False) -> int:
    report = collect()
    if as_json:
        import json

        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0
