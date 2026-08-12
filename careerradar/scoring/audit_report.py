"""Re-run the auditor over stored verdicts. No API calls.

The point of this command is that it turns "the model sometimes invents a blocker" into a
number you can watch move. Both checks are objective -- a quote is or is not in the
posting, and a blocker either does or does not demand something the profile says the
candidate has -- so this is a correctness measurement, not a heuristic, and it needs no
labelled data to produce.

It reads the stored description rather than re-rendering the prompt, so it also works on
verdicts written before the auditor existed. That is how the first measurement was taken:
across 9,053 stored blockers, 89.6% of quotes were verifiable and roughly 10% could not be
found in the posting at all.
"""

import json

from careerradar.core.database import Database
from careerradar.profile.adapter import load_profile
from careerradar.scoring.audit import blocker_contradicts_profile, locate_blocker
from careerradar.scoring.prompts import MAX_DESCRIPTION_CHARS


def collect(profile_version=None, limit=None, db=None):
    owned = db is None
    db = db or Database()
    try:
        if profile_version is None:
            row = db.conn.execute(
                "SELECT version FROM profiles WHERE is_active = 1"
            ).fetchone()
            if row is None:
                return None
            profile_version = row[0]

        profile = load_profile(db=db)
        query = """
            SELECT v.job_id, v.hard_blockers, v.eligibility, v.core_requirements,
                   j.title, j.company, j.location, j.description
              FROM job_verdicts v JOIN jobs j ON j.id = v.job_id
             WHERE v.profile_version = ?
               AND j.description IS NOT NULL
             ORDER BY v.job_id
        """
        params = [profile_version]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = db.conn.execute(query, params).fetchall()

        quote_status = {}
        contradictions = []
        blockers_total = 0

        for row in rows:
            seen = "\n".join([
                row["title"] or "", row["company"] or "", row["location"] or "",
                (row["description"] or "")[:MAX_DESCRIPTION_CHARS],
            ])
            for blocker in _decode(row["hard_blockers"]):
                blockers_total += 1
                # `locate_blocker`, not `locate`: the auditor is what decides whether
                # a verdict lives, and it reads a blocker as prose that may be wrapped
                # around its quote. Reporting the stricter whole-string check made the
                # command understate the rate the pipeline actually enforces, which is
                # the opposite of what a watched number should do.
                status, _span = locate_blocker(blocker, seen)
                quote_status[status] = quote_status.get(status, 0) + 1
                for hit in blocker_contradicts_profile(blocker, profile):
                    contradictions.append({
                        "job_id": row["job_id"], "title": row["title"],
                        "eligibility": row["eligibility"], **hit,
                    })

        return {
            "profile_version": profile_version,
            "verdicts": len(rows),
            "blockers": blockers_total,
            "quote_status": quote_status,
            "contradictions": contradictions,
        }
    finally:
        if owned:
            db.close()


def _decode(raw):
    """The quote text of each stored blocker, whichever shape the row is in.

    Migration v7 rewrote blockers as `{"quote": ..., "why": ...}`, but this command is the
    one place that reads verdicts written by every version of the scorer there has ever
    been -- that is how the first quote-status measurement was taken. Reducing both shapes
    to the quote here keeps the caller from having to know which it got, and keeps the
    rates comparable across the change: a migrated row's quote is the old string.
    """
    if not raw or raw == "[]":
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return [raw]
    if not isinstance(decoded, list):
        return [str(decoded)]
    return [item.get("quote") or "" if isinstance(item, dict) else str(item)
            for item in decoded]


def render(report, show=20):
    if report is None:
        return "No active profile. Build one with:  careerradar profile build"
    if not report["blockers"]:
        return (f"profile v{report['profile_version']}: "
                f"{report['verdicts']:,} verdicts, no hard blockers to check.")

    total = report["blockers"]
    status = report["quote_status"]
    verifiable = status.get("verified", 0) + status.get("repaired", 0)

    out = [f"profile v{report['profile_version']} · {report['verdicts']:,} verdicts · "
           f"{total:,} hard blockers", ""]
    out.append("quote verification -- is the phrase actually in the posting?")
    for name in ("verified", "repaired", "too_short", "not_found"):
        n = status.get(name, 0)
        out.append(f"  {name:<12} {n:>6}  ({n / total:5.1%})")
    out.append(f"  {'verifiable':<12} {verifiable:>6}  ({verifiable / total:5.1%})")

    contradictions = report["contradictions"]
    out.append("")
    out.append("blockers demanding something the profile says the candidate HAS")
    out.append("  -- the most expensive error in the pipeline: a fabricated blocker")
    out.append("     deletes an opportunity and the candidate never learns it existed")
    out.append(f"  count: {len(contradictions)}")
    for hit in contradictions[:show]:
        out.append(f"    job {hit['job_id']:>6}  [{hit['field']}={hit['value']}]  "
                   f"{hit['quote'][:70]}")
    if len(contradictions) > show:
        out.append(f"    ... and {len(contradictions) - show} more")
    return "\n".join(out)


def run_audit(profile_version=None, limit=None):
    print(render(collect(profile_version, limit=limit)))
    return 0
