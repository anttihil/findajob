"""Verdict-distribution observability.

The bug this guards against is not a crash. It is a scoring agent whose judgements have
quietly stopped discriminating: every posting lands in the same cell, every verdict stays
individually defensible, and the ranking the pipeline rests on flattens. Nothing in one
verdict shows that.

The old suite checked band cliffs -- whether the model stepped over a boundary rather than
into it. That was the right question while the model chose the numbers. It computes them
now, so the check would only confirm its own arithmetic; what replaces it is the ordinal
marginals, where a collapse says WHICH judgement stopped discriminating.
"""

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.scoring import rubric
from careerradar.scoring.stats import COLLAPSE_SHARE, render


def stats(**overrides: Any) -> dict[str, Any]:
    base = {
        "profile_version": 4,
        "total": 100,
        "scale_versions": {1: 100},
        "counts": {70: 60, 30: 40},
        "verdicts": {"worth_applying": 60, "poor_fit": 40},
        "tiers": {3: 60, 7: 40},
        "marginals": {
            "eligibility": {"eligible": 60, "blocked": 40},
            "role_match": {"adjacent": 60, "different_domain": 40},
            "capability_match": {"meets": 60, "major_gaps": 40},
            "seniority_gap": {"matched": 100},
            "evidence_quality": {"strong": 100},
        },
        "crosstab": {("adjacent", "meets"): 60, ("different_domain", "major_gaps"): 40},
        "collapsed": ["seniority_gap", "evidence_quality"],
        "blockers": 40,
        "audit_flags": {"quote_repaired": 3},
        "distinct_scores": 2,
        "cost": 0.5,
    }
    base.update(overrides)
    return base


class MixedScaleTests(unittest.TestCase):
    """Two scales in one corpus have no joint distribution worth rendering."""

    def test_a_mixed_corpus_is_refused_rather_than_averaged(self) -> None:
        out = render(stats(scale_versions={0: 5000, 1: 55}))
        self.assertIn("more than one", out)
        self.assertIn("--scale-version", out)
        # And it must not print a distribution anyway.
        self.assertNotIn("ordinal marginals", out)

    def test_the_refusal_says_what_scale_zero_means(self) -> None:
        out = render(stats(scale_versions={0: 5000, 1: 55}))
        self.assertIn("the model emitted the number itself", out)

    def test_a_single_scale_renders(self) -> None:
        self.assertIn("ordinal marginals", render(stats()))


class MarginalTests(unittest.TestCase):
    def test_a_collapsed_dimension_is_called_out(self) -> None:
        out = render(stats())
        self.assertIn("seniority_gap   <-- collapsed", out)

    def test_a_discriminating_dimension_is_not_flagged(self) -> None:
        out = render(stats())
        self.assertNotIn("role_match   <-- collapsed", out)

    def test_every_value_is_listed_even_at_zero(self) -> None:
        """An unused value is the finding; omitting it hides exactly what we look for."""
        out = render(stats())
        for value in rubric.CAPABILITY_MATCH:
            self.assertIn(value, out)

    def test_the_collapse_threshold_is_a_share_not_a_count(self) -> None:
        self.assertLess(COLLAPSE_SHARE, 1.0)
        self.assertGreater(COLLAPSE_SHARE, 0.5)


class RenderTests(unittest.TestCase):
    def test_no_verdicts_says_so_instead_of_rendering_an_empty_chart(self) -> None:
        self.assertIn("No verdicts", render({"profile_version": 9, "total": 0}))

    def test_no_active_profile_is_a_message_not_a_traceback(self) -> None:
        self.assertIn("profile build", render(None))

    def test_the_crosstab_covers_the_grid(self) -> None:
        out = render(stats())
        for role in rubric.ROLE_MATCH:
            self.assertIn(role, out)

    def test_the_bands_are_labelled_as_a_projection(self) -> None:
        """They are derived from the ordinals, so they measure nothing on their own."""
        self.assertIn("projection of the ordinals", render(stats()))

    def test_a_scale_zero_corpus_explains_its_empty_marginals(self) -> None:
        out = render(
            stats(
                scale_versions={0: 100},
                marginals={n: {} for n in rubric.DIMENSIONS},
                crosstab={},
                collapsed=[],
            )
        )
        self.assertIn("Scale 0", out)
        self.assertIn("empty by construction", out)

    def test_a_contradicted_blocker_is_the_flag_that_gets_pointed_at(self) -> None:
        out = render(stats(audit_flags={"blocker_contradicts_profile": 12}))
        self.assertIn("a blocker the candidate does not have", out)


if __name__ == "__main__":
    unittest.main()
