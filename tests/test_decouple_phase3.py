"""Phase 3 tests: Generalize Market & Gap Analytics, Target-Driven Extraction,

and Decoupled Blocker Detection.
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from careerradar.market.gap_analysis import GapAnalysis
from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.models import MasterSkillCategory, Profile, RoleTargeting
from careerradar.search.keyword_score import JobScorer
from careerradar.taxonomy.blockers import (
    BlockerExtractor,
    load_blocker_extractor,
)
from careerradar.taxonomy.skills import Skill, Taxonomy
from careerradar.web.app import app


class BlockerDecouplingTests(unittest.TestCase):
    def test_default_blocker_patterns(self) -> None:
        extractor = BlockerExtractor()
        self.assertIn(
            "eu_work_authorization",
            extractor.extract("Applicants must hold EU work authorization."),
        )
        self.assertIn(
            "us_work_authorization",
            extractor.extract("Position is open to US citizens only. No visa sponsorship."),
        )
        self.assertIn(
            "security_clearance",
            extractor.extract("Active TS/SCI security clearance required."),
        )
        self.assertIn(
            "local_language",
            extractor.extract("Flytande svenska är ett krav för tjänsten."),
        )
        self.assertEqual(extractor.extract("Standard software engineer job description."), [])

    def test_custom_and_candidate_dealbreakers(self) -> None:
        custom_blockers = {
            "no_night_shifts": {
                "label": "No night shifts",
                "patterns": [r"\bnight shifts?\b"],
            }
        }
        dealbreakers = ["24-hour on-call rotation", "drug testing required"]
        extractor = BlockerExtractor(blockers=custom_blockers, dealbreakers=dealbreakers)

        # Standard still present
        self.assertIn("security_clearance", extractor.extract("Secret security clearance needed."))
        # Custom present
        self.assertIn("no_night_shifts", extractor.extract("Must be available for night shifts."))
        # Dealbreaker present
        self.assertIn(
            "dealbreaker_0",
            extractor.extract("Requires participation in 24-hour on-call rotation."),
        )
        self.assertIn(
            "dealbreaker_1",
            extractor.extract("Mandatory drug testing required prior to employment."),
        )

    def test_load_blocker_extractor_from_profile_and_config(self) -> None:
        profile = Profile(
            name="Alice",
            targeting=RoleTargeting(dealbreakers=["mandatory weekend travel"]),
            dealbreakers=["mandatory weekend travel"],
        )
        config = {
            "blockers": {
                "relocation": {
                    "label": "Relocation required",
                    "patterns": [r"\bmust relocate to\b"],
                }
            }
        }
        extractor = load_blocker_extractor(config=config, profile=profile)
        self.assertIn("relocation", extractor.extract("Candidate must relocate to Denver."))
        self.assertIn("dealbreaker_0", extractor.extract("Requires mandatory weekend travel."))


class TargetDrivenTaxonomyTests(unittest.TestCase):
    def test_taxonomy_works_without_skills_yaml(self) -> None:
        tax = Taxonomy(path="/nonexistent/path/skills.yaml", data={})
        self.assertEqual(len(tax), 0)
        self.assertEqual(tax.categories, [])
        self.assertTrue(len(tax.blockers) > 0)
        self.assertEqual(tax.extract("Just some text"), {})

    def test_open_vocabulary_pattern_compilation(self) -> None:
        skill_cpp = Skill("cplusplus", {"label": "C++", "category": "Languages"})
        self.assertTrue(skill_cpp.search("Proficient in C++ and Python."))
        self.assertFalse(skill_cpp.search("C++11 standard."))

        skill_dotnet = Skill("dotnet", {"label": ".NET", "category": "Frameworks"})
        self.assertTrue(skill_dotnet.search("Services built on .NET Core."))

        skill_csharp = Skill("csharp", {"label": "C#", "category": "Languages"})
        self.assertTrue(skill_csharp.search("C# Developer position."))

        skill_ab = Skill("ab_testing", {"label": "A/B Testing", "category": "Analytics"})
        self.assertTrue(skill_ab.search("Experience with A/B testing and experimentation."))

    def test_taxonomy_from_profile_across_industries(self) -> None:
        # Non-tech industry: Healthcare / Nursing
        profile = Profile(
            name="Nurse Bob",
            skills=[
                MasterSkillCategory(
                    category="Clinical",
                    skills=["Patient Triage", "ICU Care", "Ventilator Support"],
                ),
                MasterSkillCategory(
                    category="Certifications",
                    skills=["BLS", "ACLS", "CCRN"],
                ),
            ],
        )
        tax = Taxonomy.from_profile(profile)
        self.assertIn("Clinical", tax.categories)
        self.assertIn("Certifications", tax.categories)
        self.assertIn("patient_triage", tax.skills)
        self.assertIn("icu_care", tax.skills)
        self.assertIn("ventilator_support", tax.skills)
        self.assertIn("bls", tax.skills)

        posting_text = (
            "We are seeking an ICU Nurse skilled in Patient Triage and Ventilator Support. "
            "BLS certification required."
        )
        found = tax.extract(posting_text, title="Staff ICU Nurse")
        self.assertIn("patient_triage", found)
        self.assertIn("ventilator_support", found)
        self.assertIn("bls", found)
        self.assertNotIn("icu_care", found)

    def test_taxonomy_clone_isolation(self) -> None:
        tax = Taxonomy(path=None, data={"skills": {"python": {"label": "Python"}}})
        clone = tax.clone()
        clone.skills["rust"] = Skill("rust", {"label": "Rust"})
        self.assertIn("rust", clone)
        self.assertNotIn("rust", tax)


class DecoupledGapAnalysisTests(unittest.TestCase):
    def test_gap_analysis_without_taxonomy(self) -> None:
        profile = Profile(
            name="Candidate",
            skills=[
                MasterSkillCategory(
                    category="Backend",
                    skills=["Go", "Kubernetes", "FastAPI"],
                )
            ],
        )
        adapter = ProfileAdapter(profile, taxonomy=None)
        mock_db = mock.Mock()
        mock_db.conn = mock.Mock()
        mock_roles = mock.Mock()
        mock_roles.hash = "roles123"

        gap = GapAnalysis(
            db=mock_db,
            config={},
            roles=mock_roles,
            taxonomy=None,
            profile=adapter,
        )

        empty = gap._empty_result(window_days=90, mode="observed", reason="no_eligible_postings")
        self.assertIsNone(empty["provenance"]["taxonomy_hash"])

        # Score rows without taxonomy
        stats = {
            "fastapi": {
                "total_weight": 10.0,
                "good_fit_weight": 5.0,
                "weighted_count": 8.0,
                "raw_count": 8,
                "weights": [1.0] * 8,
                "companies": {"Acme": 5, "Beta": 3},
                "in_title": 2,
                "blocking_weight": 0.0,
                "familiar_share_sum": 4.0,
                "familiar_share_n": 4,
                "salaries": [],
                "baseline_salary": 120000,
            },
            "kafka": {
                "total_weight": 10.0,
                "good_fit_weight": 5.0,
                "weighted_count": 6.0,
                "raw_count": 6,
                "weights": [1.0] * 6,
                "companies": {"Acme": 4, "Gamma": 2},
                "in_title": 0,
                "blocking_weight": 3.0,
                "familiar_share_sum": 3.0,
                "familiar_share_n": 3,
                "salaries": [],
                "baseline_salary": 120000,
            },
        }

        diagnostics = {"missing_weight": 0.0}
        rows = gap._score_rows(stats, n_eff_total=10.0, diagnostics=diagnostics)
        self.assertEqual(len(rows), 2)

        fastapi_row = next(r for r in rows if r["skill"] == "fastapi")
        self.assertEqual(fastapi_row["label"], "FastAPI")
        self.assertEqual(fastapi_row["category"], "Backend")
        self.assertTrue(fastapi_row["user_has"])

        kafka_row = next(r for r in rows if r["skill"] == "kafka")
        self.assertEqual(kafka_row["label"], "Kafka")
        self.assertEqual(kafka_row["category"], "other")
        self.assertFalse(kafka_row["user_has"])


class DecoupledJobScorerTests(unittest.TestCase):
    def test_job_scorer_works_without_taxonomy(self) -> None:
        profile = Profile(
            name="Alice",
            skills=[MasterSkillCategory(category="Engineering", skills=["Python", "Docker"])],
        )
        adapter = ProfileAdapter(profile, taxonomy=None)
        mock_roles = mock.Mock()
        mock_roles.get.return_value = mock.Mock(enabled=True)
        mock_roles.resume_for.return_value = "default"

        scorer = JobScorer(profile=adapter, roles=mock_roles, taxonomy=None)
        posting = {
            "title": "Senior Software Engineer",
            "description": "Python and Docker needed",
            "skills": {"python": {"in_title": False}, "docker": {"in_title": False}},
            "role_family": "backend",
            "seniority": "senior",
        }
        res = scorer.score(posting)
        self.assertIn("score", res)
        self.assertEqual(res["matched_skills"], ["docker", "python"])
        self.assertEqual(res["missing_skills"], [])


class ApiSkillDetailOpenVocabularyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_unknown_skill_returns_404(self) -> None:
        resp = self.client.get("/api/skills/definitely_nonexistent_skill_xyz_123")
        self.assertEqual(resp.status_code, 404)

    def test_profile_open_vocabulary_skill_is_accessible(self) -> None:
        with (
            mock.patch(
                "careerradar.market.repository.get_skill_drilldown_postings", return_value=[]
            ),
            mock.patch("careerradar.market.repository.get_skill_cooccurring", return_value=[]),
            mock.patch("careerradar.market.repository.get_skill_by_family", return_value=[]),
        ):
            resp = self.client.get("/api/skills/python")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["skill"], "python")
            self.assertIn("label", data)
            self.assertIn("category", data)
