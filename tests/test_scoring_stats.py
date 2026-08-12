"""Verdict-distribution observability.

The bug this guards against is not a crash. It is a scoring prompt that makes the model
avoid the top of its scale: every verdict stays defensible while the ranking the whole
pipeline rests on quietly flattens. These tests pin the arithmetic that makes that
visible.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.scoring.stats import band_for, boundary_cliff, render  # noqa: E402


class BandTests(unittest.TestCase):
    def test_every_score_in_range_lands_in_exactly_one_band(self):
        self.assertEqual([band_for(s) for s in (0, 19, 20, 39, 40, 59, 60, 79, 80, 100)],
                         ["mismatch", "mismatch", "poor_fit", "poor_fit", "stretch",
                          "stretch", "worth_applying", "worth_applying", "strong",
                          "strong"])

    def test_a_score_outside_the_scale_is_not_forced_into_a_band(self):
        self.assertIsNone(band_for(101))
        self.assertIsNone(band_for(-1))


class CliffTests(unittest.TestCase):
    def test_a_boundary_the_model_steps_over_is_visible_as_an_empty_upper_side(self):
        counts = {76: 12, 77: 4, 78: 19, 79: 0, 80: 0, 81: 0, 82: 0, 83: 0}
        cliff = boundary_cliff(counts, 80)
        self.assertEqual(cliff["below"], 35)
        self.assertEqual(cliff["at_or_above"], 0)
        self.assertEqual(cliff["empty_scores"], [79, 80, 81, 82, 83])

    def test_a_boundary_in_ordinary_use_shows_mass_on_both_sides(self):
        counts = {56: 10, 57: 8, 58: 9, 59: 7, 60: 11, 61: 6, 62: 12, 63: 5}
        cliff = boundary_cliff(counts, 60)
        self.assertEqual((cliff["below"], cliff["at_or_above"]), (34, 34))
        self.assertEqual(cliff["empty_scores"], [])

    def test_an_empty_corpus_reports_no_cliff_rather_than_dividing_by_zero(self):
        cliff = boundary_cliff({}, 80)
        self.assertEqual((cliff["below"], cliff["at_or_above"]), (0, 0))


class RenderTests(unittest.TestCase):
    def stats(self, counts, **overrides):
        verdicts = {}
        for score, n in counts.items():
            verdicts[band_for(score)] = verdicts.get(band_for(score), 0) + n
        base = {
            "profile_version": 4, "total": sum(counts.values()), "counts": counts,
            "verdicts": verdicts, "distinct_scores": len(counts), "mislabelled": 0,
            "blockers": 0, "blockers_not_mismatch": 0, "cost": 1.5,
            "cliffs": [boundary_cliff(counts, low) for low in (80, 60, 40, 20)],
        }
        base.update(overrides)
        return base

    def test_an_unused_top_band_is_called_out_not_just_left_at_zero(self):
        rendered = render(self.stats({72: 147, 78: 19}))
        self.assertIn("nothing crosses", rendered)

    def test_a_healthy_distribution_is_not_flagged(self):
        rendered = render(self.stats({72: 40, 82: 35, 55: 30, 25: 20}))
        self.assertNotIn("nothing crosses", rendered)

    def test_no_verdicts_says_so_instead_of_rendering_an_empty_chart(self):
        self.assertIn("No verdicts", render({"profile_version": 9, "total": 0}))

    def test_no_active_profile_is_a_message_not_a_traceback(self):
        self.assertIn("profile build", render(None))
