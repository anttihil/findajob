"""Distribution observability for stored verdicts. Reads the database, calls nothing.

Exists because a scoring agent fails quietly. A prompt that makes the model reluctant to
use the top of its scale produces verdicts that are individually defensible and
collectively useless -- every posting lands in one or two bands, and the ranking the whole
pipeline depends on flattens. Nothing in a single verdict shows that; only the shape of
several thousand does.
"""

from careerradar.core.database import Database

# The bands as `scoring/prompts.py` states them to the model. Duplicated deliberately:
# this module's job is to check whether stored verdicts match what the prompt asked for,
# and importing the prompt's own numbers to grade the prompt would assume the answer.
BANDS = [
    ("strong", 80, 100),
    ("worth_applying", 60, 79),
    ("stretch", 40, 59),
    ("poor_fit", 20, 39),
    ("mismatch", 0, 19),
]


def band_for(score):
    for name, low, high in BANDS:
        if low <= score <= high:
            return name
    return None


def boundary_cliff(counts, boundary, width=4):
    """Is the model stepping over a band boundary rather than crossing it?

    Compares the `width` scores below a boundary with the `width` at and above it. A model
    using its scale continuously produces comparable mass on both sides; one avoiding the
    upper band empties the top while piling up just underneath. Reported as a ratio so the
    caller decides what is suspicious -- there is no threshold here that is not arbitrary.
    """
    below = sum(counts.get(s, 0) for s in range(boundary - width, boundary))
    at_or_above = sum(counts.get(s, 0) for s in range(boundary, boundary + width))
    return {
        "boundary": boundary,
        "below": below,
        "at_or_above": at_or_above,
        "empty_scores": [s for s in range(boundary - 1, boundary + width)
                         if counts.get(s, 0) == 0],
    }


def collect(profile_version=None, db=None):
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

        rows = db.conn.execute(
            "SELECT fit_score, verdict, hard_blockers, cost_usd "
            "FROM job_verdicts WHERE profile_version = ?",
            (profile_version,),
        ).fetchall()
        if not rows:
            return {"profile_version": profile_version, "total": 0}

        counts, verdicts = {}, {}
        mislabelled = blockers = blockers_not_mismatch = 0
        cost = 0.0
        for score, verdict, hard_blockers, row_cost in rows:
            counts[score] = counts.get(score, 0) + 1
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            cost += row_cost or 0.0
            if band_for(score) != verdict:
                mislabelled += 1
            if hard_blockers and hard_blockers != "[]":
                blockers += 1
                if verdict != "mismatch":
                    blockers_not_mismatch += 1

        return {
            "profile_version": profile_version,
            "total": len(rows),
            "counts": counts,
            "verdicts": verdicts,
            "distinct_scores": len(counts),
            "mislabelled": mislabelled,
            "blockers": blockers,
            "blockers_not_mismatch": blockers_not_mismatch,
            "cost": cost,
            "cliffs": [boundary_cliff(counts, low)
                       for _name, low, _high in BANDS if low > 0],
        }
    finally:
        if owned:
            db.close()


def render(stats, width=48):
    if stats is None:
        return "No active profile. Build one with:  careerradar profile build"
    if not stats.get("total"):
        return f"No verdicts stored for profile v{stats['profile_version']}."

    total = stats["total"]
    out = [f"profile v{stats['profile_version']} · {total:,} verdicts · "
           f"${stats['cost']:.4f} · {stats['distinct_scores']} distinct scores", ""]

    peak = max(
        sum(stats["counts"].get(s, 0) for s in range(low, low + 10))
        for _n, low, _h in [(n, l - l % 10, h) for n, l, h in BANDS]
    ) or 1
    out.append("score distribution (10-point buckets)")
    for low in range(0, 100, 10):
        n = sum(stats["counts"].get(s, 0) for s in range(low, low + 10))
        bar = "#" * max(1, round(n / peak * width)) if n else ""
        out.append(f"  {low:>3}-{low + 9:<3} {n:>6}  {bar}")

    out.append("")
    out.append("bands")
    for name, low, high in BANDS:
        n = stats["verdicts"].get(name, 0)
        out.append(f"  {name:<15} {n:>6}  ({n / total:5.1%})   scores {low}-{high}")

    out.append("")
    out.append("boundary use -- a band the model steps over rather than into")
    for cliff in stats["cliffs"]:
        empty = ", ".join(str(s) for s in cliff["empty_scores"]) or "none"
        flag = ""
        if cliff["below"] and not cliff["at_or_above"]:
            flag = "   <-- nothing crosses"
        out.append(f"  {cliff['boundary']:>3}: {cliff['below']:>5} just below, "
                   f"{cliff['at_or_above']:>5} at or above   empty: {empty}{flag}")

    out.append("")
    out.append("consistency")
    out.append(f"  label disagrees with its score band:  {stats['mislabelled']:>6}"
               f"  ({stats['mislabelled'] / total:.2%})")
    out.append(f"  verdicts carrying a hard blocker:     {stats['blockers']:>6}"
               f"  ({stats['blockers'] / total:.1%})")
    out.append(f"  ... of those, not scored 'mismatch':  {stats['blockers_not_mismatch']:>6}"
               f"  (the prompt says a blocker means mismatch)")
    return "\n".join(out)


def run_stats(profile_version=None):
    print(render(collect(profile_version)))
    return 0
