"""Ordering and projection over the ordinal tuple.

The tuple space is 180 points, so nothing here samples: every property is checked
exhaustively. That is affordable exactly once, and this is the module where it matters --
`scale.py` is the only thing standing between a set of categorical judgements and the
number the dashboard sorts on, and a subtle asymmetry in it would be invisible in any
individual verdict.
"""

import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.scoring import rubric, scale  # noqa: E402

TUPLES = list(itertools.product(rubric.ELIGIBILITY, rubric.ROLE_MATCH,
                                rubric.CAPABILITY_MATCH, rubric.SENIORITY_GAP,
                                rubric.EVIDENCE_QUALITY))

PARETO_POINTS = list(itertools.product(range(len(rubric.ROLE_MATCH)),
                                       range(len(rubric.CAPABILITY_MATCH)),
                                       range(len(rubric.SENIORITY_GAP))))


def as_kwargs(t):
    return dict(zip(rubric.DIMENSIONS, t))


class ParetoTests(unittest.TestCase):
    """The tier is the ranking that does NOT invent an exchange rate."""

    def test_dominance_always_means_a_strictly_better_tier(self):
        for a, b in itertools.product(PARETO_POINTS, repeat=2):
            if scale._dominates(a, b):
                self.assertLess(scale._TIERS[a], scale._TIERS[b], f"{a} dominates {b}")

    def test_points_sharing_a_tier_are_mutually_incomparable(self):
        """A tie must mean 'we cannot say', never 'these are equally good'."""
        for a, b in itertools.product(PARETO_POINTS, repeat=2):
            if scale._TIERS[a] == scale._TIERS[b]:
                self.assertFalse(scale._dominates(a, b), f"{a} dominates same-tier {b}")

    def test_the_layering_covers_the_grid_exactly_once(self):
        self.assertEqual(len(scale._TIERS), 60)
        self.assertEqual(sorted(scale._TIERS), sorted(PARETO_POINTS))

    def test_tiers_are_contiguous_from_one(self):
        self.assertEqual(sorted(set(scale._TIERS.values())),
                         list(range(1, scale.MAX_TIER + 1)))

    def test_the_best_and_worst_corners_are_alone_in_their_tiers(self):
        best = scale.pareto_tier(role_match="same_role", capability_match="exceeds",
                                 seniority_gap="matched")
        worst = scale.pareto_tier(role_match="different_field",
                                  capability_match="not_close",
                                  seniority_gap="candidate_below")
        self.assertEqual(best, 1)
        self.assertEqual(worst, scale.MAX_TIER)


class EligibilityTests(unittest.TestCase):
    def test_eligibility_partitions_ahead_of_every_tier(self):
        """The worst eligible verdict still sorts before the best blocked one.

        This is the structural fix for the stored verdict that scored 100 while its own
        reasoning named a language blocker.
        """
        worst_eligible = scale.sort_key({
            "eligibility": "eligible", "role_match": "different_field",
            "capability_match": "not_close", "seniority_gap": "candidate_below"})
        best_blocked = scale.sort_key({
            "eligibility": "blocked", "role_match": "same_role",
            "capability_match": "exceeds", "seniority_gap": "matched"})
        self.assertLess(worst_eligible, best_blocked)

    def test_blocked_cannot_reach_the_bottom_band(self):
        for t in TUPLES:
            if t[0] != "blocked":
                continue
            score = scale.fit_score(**as_kwargs(t))
            self.assertLessEqual(score, 5, t)
            self.assertEqual(scale.band_for(score), "mismatch", t)

    def test_conditional_is_demoted_but_stays_visible(self):
        """Not a veto: a clearance the candidate could obtain should not vanish."""
        for t in TUPLES:
            if t[0] != "conditional":
                continue
            self.assertLessEqual(scale.fit_score(**as_kwargs(t)), 45, t)
        best = scale.fit_score(eligibility="conditional", role_match="same_role",
                               capability_match="exceeds", seniority_gap="matched",
                               evidence_quality="strong")
        self.assertEqual(scale.band_for(best), "stretch")

    def test_relaxing_eligibility_never_lowers_the_score(self):
        for rest in itertools.product(rubric.ROLE_MATCH, rubric.CAPABILITY_MATCH,
                                      rubric.SENIORITY_GAP, rubric.EVIDENCE_QUALITY):
            scores = [scale.fit_score(**as_kwargs((e,) + rest))
                      for e in rubric.ELIGIBILITY]
            self.assertEqual(scores, sorted(scores, reverse=True), rest)


class ProjectionTests(unittest.TestCase):
    def test_every_tuple_projects_into_range(self):
        for t in TUPLES:
            self.assertIn(scale.fit_score(**as_kwargs(t)), range(0, 101), t)

    def test_the_score_is_monotone_on_every_dimension(self):
        """Improving one ordinal, holding the rest fixed, never lowers the score."""
        for dimension in rubric.DIMENSIONS:
            if dimension == "evidence_quality":
                continue  # shrinks toward the middle by design; covered separately
            values = rubric.ANCHORS[dimension][0]
            others = [rubric.ANCHORS[d][0] for d in rubric.DIMENSIONS if d != dimension]
            for combo in itertools.product(*others):
                base = dict(zip([d for d in rubric.DIMENSIONS if d != dimension], combo))
                scores = [scale.fit_score(**base, **{dimension: v}) for v in values]
                self.assertEqual(scores, sorted(scores, reverse=True),
                                 f"{dimension} not monotone at {base}")

    def test_a_thin_posting_is_pulled_toward_the_middle_not_downward(self):
        """Thin evidence means we know less, so we should claim less in BOTH directions."""
        good = dict(eligibility="eligible", role_match="same_role",
                    capability_match="exceeds", seniority_gap="matched")
        bad = dict(eligibility="eligible", role_match="different_field",
                   capability_match="not_close", seniority_gap="matched")
        self.assertLess(scale.fit_score(**good, evidence_quality="thin"),
                        scale.fit_score(**good, evidence_quality="strong"))
        self.assertGreater(scale.fit_score(**bad, evidence_quality="thin"),
                           scale.fit_score(**bad, evidence_quality="strong"))

    def test_the_top_of_the_scale_is_reachable(self):
        """The scale the model emitted never produced 79, 80 or 81 at all."""
        top = scale.fit_score(eligibility="eligible", role_match="same_role",
                              capability_match="exceeds", seniority_gap="matched",
                              evidence_quality="strong")
        self.assertGreaterEqual(top, 90)
        self.assertEqual(scale.band_for(top), "strong")

    def test_every_band_is_reachable(self):
        reached = {scale.band_for(scale.fit_score(**as_kwargs(t))) for t in TUPLES}
        self.assertEqual(reached, {name for name, _low in scale.BANDS})

    def test_resolution_beats_what_the_model_used_unaided(self):
        """53 distinct values across 5,511 verdicts, clustered on a handful of anchors."""
        distinct = {scale.fit_score(**as_kwargs(t)) for t in TUPLES if t[0] == "eligible"}
        self.assertGreater(len(distinct), 53)

    def test_band_agrees_with_the_score_by_construction(self):
        for t in TUPLES:
            projected = scale.project(dict(as_kwargs(t)))
            self.assertEqual(projected["verdict"],
                             scale.band_for(projected["fit_score"]), t)

    def test_project_reports_the_scale_version_it_used(self):
        projected = scale.project(dict(as_kwargs(TUPLES[0])))
        self.assertEqual(projected["scale_version"], scale.SCALE_VERSION)
        self.assertIn("pareto_tier", projected)


class GridTests(unittest.TestCase):
    def test_the_grid_covers_every_role_and_capability(self):
        self.assertEqual(set(scale.GRID), set(rubric.ROLE_MATCH))
        for row in scale.GRID.values():
            self.assertEqual(len(row), len(rubric.CAPABILITY_MATCH))

    def test_the_grid_is_monotone_along_both_axes(self):
        for role in rubric.ROLE_MATCH:
            row = scale.GRID[role]
            self.assertEqual(list(row), sorted(row, reverse=True), role)
        for i in range(len(rubric.CAPABILITY_MATCH)):
            column = [scale.GRID[role][i] for role in rubric.ROLE_MATCH]
            self.assertEqual(column, sorted(column, reverse=True), i)

    def test_the_adjustment_tables_cover_their_scales(self):
        self.assertEqual(set(scale.SENIORITY_DELTA), set(rubric.SENIORITY_GAP))
        self.assertEqual(set(scale.ELIGIBILITY_CEILING), set(rubric.ELIGIBILITY))


if __name__ == "__main__":
    unittest.main()
