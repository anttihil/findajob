"""Ordering and projection over the ordinal tuple. Pure: no DB, no LLM, no clock.

The stored verdict is the tuple, not a number. Everything here is a *view* of it, which is
the whole point: a scalar fit score is a many-to-one map, and a many-to-one map only
destroys information if you throw away the domain. Keep the ordinals in columns and the
score becomes a projection you can change for free -- `careerradar score rescale`
recomputes every row without an API call.

Two views are provided, and they answer different questions.

`pareto_tier` ranks WITHOUT inventing an exchange rate. A weighted score has to assert
something like "two steps of capability equal one step of role_match"; nothing in the data
supports such a rate, and the old 0.45/0.25/0.20/0.10 weights are exactly that assertion
made without evidence. Pareto dominance refuses to make it: A outranks B only when A is at
least as good on every dimension and strictly better on one. Everything else is an honest
tie, broken outside this module by recency.

`fit_score` is the scalar, for the handful of places that genuinely need one number -- a
digest line, a compact badge, a coarse sort. It IS an invented exchange rate, and it is
labelled as such wherever it surfaces. It exists because some consumers cannot take a
partial order, not because the number means more than the tuple.

The tuple space is 4 x 5 x 3 = 60, so both views are precomputed lookup tables built once
at import. Ranking a corpus is a dict hit per row, never a pairwise sweep.
"""

from careerradar.scoring import rubric

# Bumped when any table below changes. Stored on each verdict row so a corpus scored under
# two different scales is detectable rather than silently averaged.
SCALE_VERSION = 1

# The dimensions Pareto dominance is computed over. `eligibility` is deliberately absent:
# it partitions rather than trades off (see `eligibility_rank`). `evidence_quality` is also
# absent -- it says how much to trust the other four, which is a different claim from being
# better or worse, and folding it in would let a confidently-judged bad fit outrank a
# tentatively-judged good one.
PARETO_DIMENSIONS = ("role_match", "capability_match", "seniority_gap")

_RANKS = {name: {value: i for i, value in enumerate(rubric.ANCHORS[name][0])}
          for name in rubric.DIMENSIONS}


def eligibility_rank(eligibility: str) -> int:
    """0 = eligible, 1 = conditional, 2 = blocked. A partition, not a term in a sum.

    Nothing in a lower group outranks anything in a higher one, at any tier. This is what
    makes the stored `fit_score = 100, verdict = mismatch` row -- a real verdict whose
    reasoning named a Swedish-language blocker -- structurally impossible rather than
    merely discouraged by the prompt.
    """
    return _RANKS["eligibility"][eligibility]


def _dominates(a: tuple, b: tuple) -> bool:
    """`a` is at least as good as `b` everywhere and strictly better somewhere.

    Coordinates are rank indices, so LOWER is better.
    """
    return all(x <= y for x, y in zip(a, b)) and a != b


def _build_tiers() -> dict:
    """Peel Pareto layers off the 60-point grid.

    Layer 1 is the non-dominated front, layer 2 the front of what remains, and so on. The
    longest chain in a 4 x 5 x 3 grid is 3 + 4 + 2 + 1 = 10, so tiers run 1..10.
    """
    remaining = {
        (r, c, s)
        for r in range(len(rubric.ROLE_MATCH))
        for c in range(len(rubric.CAPABILITY_MATCH))
        for s in range(len(rubric.SENIORITY_GAP))
    }
    tiers, tier = {}, 1
    while remaining:
        front = {p for p in remaining
                 if not any(_dominates(q, p) for q in remaining if q != p)}
        # A finite strict partial order always has maximal elements, so `front` cannot be
        # empty. Assert rather than risk an infinite loop if a future edit breaks that.
        assert front, "empty Pareto front -- dominance is no longer a strict partial order"
        for point in front:
            tiers[point] = tier
        remaining -= front
        tier += 1
    return tiers


_TIERS = _build_tiers()
MAX_TIER = max(_TIERS.values())


def pareto_tier(*, role_match: str, capability_match: str, seniority_gap: str) -> int:
    """1 (best) .. MAX_TIER. Equal tiers are incomparable, not equal in quality."""
    return _TIERS[(_RANKS["role_match"][role_match],
                   _RANKS["capability_match"][capability_match],
                   _RANKS["seniority_gap"][seniority_gap])]


def sort_key(verdict: dict) -> tuple:
    """The default list order. Lower sorts first.

    Eligibility partitions, then tier. Ties inside a tier are genuine -- the caller adds
    recency or another tiebreak, and must not read anything into the resulting order.
    """
    return (eligibility_rank(verdict["eligibility"]),
            pareto_tier(role_match=verdict["role_match"],
                        capability_match=verdict["capability_match"],
                        seniority_gap=verdict["seniority_gap"]))


# --- the scalar projection ------------------------------------------------------------
#
# Everything below here is the invented part. It is kept in one small table so that the
# invention is visible and revisable, instead of being smeared across four weights whose
# provenance nobody remembers.

# GRID[role_match][capability_match], before seniority and eligibility adjust it.
GRID = {
    #                    exceeds  meets  most_with_gaps  major_gaps  not_close
    "same_role":        (    92,    85,             70,         48,        28),
    "adjacent":         (    82,    74,             60,         40,        22),
    "different_domain": (    66,    58,             46,         30,        15),
    "different_field":  (    42,    36,             28,         16,         6),
}

SENIORITY_DELTA = {"matched": 0, "candidate_above": -5, "candidate_below": -12}

# A thin posting is pulled toward the middle: we know less, so we should claim less in both
# directions. One shrink step, not a graded one -- a two-stage shrink is unreadable and no
# better justified.
THIN_ANCHOR, THIN_SHRINK = 45, 0.70

# A ceiling, and deliberately ONLY a ceiling. `blocked` is a veto; `conditional` lands
# mid-scale so a clearance the candidate could obtain stays visible without competing with
# work they can take today.
#
# An earlier version also subtracted a flat penalty for `conditional`, which inverted the
# scale at the bottom: adjacent/not_close/candidate_below scored 0 as `conditional` but 5 as
# `blocked`, so admitting the blocker was surmountable made the posting rank lower. Caps
# compose monotonically (100 >= 45 >= 5) and the penalty bought nothing the cap did not --
# at the top it is redundant, and at the bottom it was wrong.
ELIGIBILITY_CEILING = {
    "eligible": 100,
    "conditional": 45,
    "blocked": 5,
}

# Kept at the values the old prompt used, and the same edges `stats.py` and the UI colours
# already key off. Moving the scale and the boundaries in one change would make the
# rollout undiagnosable.
BANDS = (("strong", 80), ("worth_applying", 60), ("stretch", 40), ("poor_fit", 20),
         ("mismatch", 0))


def band_for(score: int) -> str:
    for name, low in BANDS:
        if score >= low:
            return name
    return BANDS[-1][0]


def fit_score(*, eligibility: str, role_match: str, capability_match: str,
              seniority_gap: str, evidence_quality: str) -> int:
    """Project the tuple onto 0-100. Order of operations is fixed and load-bearing."""
    score = GRID[role_match][_RANKS["capability_match"][capability_match]]
    score += SENIORITY_DELTA[seniority_gap]
    if evidence_quality == "thin":
        score = THIN_ANCHOR + THIN_SHRINK * (score - THIN_ANCHOR)
    score = min(score, ELIGIBILITY_CEILING[eligibility])
    return int(round(max(0, min(100, score))))


def project(verdict: dict) -> dict:
    """Everything derived from one verdict's ordinals, for persistence and display."""
    ordinals = {name: verdict[name] for name in rubric.DIMENSIONS}
    score = fit_score(**ordinals)
    return {
        "fit_score": score,
        "verdict": band_for(score),
        "pareto_tier": pareto_tier(role_match=verdict["role_match"],
                                   capability_match=verdict["capability_match"],
                                   seniority_gap=verdict["seniority_gap"]),
        "eligibility_rank": eligibility_rank(verdict["eligibility"]),
        "scale_version": SCALE_VERSION,
    }
