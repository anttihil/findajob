"""Deterministic requirement coverage. [DEPRECATED / RETIRED]

This module and `JobScorer` are retired as part of Phase 4 of the taxonomy decoupling.
Candidate-job fit scoring is handled by the LLM-agentic pipeline in `careerradar/scoring/`.
Retained for backwards compatibility.
"""

import warnings
from typing import TYPE_CHECKING, Any

warnings.warn(
    "careerradar.search.keyword_score is deprecated and retired in Phase 4. "
    "Use the LLM scoring pipeline for fit evaluation.",
    DeprecationWarning,
    stacklevel=2,
)

if TYPE_CHECKING:
    from careerradar.profile.adapter import ProfileAdapter
    from careerradar.taxonomy.roles import RoleTaxonomy
    from careerradar.taxonomy.skills import Taxonomy

# Redistributed proportionally when BM25 was removed (0.45/0.20/0.10 over 0.75), rather
# than re-tuned. There is nothing to tune against, and inventing a number is how the
# original weights got here.
DEFAULT_WEIGHTS = {
    "skill_coverage": 0.62,
    "title_family": 0.24,
    "seniority_fit": 0.14,
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


def skill_coverage(
    required: dict[str, dict[str, Any]] | None,
    profile: "ProfileAdapter",
    taxonomy: "Taxonomy | None" = None,
) -> tuple[float, list[str], list[str], float | None]:
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
    matched: list[str] = []
    missing: list[str] = []
    earned = 0.0
    possible = 0.0

    for skill, info in (required or {}).items():
        weight = TITLE_SKILL_MULTIPLIER if info.get("in_title") else 1.0
        possible += weight
        has_skill = profile.has(skill)
        credit = 1.0 if has_skill else 0.0
        if not has_skill and taxonomy is not None:
            implied = any(profile.has(child) for child in taxonomy.implied_by(skill))
            if implied:
                credit = IMPLIED_CREDIT
        if credit > 0:
            earned += weight * credit
            matched.append(skill)
        else:
            missing.append(skill)

    coverage = (earned + COVERAGE_PRIOR_WEIGHT * COVERAGE_PRIOR) / (
        possible + COVERAGE_PRIOR_WEIGHT
    )
    total = len(matched) + len(missing)
    ratio = (len(matched) / total) if total else None
    return coverage, matched, missing, ratio


def title_family_fit(
    role_family: str | None,
    roles: "RoleTaxonomy",
    profile: "ProfileAdapter | None" = None,  # noqa: ARG001 - kept so callers need not special-case this scorer
) -> float:
    """How close the posting's role family sits to the user's stated targets."""
    if not role_family:
        return 0.0
    family = roles.get(role_family)
    if not family or not family.enabled:
        return 0.0
    return 1.0


def seniority_fit(seniority: str | None) -> float:
    return SENIORITY_FIT.get(seniority or "unspecified", 0.7)


class JobScorer:
    """[DEPRECATED / RETIRED] Scores postings against the user profile.

    Retired in Phase 4. Candidate-job fit evaluation is performed by the LLM agentic
    scoring pipeline.
    """

    def __init__(
        self,
        profile: "ProfileAdapter",
        roles: "RoleTaxonomy",
        taxonomy: "Taxonomy | None" = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        warnings.warn(
            "JobScorer is retired and deprecated. Use the LLM scoring pipeline instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.profile = profile
        self.roles = roles
        self.taxonomy = taxonomy
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update({k: v for k, v in weights.items() if k in DEFAULT_WEIGHTS})

    def score(self, posting: dict[str, Any]) -> dict[str, Any]:
        """Score one posting. Returns a dict with the total and every component.

        `posting` needs title, description, role_family, seniority, and optionally a
        pre-extracted `skills` map; skills are extracted here if absent.
        """
        title = posting.get("title") or ""
        description = posting.get("description") or ""

        required = posting.get("skills")
        if required is None:
            if self.taxonomy is not None:
                required = self.taxonomy.extract(description, title=title)
            else:
                required = {}

        coverage, matched, missing, ratio = skill_coverage(
            required, self.profile, taxonomy=self.taxonomy
        )
        components = {
            "skill_coverage": coverage,
            "title_family": title_family_fit(posting.get("role_family"), self.roles, self.profile),
            "seniority_fit": seniority_fit(posting.get("seniority")),
        }

        total = sum(components[name] * weight for name, weight in self.weights.items())
        total = max(0.0, min(1.0, total))

        return {
            # A coverage index, not a percent. See the module docstring.
            "score": round(total * 100),
            "components": components,
            "weights": dict(self.weights),
            "matched_skills": sorted(matched),
            "missing_skills": sorted(missing),
            "matched_count": len(matched),
            "required_count": len(required),
            "coverage_ratio": ratio,
            "resume_match": self.roles.resume_for(posting.get("role_family")),
        }
