"""Status-report tests.

The report exists to catch a silently stopped stage, so what is worth pinning is that the
warnings actually fire on the states that were missed in production: a scorer that has not
written a verdict in days, and verdict coverage that is far apart between two sources.
Both are computed from data the report itself gathers, so an in-memory fixture is enough.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.status import COVERAGE_SKEW, render


def _report(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    report = {
        "generated_at": now.isoformat(),
        "search": {
            "last_run": (now - timedelta(hours=6)).isoformat(),
            "hours_since": 6.0,
            "status": "ok",
            "cells": (30, 32),
            "postings_new": 400,
            "runs_per_day_7d": 2.0,
        },
        "score": {
            "last_verdict": (now - timedelta(hours=2)).isoformat(),
            "hours_since": 2.0,
            "backlog": 120,
            "backlog_share": 0.05,
            "oldest_unscored": (now - timedelta(days=3)).isoformat(),
            "oldest_unscored_days": 3.0,
        },
        "research": {"last_run": None, "hours_since": None, "status": None, "dossiers": 0},
        "coverage": [
            {"source": "linkedin", "postings": 1000, "scored": 670, "share": 0.67},
            {"source": "indeed", "postings": 1000, "scored": 660, "share": 0.66},
        ],
        "cells": [
            {
                "tier": "core",
                "cells": 210,
                "never_scraped": 0,
                "oldest_success_hours": 30.0,
                "cadence_hours": 24,
                "stale": False,
                "erroring": 0,
                "backed_off": 0,
            }
        ],
        "quarantined": [],
        "taxonomy": {"skills_hash": "abc", "roles_hash": "def", "stored": [("abc", 100)]},
    }
    report.update(overrides)
    return report


class StatusRenderTests(unittest.TestCase):
    def test_healthy_report_raises_nothing(self) -> None:
        out = render(_report())
        self.assertNotIn("!", out)

    def test_stalled_scorer_is_called_out(self) -> None:
        """The production failure: the timer was disabled and nothing said so."""
        score = dict(_report()["score"])
        score["hours_since"] = 24 * 7
        out = render(_report(score=score))
        self.assertIn("scoring looks stalled", out)
        self.assertIn("careerradar-score.timer", out)

    def test_never_scored_corpus_is_called_out(self) -> None:
        score = dict(_report()["score"])
        score.update({"last_verdict": None, "hours_since": None})
        self.assertIn("no verdicts stored", render(_report(score=score)))

    def test_coverage_skew_between_sources_is_called_out(self) -> None:
        coverage = [
            {"source": "linkedin", "postings": 1000, "scored": 670, "share": 0.67},
            {"source": "indeed", "postings": 1000, "scored": 270, "share": 0.27},
        ]
        out = render(_report(coverage=coverage))
        self.assertIn("coverage spread", out)

    def test_small_sources_do_not_trigger_the_skew_warning(self) -> None:
        """A 12-posting feed at 100% must not be read as a skew against a 65% board."""
        coverage = [
            {"source": "linkedin", "postings": 1000, "scored": 650, "share": 0.65},
            {"source": "hackernews", "postings": 12, "scored": 12, "share": 1.0},
        ]
        self.assertNotIn("coverage spread", render(_report(coverage=coverage)))

    def test_skew_threshold_is_the_documented_one(self) -> None:
        self.assertAlmostEqual(COVERAGE_SKEW, 0.20)

    def test_quarantined_postings_are_listed_with_a_remedy(self) -> None:
        quarantined = [{"id": 7, "title": "AI Engineer", "error": "validation error: status"}]
        out = render(_report(quarantined=quarantined))
        self.assertIn("#7", out)
        self.assertIn("careerradar score retry", out)

    def test_stale_taxonomy_hash_is_marked(self) -> None:
        taxonomy = {"skills_hash": "abc", "roles_hash": "def", "stored": [("old", 900)]}
        self.assertIn("(stale)", render(_report(taxonomy=taxonomy)))


if __name__ == "__main__":
    unittest.main()
