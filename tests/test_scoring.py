"""Scoring tests, written as monotonicity properties rather than expected constants.

The old test asserted `score >= 75` for a fixture, derived from the previous scorer's
"+25 per title skill, +5 per body skill" arithmetic. That kind of assertion pins an
implementation detail: any reweighting breaks it, and re-tuning the constant to match
teaches nothing. What actually needs to hold is directional -- more matched skills should
never lower a score, a better-fitting seniority should never score worse, and so on.

Two regressions found by a live dry run are pinned here as explicit cases: a boiler-operator
"Stationary Engineer" posting scored 83 because zero recognized skills yielded
skill_coverage = 1.00, and small denominators made two matched skills look as good as twenty.
"""

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.models import (
    MasterSkillCategory,
    Profile,
)
from careerradar.search.keyword_score import (
    JobScorer,
    seniority_fit,
    skill_coverage,
    title_family_fit,
)
from careerradar.taxonomy.roles import load_roles
from careerradar.taxonomy.skills import Taxonomy, load_taxonomy


def make_profile(
    skills: list[str] | dict[str, Any] | set[str], taxonomy: Taxonomy
) -> ProfileAdapter:
    """A profile with explicit skills, so tests do not depend on the real resumes."""
    skill_list = list(skills.keys()) if isinstance(skills, dict) else list(skills)
    profile = Profile(
        summary_guidance="test candidate",
        skills=[
            MasterSkillCategory(
                category="Technical",
                skills=skill_list,
            )
        ],
    )
    return ProfileAdapter(profile, version=0, taxonomy=taxonomy)


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()
        self.profile = make_profile(
            ["python", "docker", "terraform"],
            self.tax,
        )

    def test_no_recognized_skills_does_not_mean_perfect_coverage(self) -> None:
        """The Stationary Engineer regression: an empty requirement set scored 1.00."""
        coverage, matched, missing, _ratio = skill_coverage({}, self.profile)
        self.assertLess(coverage, 0.4)
        self.assertEqual(matched, [])
        self.assertEqual(missing, [])

    def test_coverage_is_shrunk_toward_the_prior_on_small_denominators(self) -> None:
        """Two-of-two must not look as strong as twenty-of-twenty."""
        small, *_ = skill_coverage({"python": {}}, self.profile)
        large, *_ = skill_coverage(
            {k: {} for k in ["python", "docker", "terraform"] * 5}, self.profile
        )
        self.assertLess(small, large)

    def test_full_coverage_approaches_one_with_enough_evidence(self) -> None:
        required = {k: {} for k in ["python", "docker"]}
        coverage, *_ = skill_coverage(required, self.profile)
        self.assertGreater(coverage, 0.35)
        self.assertLess(coverage, 1.0)

    def test_more_matched_skills_never_lowers_coverage(self) -> None:
        base = {"python": {}}
        better = {"python": {}, "docker": {}}
        self.assertGreaterEqual(
            skill_coverage(better, self.profile)[0],
            skill_coverage(base, self.profile)[0],
        )

    def test_unmatched_requirements_lower_coverage(self) -> None:
        matched_only = {"python": {}, "docker": {}}
        with_gaps = {"python": {}, "docker": {}, "kubernetes": {}, "kafka": {}}
        self.assertGreater(
            skill_coverage(matched_only, self.profile)[0],
            skill_coverage(with_gaps, self.profile)[0],
        )

    def test_missing_skills_are_reported(self) -> None:
        _c, matched, missing, _r = skill_coverage({"python": {}, "kubernetes": {}}, self.profile)
        self.assertIn("python", matched)
        self.assertIn("kubernetes", missing)

    def test_stronger_evidence_scores_at_least_as_well(self) -> None:
        has_skill = make_profile(["python"], self.tax)
        no_skill = make_profile([], self.tax)
        required = {"python": {}}
        self.assertGreater(
            skill_coverage(required, has_skill)[0],
            skill_coverage(required, no_skill)[0],
        )

    def test_title_skills_weigh_more_than_body_skills(self) -> None:
        """A skill in the title is a hard requirement; missing it should cost more."""
        in_title = {"kubernetes": {"in_title": True}}
        in_body = {"kubernetes": {"in_title": False}}
        self.assertLess(
            skill_coverage(in_title, self.profile)[0],
            skill_coverage(in_body, self.profile)[0],
        )


class ComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()
        self.roles = load_roles()
        self.profile = make_profile(["python"], self.tax)

    def test_seniority_fit_prefers_mid_and_senior(self) -> None:
        self.assertGreater(seniority_fit("mid"), seniority_fit("staff"))
        self.assertGreater(seniority_fit("senior"), seniority_fit("staff"))
        self.assertGreater(seniority_fit("senior"), seniority_fit("intern"))
        self.assertGreater(seniority_fit("unspecified"), seniority_fit("intern"))

    def test_unknown_seniority_is_neutral_not_zero(self) -> None:
        self.assertGreater(seniority_fit(None), 0.5)

    def test_active_target_family_fits_and_unclassified_scores_zero(self) -> None:
        ai = title_family_fit("ai_engineer", self.roles, self.profile)
        self.assertEqual(ai, 1.0)
        unclassified = title_family_fit(None, self.roles, self.profile)
        self.assertEqual(unclassified, 0.0)


class CoverageRatioTests(unittest.TestCase):
    """The ratio must be able to say "unknown", which the smoothed score cannot."""

    def setUp(self) -> None:
        self.tax = load_taxonomy()
        self.profile = make_profile(["python"], self.tax)

    def test_a_posting_naming_nothing_recognised_has_unknown_coverage(self) -> None:
        _cov, _m, _mi, ratio = skill_coverage({}, self.profile)
        self.assertIsNone(ratio)

    def test_the_ratio_is_matched_over_required(self) -> None:
        required = {"python": {"in_title": False}, "kubernetes": {"in_title": False}}
        _cov, matched, missing, ratio = skill_coverage(required, self.profile)
        self.assertEqual(ratio, len(matched) / (len(matched) + len(missing)))


class ImpliesTests(unittest.TestCase):
    """A posting asking for `llm_apps` should see evidence of `claude_api`."""

    def setUp(self) -> None:
        self.tax = load_taxonomy()
        self.profile = make_profile(["claude_api"], self.tax)

    def test_a_child_skill_evidences_its_parent(self) -> None:
        required = {"llm_apps": {"in_title": False}}
        without = skill_coverage(required, self.profile)
        with_tax = skill_coverage(required, self.profile, taxonomy=self.tax)
        self.assertIn("llm_apps", without[2])  # missing without the edges
        self.assertIn("llm_apps", with_tax[1])  # matched with them

    def test_implied_credit_is_worth_less_than_the_named_skill(self) -> None:
        implied = skill_coverage(
            {"llm_apps": {"in_title": False}}, self.profile, taxonomy=self.tax
        )[0]
        named = skill_coverage(
            {"claude_api": {"in_title": False}}, self.profile, taxonomy=self.tax
        )[0]
        self.assertLess(implied, named)


class ScorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()
        self.roles = load_roles()
        self.profile = make_profile(
            [
                "python",
                "docker",
                "aws",
                "terraform",
                "postgresql",
                "react",
                "typescript",
            ],
            self.tax,
        )
        self.scorer = JobScorer(self.profile, self.roles, self.tax)

    def _score(self, **posting: Any) -> int:
        return self.scorer.score(posting)["score"]

    def test_score_is_bounded(self) -> None:
        for posting in [
            {},
            {"title": "x", "description": "y"},
            {
                "title": "Senior Platform Engineer",
                "description": "Python Docker AWS Terraform PostgreSQL React TypeScript",
                "role_family": "platform_engineer",
                "seniority": "senior",
            },
        ]:
            score = self.scorer.score(posting)["score"]
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_good_fit_outscores_poor_fit(self) -> None:
        good = self._score(
            title="Senior Platform Engineer",
            description="You will use Python, Docker, AWS, Terraform and PostgreSQL.",
            role_family="platform_engineer",
            seniority="senior",
        )
        poor = self._score(
            title="Senior Kubernetes Kafka Engineer",
            description="Deep Kubernetes, Kafka, Snowflake, Spark and Scala required.",
            role_family="data_engineer",
            seniority="staff",
        )
        self.assertGreater(good, poor)

    def test_off_topic_posting_scores_low(self) -> None:
        """The Stationary Engineer case, end to end."""
        score = self._score(
            title="Stationary Engineer",
            description=(
                "Operate and maintain boilers, chillers and HVAC systems in a "
                "commercial building. Must hold a valid operating licence and be "
                "able to lift 50 pounds. Shift work required."
            ),
            role_family=None,
            seniority="unspecified",
        )
        self.assertLess(score, 45, "an off-topic posting should not score highly")

    def test_adding_a_matched_skill_never_lowers_the_score(self) -> None:
        base = {
            "title": "Engineer",
            "description": "Python required.",
            "role_family": "software_engineer",
            "seniority": "mid",
        }
        richer = dict(base, description="Python and Docker and AWS required.")
        self.assertGreaterEqual(
            self.scorer.score(richer)["score"], self.scorer.score(base)["score"]
        )

    def test_adding_an_unmatched_requirement_never_raises_the_score(self) -> None:
        base = {
            "title": "Engineer",
            "description": "Python and Docker required.",
            "role_family": "software_engineer",
            "seniority": "mid",
        }
        harder = dict(
            base,
            description="Python and Docker required. Also Kubernetes, Kafka, Snowflake.",
        )
        self.assertLessEqual(self.scorer.score(harder)["score"], self.scorer.score(base)["score"])

    def test_better_seniority_fit_never_lowers_the_score(self) -> None:
        base = {
            "title": "Engineer",
            "description": "Python Docker AWS.",
            "role_family": "software_engineer",
        }
        self.assertGreaterEqual(
            self.scorer.score(dict(base, seniority="mid"))["score"],
            self.scorer.score(dict(base, seniority="intern"))["score"],
        )

    def test_classified_family_never_lowers_the_score(self) -> None:
        base = {
            "title": "Platform Engineer",
            "description": "Python Docker AWS.",
            "seniority": "mid",
        }
        self.assertGreaterEqual(
            self.scorer.score(dict(base, role_family="platform_engineer"))["score"],
            self.scorer.score(dict(base, role_family=None))["score"],
        )

    def test_scoring_is_deterministic(self) -> None:
        posting = {
            "title": "Senior Platform Engineer",
            "description": "Python, Docker, AWS, Terraform.",
            "role_family": "platform_engineer",
            "seniority": "senior",
        }
        scores = {self.scorer.score(posting)["score"] for _ in range(5)}
        self.assertEqual(len(scores), 1)

    def test_result_exposes_every_component_for_auditability(self) -> None:
        result = self.scorer.score(
            {
                "title": "Senior Platform Engineer",
                "description": "Python, Docker, AWS, Terraform, Kubernetes.",
                "role_family": "platform_engineer",
                "seniority": "senior",
            }
        )
        for key in (
            "score",
            "components",
            "weights",
            "matched_skills",
            "missing_skills",
            "matched_count",
            "required_count",
            "coverage_ratio",
            "resume_match",
        ):
            self.assertIn(key, result)
        for component in ("skill_coverage", "title_family", "seniority_fit"):
            self.assertIn(component, result["components"])
            self.assertGreaterEqual(result["components"][component], 0.0)
            self.assertLessEqual(result["components"][component], 1.0)

    def test_missing_skills_surface_the_gap(self) -> None:
        result = self.scorer.score(
            {
                "title": "Platform Engineer",
                "description": "Python and Docker, plus Kubernetes and Kafka.",
                "role_family": "platform_engineer",
                "seniority": "mid",
            }
        )
        self.assertIn("kubernetes", result["missing_skills"])
        self.assertIn("kafka", result["missing_skills"])
        self.assertIn("python", result["matched_skills"])

    def test_resume_match_follows_the_role_family(self) -> None:
        result = self.scorer.score(
            {
                "title": "Senior Platform Engineer",
                "description": "Terraform.",
                "role_family": "platform_engineer",
                "seniority": "senior",
            }
        )
        self.assertEqual(result["resume_match"], "platform_devops_engineer.md")

    def test_weights_are_configurable_and_clamped_to_known_keys(self) -> None:
        scorer = JobScorer(
            self.profile,
            self.roles,
            self.tax,
            weights={
                "skill_coverage": 1.0,
                "title_family": 0.0,
                "seniority_fit": 0.0,
                "bm25": 0.7,
                "bogus": 5.0,
            },
        )
        self.assertNotIn("bogus", scorer.weights)
        # bm25 is gone from the component set, so a config still naming it is ignored
        # rather than silently reintroducing a weight nothing reads.
        self.assertNotIn("bm25", scorer.weights)
        self.assertEqual(scorer.weights["skill_coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
