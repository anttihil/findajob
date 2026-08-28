import os
import sys
import unittest

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.web.app import app


class SkillsGapApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_skills_gap_endpoint_returns_200_and_expected_structure(self):
        resp = self.client.get("/api/skills/gap?window_days=90&weighting=interest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("views", data)
        self.assertIn("priority_gaps", data["views"])
        self.assertIn("validated_strengths", data["views"])
        self.assertIn("dead_weight", data["views"])
        self.assertIn("suppressed", data["views"])
        self.assertIn("provenance", data)
