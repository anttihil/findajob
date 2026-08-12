"""Distribution observability for stored verdicts. Reads the database, calls nothing.

Exists because a scoring agent fails quietly. A prompt that pushes the model toward one
answer produces verdicts that are individually defensible and collectively useless -- every
posting lands in the same cell, and the ranking the whole pipeline depends on flattens.
Nothing in a single verdict shows that; only the shape of several thousand does.

What is worth looking at changed when the score stopped being something the model emits.

The old report measured band cliffs: whether the model stepped *over* a boundary rather
than into it, which was a real question when it was choosing numbers and could avoid 79,
80 and 81 entirely (it did). It cannot avoid anything now -- `scale.py` computes the number
-- so that check would only ever confirm its own arithmetic. `mislabelled` is gone for the
same reason: the band is derived from the score, so disagreement is impossible by
construction rather than reassuring.

What replaces them is the ordinal marginals and the role_match x capability_match
cross-tab. A collapsed marginal is the new "the model is not using its scale", and it is
far more diagnosable than a flat score histogram: it says WHICH judgement collapsed.
"""

from careerradar.core.database import Database
from careerradar.scoring import rubric, scale

# Below this share of verdicts on a single value, a dimension is not discriminating.
# Advisory: with a corpus that really is mostly mismatches, a lopsided `role_match` is the
# truth rather than a bug, which is why this prints a flag and not an error.
COLLAPSE_SHARE = 0.85


def collect(profile_version=None, scale_version=None, db=None):
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

        query = (
            "SELECT fit_score, verdict, eligibility, role_match, capability_match, "
            "seniority_gap, evidence_quality, pareto_tier, hard_blockers, audit_flags, "
            "COALESCE(scale_version, 0) AS scale_version, cost_usd "
            "FROM job_verdicts WHERE profile_version = ?"
        )
        params = [profile_version]
        if scale_version is not None:
            query += " AND COALESCE(scale_version, 0) = ?"
            params.append(scale_version)
        rows = db.conn.execute(query, params).fetchall()
        if not rows:
            return {"profile_version": profile_version, "total": 0}

        scale_versions = {}
        for row in rows:
            scale_versions[row["scale_version"]] = \
                scale_versions.get(row["scale_version"], 0) + 1

        counts, verdicts, tiers = {}, {}, {}
        marginals = {name: {} for name in rubric.DIMENSIONS}
        crosstab = {}
        blockers = 0
        flags = {}
        cost = 0.0

        for row in rows:
            counts[row["fit_score"]] = counts.get(row["fit_score"], 0) + 1
            verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
            cost += row["cost_usd"] or 0.0
            if row["hard_blockers"] and row["hard_blockers"] != "[]":
                blockers += 1
            if row["pareto_tier"] is not None:
                tiers[row["pareto_tier"]] = tiers.get(row["pareto_tier"], 0) + 1
            for name in rubric.DIMENSIONS:
                value = row[name]
                if value:
                    marginals[name][value] = marginals[name].get(value, 0) + 1
            if row["role_match"] and row["capability_match"]:
                cell = (row["role_match"], row["capability_match"])
                crosstab[cell] = crosstab.get(cell, 0) + 1
            for flag in _decode_flags(row["audit_flags"]):
                flags[flag] = flags.get(flag, 0) + 1

        collapsed = [
            name for name, values in marginals.items()
            if values and max(values.values()) / sum(values.values()) >= COLLAPSE_SHARE
        ]

        return {
            "profile_version": profile_version,
            "total": len(rows),
            "scale_versions": scale_versions,
            "counts": counts,
            "verdicts": verdicts,
            "tiers": tiers,
            "marginals": marginals,
            "crosstab": crosstab,
            "collapsed": collapsed,
            "blockers": blockers,
            "audit_flags": dict(sorted(flags.items(), key=lambda kv: -kv[1])),
            "distinct_scores": len(counts),
            "cost": cost,
        }
    finally:
        if owned:
            db.close()


def _decode_flags(raw):
    import json

    if not raw:
        return []
    try:
        return [entry["flag"] for entry in json.loads(raw) if isinstance(entry, dict)]
    except (ValueError, TypeError, KeyError):
        return []


def render(stats, width=44):
    if stats is None:
        return "No active profile. Build one with:  careerradar profile build"
    if not stats.get("total"):
        return f"No verdicts stored for profile v{stats['profile_version']}."

    total = stats["total"]
    versions = stats["scale_versions"]
    out = []

    if len(versions) > 1:
        # Refuse rather than average. A corpus half-scored under a model-emitted number and
        # half under a computed one has no meaningful joint distribution, and rendering one
        # would invite exactly the comparison the scale_version column exists to prevent.
        listing = ", ".join(f"v{v}: {n:,}" for v, n in sorted(versions.items()))
        return (
            f"profile v{stats['profile_version']} holds verdicts from more than one "
            f"scale ({listing}).\n\n"
            "These are not comparable: scale 0 means the model emitted the number itself.\n"
            "Pick one with  careerradar score stats --scale-version N,\n"
            "or re-score the corpus with  careerradar score run."
        )

    scale_version = next(iter(versions))
    out.append(f"profile v{stats['profile_version']} · scale v{scale_version} · "
               f"{total:,} verdicts · ${stats['cost']:.4f} · "
               f"{stats['distinct_scores']} distinct scores")

    if scale_version == 0:
        out.append("")
        out.append("Scale 0: the model emitted these numbers directly. Ordinals are absent,")
        out.append("so the marginals and cross-tab below are empty by construction.")

    if stats["tiers"]:
        out.append("")
        out.append("pareto tier -- 1 dominates everything below it; equal tiers are ties")
        peak = max(stats["tiers"].values()) or 1
        for tier in range(1, scale.MAX_TIER + 1):
            n = stats["tiers"].get(tier, 0)
            bar = "#" * max(1, round(n / peak * width)) if n else ""
            out.append(f"  {tier:>3}  {n:>6}  {bar}")

    out.append("")
    out.append("ordinal marginals -- a collapsed one is the model not using its scale")
    for name in rubric.DIMENSIONS:
        values = stats["marginals"][name]
        if not values:
            continue
        flag = "   <-- collapsed" if name in stats["collapsed"] else ""
        out.append(f"  {name}{flag}")
        subtotal = sum(values.values())
        for value in rubric.ANCHORS[name][0]:
            n = values.get(value, 0)
            out.append(f"    {value:<18} {n:>6}  ({n / subtotal:5.1%})")

    if stats["crosstab"]:
        out.append("")
        out.append("role_match x capability_match -- which cells the corpus occupies")
        header = "".join(f"{c[:9]:>10}" for c in rubric.CAPABILITY_MATCH)
        out.append(f"  {'':<18}{header}")
        for role in rubric.ROLE_MATCH:
            cells = "".join(
                f"{stats['crosstab'].get((role, cap), 0):>10,}"
                for cap in rubric.CAPABILITY_MATCH
            )
            out.append(f"  {role:<18}{cells}")

    out.append("")
    out.append("bands (a projection of the ordinals, not a measurement)")
    for name, low in scale.BANDS:
        n = stats["verdicts"].get(name, 0)
        out.append(f"  {name:<15} {n:>6}  ({n / total:5.1%})   scores {low}+")

    out.append("")
    out.append("evidence")
    out.append(f"  verdicts carrying a hard blocker:  {stats['blockers']:>6}"
               f"  ({stats['blockers'] / total:.1%})")
    if stats["audit_flags"]:
        for flag, n in stats["audit_flags"].items():
            marker = "   <-- a blocker the candidate does not have" \
                if flag == "blocker_contradicts_profile" else ""
            out.append(f"  {flag:<32} {n:>6}{marker}")
    else:
        out.append("  no audit flags recorded (pre-v2 verdicts carry none)")
    return "\n".join(out)


def run_stats(profile_version=None, scale_version=None):
    print(render(collect(profile_version, scale_version=scale_version)))
    return 0
