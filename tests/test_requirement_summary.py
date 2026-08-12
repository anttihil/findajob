"""The must-have counter behind the card badge.

What this guards is a join, not arithmetic. `core_requirements` carries the importance and
`requirement_assessments` carries the status; they are two JSON columns joined on the
requirement text the model was told to repeat verbatim. `profile/models.py` validates that
repetition after normalising with `strip().casefold()`, so a summary that normalised any
differently would report an unassessed must-have on a verdict the validator had already
accepted -- a discrepancy visible only as a wrong number on a card.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import _requirement_summary  # noqa: E402


def core(requirement, importance="must_have", quote="quoted"):
    return {"requirement": requirement, "importance": importance, "quote": quote}


def assessed(requirement, status, evidence=None):
    return {"requirement": requirement, "status": status,
            "candidate_evidence": evidence}


class RequirementSummaryTests(unittest.TestCase):

    def test_counts_only_must_haves(self):
        summary = _requirement_summary(
            [core("Go"), core("Kafka", "nice_to_have")],
            [assessed("Go", "met"), assessed("Kafka", "unmet")],
        )
        self.assertEqual(summary["must_total"], 1)
        self.assertEqual(summary["must_met"], 1)
        self.assertEqual(summary["must_unmet"], 0)

    def test_partial_is_not_folded_into_met(self):
        summary = _requirement_summary(
            [core("5+ years"), core("Go")],
            [assessed("5+ years", "partial"), assessed("Go", "met")],
        )
        self.assertEqual((summary["must_met"], summary["must_partial"]), (1, 1))

    def test_the_join_matches_the_validator_normalisation(self):
        # `models.py` accepts this verdict: the assessment repeats the requirement up to
        # case and surrounding whitespace. The summary has to agree, or the card reports
        # an unassessed must-have on a verdict nothing else considers incomplete.
        summary = _requirement_summary(
            [core("Distributed Systems")],
            [assessed("  distributed systems ", "met")],
        )
        self.assertEqual(summary["must_met"], 1)
        self.assertEqual(summary["must_unassessed"], 0)

    def test_an_unassessed_must_have_is_reported_not_dropped(self):
        summary = _requirement_summary([core("Rust")], [])
        self.assertEqual(summary["must_total"], 1)
        self.assertEqual(summary["must_unassessed"], 1)
        self.assertEqual(summary["must_met"], 0)

    def test_a_row_without_extraction_summarises_to_nothing(self):
        # Verdicts written before the ordinal schema have no requirements at all. "0 of 0
        # must-haves met" would read as a finding about the posting rather than about the
        # age of the row, so the badge has to be absent instead.
        self.assertIsNone(_requirement_summary([], []))
        self.assertIsNone(_requirement_summary(None, None))

    def test_a_posting_with_no_must_haves_summarises_to_nothing(self):
        self.assertIsNone(
            _requirement_summary([core("Kafka", "nice_to_have")],
                                 [assessed("Kafka", "met")])
        )


if __name__ == "__main__":
    unittest.main()
