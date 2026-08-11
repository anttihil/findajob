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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.profile.adapter import ProfileAdapter  # noqa: E402
from careerradar.profile.models import (  # noqa: E402
    LEVEL_CLAIMED,
    LEVEL_MENTIONED,
    LEVEL_STRONG,
    Profile,
    Skill,
)
from careerradar.taxonomy.roles import load_roles  # noqa: E402
from careerradar.search.keyword_score import (  # noqa: E402
    Bm25Index,
    JobScorer,
    seniority_fit,
    skill_coverage,
    squash,
    title_family_fit,
    tokenize,
)
from careerradar.taxonomy.skills import load_taxonomy  # noqa: E402


def make_profile(levels, taxonomy):
    """A profile with explicit levels, so tests do not depend on the real resumes."""
    profile = Profile(
        bio="test candidate",
        skills=[
            Skill(key=key, label=key, level=level, evidence="test")
            for key, level in levels.items()
        ],
    )
    return ProfileAdapter(profile, version=0, taxonomy=taxonomy)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()
        self.profile = make_profile(
            {"python": LEVEL_STRONG, "docker": LEVEL_CLAIMED,
             "terraform": LEVEL_MENTIONED},
            self.tax,
        )

    def test_no_recognized_skills_does_not_mean_perfect_coverage(self):
        """The Stationary Engineer regression: an empty requirement set scored 1.00."""
        coverage, matched, missing = skill_coverage({}, self.profile)
        self.assertLess(coverage, 0.4)
        self.assertEqual(matched, [])
        self.assertEqual(missing, [])

    def test_coverage_is_shrunk_toward_the_prior_on_small_denominators(self):
        """Two-of-two must not look as strong as twenty-of-twenty."""
        small, _, _ = skill_coverage({"python": {}}, self.profile)
        large, _, _ = skill_coverage(
            {k: {} for k in ["python", "docker", "terraform"] * 5}, self.profile
        )
        self.assertLess(small, large)

    def test_full_coverage_approaches_one_with_enough_evidence(self):
        required = {k: {} for k in ["python", "docker"]}
        coverage, _, _ = skill_coverage(required, self.profile)
        self.assertGreater(coverage, 0.35)
        self.assertLess(coverage, 1.0)

    def test_more_matched_skills_never_lowers_coverage(self):
        base = {"python": {}}
        better = {"python": {}, "docker": {}}
        self.assertGreaterEqual(
            skill_coverage(better, self.profile)[0],
            skill_coverage(base, self.profile)[0],
        )

    def test_unmatched_requirements_lower_coverage(self):
        matched_only = {"python": {}, "docker": {}}
        with_gaps = {"python": {}, "docker": {}, "kubernetes": {}, "kafka": {}}
        self.assertGreater(
            skill_coverage(matched_only, self.profile)[0],
            skill_coverage(with_gaps, self.profile)[0],
        )

    def test_missing_skills_are_reported(self):
        _, matched, missing = skill_coverage(
            {"python": {}, "kubernetes": {}}, self.profile
        )
        self.assertIn("python", matched)
        self.assertIn("kubernetes", missing)

    def test_stronger_evidence_scores_at_least_as_well(self):
        strong = make_profile({"python": LEVEL_STRONG}, self.tax)
        weak = make_profile({"python": LEVEL_MENTIONED}, self.tax)
        required = {"python": {}}
        self.assertGreater(
            skill_coverage(required, strong)[0], skill_coverage(required, weak)[0]
        )

    def test_title_skills_weigh_more_than_body_skills(self):
        """A skill in the title is a hard requirement; missing it should cost more."""
        in_title = {"kubernetes": {"in_title": True}}
        in_body = {"kubernetes": {"in_title": False}}
        self.assertLess(
            skill_coverage(in_title, self.profile)[0],
            skill_coverage(in_body, self.profile)[0],
        )


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()
        self.roles = load_roles()
        self.profile = make_profile({"python": LEVEL_STRONG}, self.tax)

    def test_seniority_fit_prefers_mid_and_senior(self):
        self.assertGreater(seniority_fit("mid"), seniority_fit("staff"))
        self.assertGreater(seniority_fit("senior"), seniority_fit("staff"))
        self.assertGreater(seniority_fit("senior"), seniority_fit("intern"))
        self.assertGreater(seniority_fit("unspecified"), seniority_fit("intern"))

    def test_unknown_seniority_is_neutral_not_zero(self):
        self.assertGreater(seniority_fit(None), 0.5)

    def test_core_families_fit_better_than_breadth(self):
        core = title_family_fit("ai_engineer", self.roles, self.profile)
        breadth = title_family_fit("mobile_engineer", self.roles, self.profile)
        self.assertGreater(core, breadth)

    def test_unclassified_family_scores_zero(self):
        self.assertEqual(title_family_fit(None, self.roles, self.profile), 0.0)

    def test_squash_is_monotonic_and_bounded(self):
        values = [squash(v, 12.0) for v in (0, 1, 5, 20, 100, 1000)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0 <= v < 1 for v in values))


class TokenizeTests(unittest.TestCase):
    def test_stopwords_and_boilerplate_are_dropped(self):
        tokens = tokenize("We are looking for a candidate with experience in Python")
        self.assertIn("python", tokens)
        for noise in ("are", "for", "a", "with", "candidate", "experience"):
            self.assertNotIn(noise, tokens)

    def test_technical_punctuation_survives(self):
        tokens = tokenize("Node.js, C++, CI/CD and .NET")
        self.assertTrue(any("node.js" in t for t in tokens))
        self.assertTrue(any("c++" in t for t in tokens))

    def test_empty_input_is_safe(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None), [])


class Bm25Tests(unittest.TestCase):
    def test_empty_corpus_degrades_gracefully(self):
        """A cold database must not crash or score everything zero."""
        index = Bm25Index()
        score = index.score(tokenize("python docker"), tokenize("python docker terraform"))
        self.assertGreater(score, 0)

    def test_overlap_scores_higher_than_no_overlap(self):
        index = Bm25Index(["python docker aws", "java spring hibernate"])
        overlapping = index.score(tokenize("python docker"), tokenize("python docker aws"))
        disjoint = index.score(tokenize("python docker"), tokenize("java spring"))
        self.assertGreater(overlapping, disjoint)

    def test_rare_terms_weigh_more_than_common_ones(self):
        corpus = ["python web app"] * 20 + ["python vllm inference"]
        index = Bm25Index(corpus)
        self.assertGreater(index.idf("vllm"), index.idf("python"))


class ScorerTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()
        self.roles = load_roles()
        self.profile = make_profile(
            {"python": LEVEL_STRONG, "docker": LEVEL_STRONG, "aws": LEVEL_STRONG,
             "terraform": LEVEL_CLAIMED, "postgresql": LEVEL_CLAIMED,
             "react": LEVEL_CLAIMED, "typescript": LEVEL_CLAIMED},
            self.tax,
        )
        self.scorer = JobScorer(self.profile, self.roles, self.tax)

    def _score(self, **posting):
        return self.scorer.score(posting)["score"]

    def test_score_is_bounded(self):
        for posting in [
            {},
            {"title": "x", "description": "y"},
            {"title": "Senior Platform Engineer",
             "description": "Python Docker AWS Terraform PostgreSQL React TypeScript",
             "role_family": "platform_engineer", "seniority": "senior"},
        ]:
            score = self.scorer.score(posting)["score"]
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_good_fit_outscores_poor_fit(self):
        good = self._score(
            title="Senior Platform Engineer",
            description="You will use Python, Docker, AWS, Terraform and PostgreSQL.",
            role_family="platform_engineer", seniority="senior",
        )
        poor = self._score(
            title="Senior Kubernetes Kafka Engineer",
            description="Deep Kubernetes, Kafka, Snowflake, Spark and Scala required.",
            role_family="data_engineer", seniority="staff",
        )
        self.assertGreater(good, poor)

    def test_off_topic_posting_scores_low(self):
        """The Stationary Engineer case, end to end."""
        score = self._score(
            title="Stationary Engineer",
            description=(
                "Operate and maintain boilers, chillers and HVAC systems in a "
                "commercial building. Must hold a valid operating licence and be "
                "able to lift 50 pounds. Shift work required."
            ),
            role_family=None, seniority="unspecified",
        )
        self.assertLess(score, 45, "an off-topic posting should not score highly")

    def test_adding_a_matched_skill_never_lowers_the_score(self):
        base = {"title": "Engineer", "description": "Python required.",
                "role_family": "software_engineer", "seniority": "mid"}
        richer = dict(base, description="Python and Docker and AWS required.")
        self.assertGreaterEqual(
            self.scorer.score(richer)["score"], self.scorer.score(base)["score"]
        )

    def test_adding_an_unmatched_requirement_never_raises_the_score(self):
        base = {"title": "Engineer", "description": "Python and Docker required.",
                "role_family": "software_engineer", "seniority": "mid"}
        harder = dict(
            base,
            description="Python and Docker required. Also Kubernetes, Kafka, Snowflake.",
        )
        self.assertLessEqual(
            self.scorer.score(harder)["score"], self.scorer.score(base)["score"]
        )

    def test_better_seniority_fit_never_lowers_the_score(self):
        base = {"title": "Engineer", "description": "Python Docker AWS.",
                "role_family": "software_engineer"}
        self.assertGreaterEqual(
            self.scorer.score(dict(base, seniority="mid"))["score"],
            self.scorer.score(dict(base, seniority="intern"))["score"],
        )

    def test_classified_family_never_lowers_the_score(self):
        base = {"title": "Platform Engineer", "description": "Python Docker AWS.",
                "seniority": "mid"}
        self.assertGreaterEqual(
            self.scorer.score(dict(base, role_family="platform_engineer"))["score"],
            self.scorer.score(dict(base, role_family=None))["score"],
        )

    def test_scoring_is_deterministic(self):
        posting = {"title": "Senior Platform Engineer",
                   "description": "Python, Docker, AWS, Terraform.",
                   "role_family": "platform_engineer", "seniority": "senior"}
        scores = {self.scorer.score(posting)["score"] for _ in range(5)}
        self.assertEqual(len(scores), 1)

    def test_result_exposes_every_component_for_auditability(self):
        result = self.scorer.score({
            "title": "Senior Platform Engineer",
            "description": "Python, Docker, AWS, Terraform, Kubernetes.",
            "role_family": "platform_engineer", "seniority": "senior",
        })
        for key in ("score", "components", "weights", "matched_skills",
                    "missing_skills", "required_count", "resume_match"):
            self.assertIn(key, result)
        for component in ("skill_coverage", "bm25", "title_family", "seniority_fit"):
            self.assertIn(component, result["components"])
            self.assertGreaterEqual(result["components"][component], 0.0)
            self.assertLessEqual(result["components"][component], 1.0)

    def test_missing_skills_surface_the_gap(self):
        result = self.scorer.score({
            "title": "Platform Engineer",
            "description": "Python and Docker, plus Kubernetes and Kafka.",
            "role_family": "platform_engineer", "seniority": "mid",
        })
        self.assertIn("kubernetes", result["missing_skills"])
        self.assertIn("kafka", result["missing_skills"])
        self.assertIn("python", result["matched_skills"])

    def test_resume_match_follows_the_role_family(self):
        result = self.scorer.score({
            "title": "Senior Platform Engineer", "description": "Terraform.",
            "role_family": "platform_engineer", "seniority": "senior",
        })
        self.assertEqual(result["resume_match"], "platform_devops_engineer.md")

    def test_weights_are_configurable_and_clamped_to_known_keys(self):
        scorer = JobScorer(
            self.profile, self.roles, self.tax,
            weights={"skill_coverage": 1.0, "bm25": 0.0, "title_family": 0.0,
                     "seniority_fit": 0.0, "bogus": 5.0},
        )
        self.assertNotIn("bogus", scorer.weights)
        self.assertEqual(scorer.weights["skill_coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
