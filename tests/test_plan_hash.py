"""Tests for plan_hash generation and plan change detection."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.search.runner import plan_hash


class PlanHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = mock.MagicMock()
        self.roles.hash = "mock_roles_hash_123"

    def test_plan_hash_with_integer_cadence(self) -> None:
        config = {
            "scraper": {
                "sources": {"indeed": True, "linkedin": False},
                "cadence_hours": 24,
                "budgets": {"indeed": {}, "linkedin": {}},
            }
        }
        h = plan_hash(self.roles, config)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 16)

    def test_plan_hash_with_empty_scraper_config(self) -> None:
        h = plan_hash(self.roles, {})
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 16)

    def test_plan_hash_deterministic(self) -> None:
        config_a = {
            "scraper": {
                "sources": {"linkedin": True, "indeed": True},
                "cadence_hours": 24,
            }
        }
        config_b = {
            "scraper": {
                "sources": {"indeed": True, "linkedin": True},
                "cadence_hours": 24,
            }
        }
        self.assertEqual(plan_hash(self.roles, config_a), plan_hash(self.roles, config_b))

    def test_plan_hash_changes_with_cadence_or_roles(self) -> None:
        config_24 = {"scraper": {"cadence_hours": 24}}
        config_48 = {"scraper": {"cadence_hours": 48}}
        self.assertNotEqual(
            plan_hash(self.roles, config_24),
            plan_hash(self.roles, config_48),
        )


if __name__ == "__main__":
    unittest.main()
