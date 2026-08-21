"""Verdict-distribution observability tests."""

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.scoring.stats import render


def stats(**overrides: Any) -> dict[str, Any]:
    base = {
        "profile_version": 4,
        "total": 100,
        "fit_count": 15,
        "no_fit_count": 85,
        "unscored_fit_count": 0,
        "reasons": {
            "skills": 45,
            "seniority": 20,
            "match": 15,
            "domain": 10,
            "location": 10,
        },
        "cost": 0.5,
    }
    base.update(overrides)
    return base


class RenderTests(unittest.TestCase):
    def test_renders_fit_breakdown_and_reasons(self) -> None:
        out = render(stats())
        self.assertIn("profile v4 · 100 verdicts · $0.5000", out)
        self.assertIn("fit breakdown:", out)
        self.assertIn("fit (>= 90% match):      15  (15.0%)", out)
        self.assertIn("no fit:                  85  (85.0%)", out)
        self.assertIn("reason_type distribution:", out)
        self.assertIn("skills", out)
        self.assertIn("seniority", out)

    def test_no_verdicts_says_so(self) -> None:
        self.assertIn("No verdicts", render({"profile_version": 9, "total": 0}))

    def test_no_active_profile_is_a_message(self) -> None:
        self.assertIn("profile build", render(None))


if __name__ == "__main__":
    unittest.main()
