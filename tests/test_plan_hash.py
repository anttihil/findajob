"""Tests for plan_hash generation and plan change detection."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.search.runner import plan_hash
from careerradar.search.targets import SearchTargets


class PlanHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = mock.MagicMock()
        self.targets.fingerprint = "mock_search_targets_fingerprint"

    def test_plan_hash_with_integer_cadence(self) -> None:
        config = {
            "scraper": {
                "sources": {"indeed": True, "linkedin": False},
                "cadence_hours": 24,
                "budgets": {"indeed": {}, "linkedin": {}},
            }
        }
        h = plan_hash(self.targets, config)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 16)

    def test_plan_hash_with_empty_scraper_config(self) -> None:
        h = plan_hash(self.targets, {})
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
        self.assertEqual(plan_hash(self.targets, config_a), plan_hash(self.targets, config_b))

    def test_plan_hash_changes_with_cadence_or_targets(self) -> None:
        config_24 = {"scraper": {"cadence_hours": 24}}
        config_48 = {"scraper": {"cadence_hours": 48}}
        self.assertNotEqual(
            plan_hash(self.targets, config_24),
            plan_hash(self.targets, config_48),
        )

    def test_plan_hash_changes_when_enabled_targets_change(self) -> None:
        config = {"scraper": {"cadence_hours": 24}}
        base = SearchTargets.from_spec(
            {
                "queries": ["Backend Engineer"],
                "locations": [{"id": "remote", "label": "Remote", "enabled": True}],
            }
        )
        changed = SearchTargets.from_spec(
            {
                "queries": ["Platform Engineer"],
                "locations": [{"id": "remote", "label": "Remote", "enabled": True}],
            }
        )
        self.assertNotEqual(plan_hash(base, config), plan_hash(changed, config))


if __name__ == "__main__":
    unittest.main()
