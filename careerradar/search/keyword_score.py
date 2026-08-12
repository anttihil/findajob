"""Deterministic requirement coverage.

What this measures is narrow and worth stating precisely: of the skills this posting names
that our taxonomy recognises, how many can the candidate evidence. It is not a fit score
and it does not rank the dashboard -- `scoring/` does that, with a model that can read the
posting. Coverage feeds three things instead: the skill hint in the scoring prompt, the
gap analytics in `market/`, and a coarse tiebreak.

Two corrections from what this file used to be.

BM25 is gone. It carried 25% of the weight and its query was `profile_tokens()` -- a bag of
the candidate's skill labels, byte-identical for every posting in the corpus. So it asked
"how much rare vocabulary does this posting share with a fixed word list", which is a
noisier restatement of the coverage component it sat next to, computed on raw tokens
instead of canonical keys. Nothing validated it, and a posting matching four generic terms
(linux, python, shell, tech_writing) scored the same 70 as one matching eighteen.

The result is no longer presented as a percent. It never was one: the coverage prior, an
unspecified seniority and an unmapped role family together put ~23 points on the floor, so
the observed range across 5,932 postings was 15-80. `coverage_ratio` reports matched over
required with its denominator, and is None -- not 1.0 -- when the posting named nothing we
recognise.
"""

from careerradar.profile.models import LEVEL_CLAIMED, LEVEL_MENTIONED, LEVEL_STRONG

# Redistributed proportionally when BM25 was removed (0.45/0.20/0.10 over 0.75), rather
# than re-tuned. There is nothing to tune against, and inventing a number is how the
# original weights got here.
DEFAULT_WEIGHTS = {
    "skill_coverage": 0.62,
    "title_family": 0.24,
    "seniority_fit": 0.14,
}

# How much each profile evidence level contributes when a required skill is matched. A skill
# named once in prose should not count as fully as one used across current projects.
LEVEL_CREDIT = {
    LEVEL_STRONG: 1.0,
    LEVEL_CLAIMED: 0.85,
    LEVEL_MENTIONED: 0.55,
}

# A skill named in the TITLE is a hard requirement in a way a body mention is not, so both
# its contribution and its cost when missing are amplified.
TITLE_SKILL_MULTIPLIER = 2.5

# Shrinkage for skill coverage, in pseudo-skills. Without it, a posting with two recognized
# skills that the user happens to have scores the same as one with twenty, and a posting with
# no recognized skills at all scores a perfect 1.0.
COVERAGE_PRIOR_WEIGHT = 4.0
# What an arbitrary posting's coverage would be absent any evidence.
COVERAGE_PRIOR = 0.25

# The user has ~4 years of engineering experience plus prior teaching. mid/senior postings
# fit; staff/principal is a stretch; intern is a mismatch.
SENIORITY_FIT = {
    "mid": 1.00,
    "senior": 0.95,
    "unspecified": 0.80,
    "lead": 0.60,
    "junior": 0.55,
    "staff": 0.40,
    "intern": 0.05,
}

# Credit for a skill the candidate evidences only through something it subsumes: the
# posting asks for `llm_apps`, the profile shows `claude_api`. Discounted, because
# subsumption is weaker evidence than the named skill -- and applied on the PROFILE side
# only. Inflating the posting side would corrupt `skill_market_stats`, which measures
# demand from `job_skills` and has no column separating observed from inferred.
IMPLIED_CREDIT = 0.8


def skill_coverage(required, profile, taxonomy=None):
    """Fraction of a posting's required skills the user can evidence, shrunk toward a prior.

    Coverage, not count: a posting listing 30 technologies of which the user has 10 is a
    worse fit than one listing 6 of which they have 5. The old scorer rewarded the former.

    The shrinkage matters as much as the ratio. A raw ratio treats "2 skills detected, both
    matched" as identical to "20 detected, all 20 matched", and treats "no skills detected"
    as perfect coverage. A live dry run scored a *Stationary Engineer* boiler-operator
    posting at 83 for exactly that reason: zero recognized skills gave coverage 1.00.

    So the ratio is smoothed with COVERAGE_PRIOR_WEIGHT pseudo-skills at COVERAGE_PRIOR:
    an empty posting lands near the prior rather than at the top, and confident coverage
    requires a real denominator.

    Returns `(coverage, matched, missing, ratio)`. `ratio` is the unsmoothed matched-over-
    required fraction for display, and is None when the posting named nothing recognised --
    that is unknown coverage, not perfect coverage, and the smoothing exists precisely
    because the raw ratio cannot say so.
    """
    matched = []
    missing = []
    earned = 0.0
    possible = 0.0

    for skill, info in (required or {}).items():
        weight = TITLE_SKILL_MULTIPLIER if info.get("in_title") else 1.0
        possible += weight
        level = profile.level(skill)
        credit = LEVEL_CREDIT.get(level, 0.5) if level > 0 else 0.0
        if level == 0 and taxonomy is not None:
            implied = max((profile.level(child)
                           for child in taxonomy.implied_by(skill)), default=0)
            if implied > 0:
                credit = LEVEL_CREDIT.get(implied, 0.5) * IMPLIED_CREDIT
        if credit > 0:
            earned += weight * credit
            matched.append(skill)
        else:
            missing.append(skill)

    coverage = (
        (earned + COVERAGE_PRIOR_WEIGHT * COVERAGE_PRIOR)
        / (possible + COVERAGE_PRIOR_WEIGHT)
    )
    total = len(matched) + len(missing)
    ratio = (len(matched) / total) if total else None
    return coverage, matched, missing, ratio


def title_family_fit(role_family, roles, profile=None):
    """How close the posting's role family sits to the user's stated targets.

    This used to ask "does the user have a tailored resume for this family, and is that
    resume present in the corpus?", which layered a file-existence check on top of the
    tier. With one unified profile there are no resume variants to check, and that middle
    branch answered 0.4 for every family -- flattening a component that carries 20% of the
    score into a constant.

    Tier is what the question was always really asking. roles.yaml already states it:
    core families are the targets, adjacent and breadth are pivots. An unmapped family
    still scores low, because nothing in roles.yaml claims it is a fit.
    """
    if not role_family:
        return 0.0
    if not roles.resume_for(role_family):
        return 0.2
    return {"core": 1.0, "adjacent": 0.75, "breadth": 0.5}.get(
        roles.tier(role_family), 0.5
    )


def seniority_fit(seniority):
    return SENIORITY_FIT.get(seniority or "unspecified", 0.7)


class JobScorer:
    """Scores postings against the user profile. Deterministic and explainable."""

    def __init__(self, profile, roles, taxonomy, weights=None):
        self.profile = profile
        self.roles = roles
        self.taxonomy = taxonomy
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(
                {k: v for k, v in weights.items() if k in DEFAULT_WEIGHTS}
            )

    def score(self, posting):
        """Score one posting. Returns a dict with the total and every component.

        `posting` needs title, description, role_family, seniority, and optionally a
        pre-extracted `skills` map; skills are extracted here if absent.
        """
        title = posting.get("title") or ""
        description = posting.get("description") or ""

        required = posting.get("skills")
        if required is None:
            required = self.taxonomy.extract(description, title=title)

        coverage, matched, missing, ratio = skill_coverage(
            required, self.profile, taxonomy=self.taxonomy
        )
        components = {
            "skill_coverage": coverage,
            "title_family": title_family_fit(
                posting.get("role_family"), self.roles, self.profile
            ),
            "seniority_fit": seniority_fit(posting.get("seniority")),
        }

        total = sum(components[name] * weight
                    for name, weight in self.weights.items())
        total = max(0.0, min(1.0, total))

        return {
            # A coverage index, not a percent. See the module docstring.
            "score": int(round(total * 100)),
            "components": components,
            "weights": dict(self.weights),
            "matched_skills": sorted(matched),
            "missing_skills": sorted(missing),
            "matched_count": len(matched),
            "required_count": len(required),
            "coverage_ratio": ratio,
            "resume_match": self.roles.resume_for(posting.get("role_family")),
        }

    def score_many(self, postings):
        return [self.score(posting) for posting in postings]
