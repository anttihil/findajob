import os
import sys
import unittest
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.market.gap_analysis import GapAnalysis
from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.models import MasterSkillCategory, Profile
from careerradar.web.app import app


class SkillsGapApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_skills_gap_endpoint_returns_200_and_expected_structure(self):
        resp = self.client.get("/api/skills/gap?window_days=90")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("views", data)
        self.assertIn("priority_gaps", data["views"])
        self.assertIn("validated_strengths", data["views"])
        self.assertIn("dead_weight", data["views"])
        self.assertIn("suppressed", data["views"])
        self.assertIn("provenance", data)

    def test_unknown_skill_returns_404(self) -> None:
        resp = self.client.get("/api/skills/definitely_nonexistent_skill_xyz_123")
        self.assertEqual(resp.status_code, 404)


class GapAnalysisLabellingTests(unittest.TestCase):
    def test_rows_are_labelled_from_the_profile(self) -> None:
        profile = Profile(
            name="Candidate",
            skills=[
                MasterSkillCategory(
                    category="Backend",
                    skills=["Go", "Kubernetes", "FastAPI"],
                )
            ],
        )
        adapter = ProfileAdapter(profile)
        mock_db = mock.Mock()
        mock_db.conn = mock.Mock()
        mock_targets = mock.Mock()
        mock_targets.fingerprint = "roles123"

        gap = GapAnalysis(
            db=mock_db,
            config={},
            targets=mock_targets,
            profile=adapter,
        )

        stats = {
            "fastapi": {
                "n_active": 10,
                "n_good_fit": 5,
                "raw_count": 8,
                "companies": {"Acme": 5, "Beta": 3},
                "in_title": 2,
                "blocking_count": 0,
                "familiar_share_sum": 4.0,
                "familiar_share_n": 4,
                "salaries": [],
                "baseline_salary": 120000,
            },
            "kafka": {
                "n_active": 10,
                "n_good_fit": 5,
                "raw_count": 6,
                "companies": {"Acme": 4, "Gamma": 2},
                "in_title": 0,
                "blocking_count": 3,
                "familiar_share_sum": 3.0,
                "familiar_share_n": 3,
                "salaries": [],
                "baseline_salary": 120000,
            },
        }

        rows = gap._score_rows(stats)
        self.assertEqual(len(rows), 2)

        fastapi_row = next(r for r in rows if r["skill"] == "fastapi")
        self.assertEqual(fastapi_row["label"], "FastAPI")
        self.assertEqual(fastapi_row["category"], "Backend")
        self.assertTrue(fastapi_row["user_has"])

        kafka_row = next(r for r in rows if r["skill"] == "kafka")
        self.assertEqual(kafka_row["label"], "Kafka")
        self.assertEqual(kafka_row["category"], "other")
        self.assertFalse(kafka_row["user_has"])


if __name__ == "__main__":
    unittest.main()
