"""Scheduler and circuit-breaker tests.

Two invariants get simulation tests rather than single-call assertions, because both fail
silently in production and would be invisible in a spot check:

  Eventual coverage -- a rotating scheduler that permanently starves some cells produces a
  dashboard where "no Kubernetes roles in Helsinki" and "my Helsinki query broke" look
  identical.

  hours_old >= 1.5x the revisit gap -- if violated, postings existed inside the unobserved
  gap, and the resulting flow estimate is too low with no downstream symptom.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.search.guard import (
    ERROR_BLOCKED,
    ERROR_FATAL,
    ERROR_RATE_LIMIT,
    ERROR_TRANSIENT,
    SourceCircuit,
    SourceTripped,
    classify_error,
)
from careerradar.search.scheduler import (
    QUALITY_SAMPLE_MIN,
    CellState,
    adaptive_hours_old,
    cell_priority,
    estimate_pages,
    estimate_units,
    is_eligible,
    is_saturated,
    overdue_cells,
    quality_multiplier,
    select_cells,
    starved_cells,
    update_ewma,
    with_location_weights,
)
from careerradar.taxonomy.roles import RoleTaxonomy, load_roles

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

CONFIG = {
    "page_size": {"indeed": 15, "linkedin": 25},
    "requests_per_description": {"indeed": 0, "linkedin": 1},
    "cadence_hours": 24,
    "hours_old_floor": 72,
    "max_hours_old": 336,
    "backfill_hours_old": 336,
    "max_staleness_hours": 72,
    "budgets": {
        "indeed": {
            "searches_per_run": 20,
            "request_units": 200,
            "results_wanted_default": 75,
            "max_results_wanted": 200,
            "fetch_descriptions": True,
        },
        "linkedin": {
            "searches_per_run": 6,
            "request_units": 60,
            "results_wanted_default": 50,
            "max_results_wanted": 100,
            "fetch_descriptions": False,
        },
    },
    "locations": {
        "us_remote": {"weight": 1.0},
        "us_nat": {"weight": 0.3},
        "los_angeles": {"weight": 1.0},
        "helsinki": {"weight": 0.5},
        "stockholm": {"weight": 0.5},
        "oslo": {"weight": 0.5},
        "copenhagen": {"weight": 0.5},
    },
}


def make_cells(source: str = "indeed", roles: RoleTaxonomy | None = None) -> list[CellState]:
    """Build the real cell matrix for one source."""
    roles = roles or load_roles()
    cells: list[CellState] = []
    for index, spec in enumerate(roles.cell_specs(sources=(source,)), start=1):
        cells.append(
            CellState(
                id=index,
                source=spec["source"],
                role_family=spec["role_family"],
                location_id=spec["location_id"],
                query=spec["query"],
                active=spec.get("active", True),
            )
        )
    return cells


class EligibilityTests(unittest.TestCase):
    def test_disabled_cell_is_ineligible(self) -> None:
        cell = CellState(1, "indeed", "x", "la", "q", enabled=0)
        self.assertFalse(is_eligible(cell, NOW))

    def test_cell_in_backoff_is_ineligible(self) -> None:
        cell = CellState(
            1, "indeed", "x", "la", "q", backoff_until=(NOW + timedelta(hours=2)).isoformat()
        )
        self.assertFalse(is_eligible(cell, NOW))

    def test_expired_backoff_is_eligible_again(self) -> None:
        cell = CellState(
            1, "indeed", "x", "la", "q", backoff_until=(NOW - timedelta(hours=1)).isoformat()
        )
        self.assertTrue(is_eligible(cell, NOW))


class PriorityTests(unittest.TestCase):
    def test_staleness_raises_priority(self) -> None:
        fresh = CellState(
            1,
            "indeed",
            "x",
            "la",
            "q",
            active=True,
            last_scraped_at=NOW.isoformat(),
            total_scrapes=5,
        )
        stale = CellState(
            2,
            "indeed",
            "x",
            "la",
            "q",
            active=True,
            total_scrapes=5,
            last_scraped_at=(NOW - timedelta(days=5)).isoformat(),
        )
        self.assertGreater(cell_priority(stale, CONFIG, NOW), cell_priority(fresh, CONFIG, NOW))

    def test_more_stale_cell_outranks_less_stale(self) -> None:
        base: dict[str, Any] = {
            "total_scrapes": 5,
            "active": True,
        }
        more_stale = CellState(
            1,
            "indeed",
            "x",
            "la",
            "q",
            last_scraped_at=(NOW - timedelta(days=10)).isoformat(),
            **base,
        )
        less_stale = CellState(
            2,
            "indeed",
            "x",
            "la",
            "q",
            last_scraped_at=(NOW - timedelta(days=2)).isoformat(),
            **base,
        )
        self.assertGreater(
            cell_priority(more_stale, CONFIG, NOW),
            cell_priority(less_stale, CONFIG, NOW),
        )

    def test_location_weight_matters(self) -> None:
        base: dict[str, Any] = {
            "active": True,
            "total_scrapes": 5,
            "last_scraped_at": (NOW - timedelta(days=3)).isoformat(),
        }
        high = CellState(1, "indeed", "x", "us_remote", "q", **base)
        low = CellState(2, "indeed", "x", "us_nat", "q", **base)
        self.assertGreater(cell_priority(high, CONFIG, NOW), cell_priority(low, CONFIG, NOW))

    def test_never_scraped_cell_gets_a_novelty_boost(self) -> None:
        base: dict[str, Any] = {
            "active": True,
            "last_scraped_at": (NOW - timedelta(days=3)).isoformat(),
        }
        fresh = CellState(1, "indeed", "x", "la", "q", total_scrapes=0, **base)
        seen = CellState(2, "indeed", "x", "la", "q", total_scrapes=10, **base)
        self.assertGreater(cell_priority(fresh, CONFIG, NOW), cell_priority(seen, CONFIG, NOW))

    def test_saturated_cell_is_revisited_sooner(self) -> None:
        base: dict[str, Any] = {
            "active": True,
            "total_scrapes": 5,
            "last_scraped_at": (NOW - timedelta(days=3)).isoformat(),
        }
        saturated = CellState(1, "indeed", "x", "la", "q", last_saturated=1, **base)
        normal = CellState(2, "indeed", "x", "la", "q", last_saturated=0, **base)
        self.assertGreater(
            cell_priority(saturated, CONFIG, NOW), cell_priority(normal, CONFIG, NOW)
        )

    def test_repeatedly_empty_cell_is_deprioritised_but_never_zeroed(self) -> None:
        base: dict[str, Any] = {
            "active": True,
            "total_scrapes": 5,
            "last_scraped_at": (NOW - timedelta(days=3)).isoformat(),
        }
        empty = CellState(1, "indeed", "x", "la", "q", consecutive_empty=8, **base)
        normal = CellState(2, "indeed", "x", "la", "q", consecutive_empty=0, **base)
        self.assertLess(cell_priority(empty, CONFIG, NOW), cell_priority(normal, CONFIG, NOW))
        # Must stay strictly positive, so a quiet family is still probed occasionally.
        self.assertGreater(cell_priority(empty, CONFIG, NOW), 0)

    def test_quality_multiplier_is_neutral_without_enough_samples(self) -> None:
        cell = CellState(1, "indeed", "x", "la", "q", ewma_fit_score=10, quality_samples=1)
        self.assertEqual(quality_multiplier(cell), 1.0)
        cell_never_scored = CellState(2, "indeed", "x", "la", "q")
        self.assertEqual(quality_multiplier(cell_never_scored), 1.0)

    def test_quality_multiplier_rewards_and_penalizes_once_sampled(self) -> None:
        strong = CellState(
            1, "indeed", "x", "la", "q", ewma_fit_score=90, quality_samples=QUALITY_SAMPLE_MIN
        )
        weak = CellState(
            2, "indeed", "x", "la", "q", ewma_fit_score=10, quality_samples=QUALITY_SAMPLE_MIN
        )
        self.assertGreater(quality_multiplier(strong), 1.0)
        self.assertLess(quality_multiplier(weak), 1.0)
        # Clamped: even a terrible track record can only ever dampen, never zero out.
        self.assertGreaterEqual(quality_multiplier(weak), 0.5)

    def test_high_quality_cell_outranks_low_quality_cell_at_equal_yield(self) -> None:
        base: dict[str, Any] = {
            "active": True,
            "total_scrapes": 5,
            "last_scraped_at": (NOW - timedelta(days=3)).isoformat(),
            "ewma_new_per_scrape": 5.0,
            "quality_samples": QUALITY_SAMPLE_MIN,
        }
        strong = CellState(1, "indeed", "x", "la", "q", ewma_fit_score=90, **base)
        weak = CellState(2, "indeed", "x", "la", "q", ewma_fit_score=10, **base)
        self.assertGreater(cell_priority(strong, CONFIG, NOW), cell_priority(weak, CONFIG, NOW))
        # A poor track record must never be able to zero out an otherwise-productive cell.
        self.assertGreater(cell_priority(weak, CONFIG, NOW), 0)

    def test_unproven_cell_is_not_starved_by_a_thin_quality_sample(self) -> None:
        """A cell with zero prior scrapes must still get a fair look regardless of quality
        -- QUALITY_SAMPLE_MIN keeps quality neutral until there's enough signal to trust,
        so it must never compound with the novelty boost to starve a brand-new cell."""
        base: dict[str, Any] = {
            "active": True,
            "last_scraped_at": (NOW - timedelta(days=3)).isoformat(),
        }
        never_scraped = CellState(1, "indeed", "x", "la", "q", total_scrapes=0, **base)
        seasoned_but_poor = CellState(
            2,
            "indeed",
            "x",
            "la",
            "q",
            total_scrapes=10,
            ewma_fit_score=10,
            quality_samples=QUALITY_SAMPLE_MIN,
            **base,
        )
        self.assertGreater(
            cell_priority(never_scraped, CONFIG, NOW),
            cell_priority(seasoned_but_poor, CONFIG, NOW),
        )


class AdaptiveHoursOldTests(unittest.TestCase):
    def test_meets_the_one_point_five_times_gap_invariant(self) -> None:
        """The core correctness property: never leave an unobserved gap."""
        for gap_hours in (12, 24, 48, 72, 100, 150):
            cell = CellState(
                1,
                "indeed",
                "x",
                "la",
                "q",
                active=True,
                last_success_at=(NOW - timedelta(hours=gap_hours)).isoformat(),
            )
            hours_old = adaptive_hours_old(cell, CONFIG, NOW)
            self.assertGreaterEqual(
                hours_old,
                min(1.5 * gap_hours, CONFIG["max_hours_old"]),
                f"gap={gap_hours}h produced hours_old={hours_old}",
            )

    def test_respects_the_floor(self) -> None:
        cell = CellState(
            1,
            "indeed",
            "x",
            "la",
            "q",
            active=True,
            last_success_at=(NOW - timedelta(hours=1)).isoformat(),
        )
        self.assertGreaterEqual(adaptive_hours_old(cell, CONFIG, NOW), 72)

    def test_is_capped(self) -> None:
        cell = CellState(
            1,
            "indeed",
            "x",
            "la",
            "q",
            active=True,
            last_success_at=(NOW - timedelta(days=90)).isoformat(),
        )
        self.assertLessEqual(adaptive_hours_old(cell, CONFIG, NOW), 336)

    def test_never_succeeded_cell_uses_the_backfill_window(self) -> None:
        cell = CellState(1, "indeed", "x", "la", "q", active=True)
        self.assertEqual(adaptive_hours_old(cell, CONFIG, NOW), 336)


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = load_roles()

    def test_respects_searches_per_run(self) -> None:
        cells = make_cells("indeed", self.roles)
        tasks = select_cells(cells, CONFIG, self.roles, "indeed", NOW)
        self.assertLessEqual(
            (tasks and len(tasks)) or 0, CONFIG["budgets"]["indeed"]["searches_per_run"]
        )

    def test_respects_request_unit_budget(self) -> None:
        cells = make_cells("indeed", self.roles)
        tasks = select_cells(cells, CONFIG, self.roles, "indeed", NOW)
        units = sum(t.est_request_units for t in tasks)
        self.assertLessEqual(units, CONFIG["budgets"]["indeed"]["request_units"])

    def test_respects_linkedin_page_ceiling(self) -> None:
        cells = make_cells("linkedin", self.roles)
        cfg = dict(CONFIG)
        cfg["budgets"] = dict(
            CONFIG["budgets"],
            linkedin=dict(CONFIG["budgets"]["linkedin"], max_pages_per_run=10),
        )
        tasks = select_cells(cells, cfg, self.roles, "linkedin", NOW)
        pages = sum(estimate_pages("linkedin", t.results_wanted, cfg) for t in tasks)
        self.assertLessEqual(pages, 10)

    def test_indeed_tasks_request_a_description_census(self) -> None:
        cells = make_cells("indeed", self.roles)
        tasks = select_cells(cells, CONFIG, self.roles, "indeed", NOW)
        for task in tasks:
            self.assertTrue(task.fetch_description)
            self.assertEqual(task.desc_selection, "census")

    def test_linkedin_without_proxies_fetches_no_descriptions(self) -> None:
        cells = make_cells("linkedin", self.roles)
        tasks = select_cells(cells, CONFIG, self.roles, "linkedin", NOW)
        for task in tasks:
            self.assertFalse(task.fetch_description)
            self.assertEqual(task.desc_selection, "none")

    def test_proxies_turn_linkedin_into_a_description_census(self) -> None:
        cells = make_cells("linkedin", self.roles)
        cfg = dict(CONFIG)
        cfg["budgets"] = dict(
            CONFIG["budgets"],
            linkedin=dict(
                CONFIG["budgets"]["linkedin"],
                fetch_descriptions=True,
                desc_selection="census",
            ),
        )
        tasks = select_cells(cells, cfg, self.roles, "linkedin", NOW)
        for task in tasks:
            self.assertTrue(task.fetch_description)
            self.assertEqual(task.desc_selection, "census")

    def test_linkedin_is_budgeted_far_below_indeed(self) -> None:
        """A budget sanity check: Indeed does 20 searches/run against LinkedIn's 6."""
        self.assertGreater(
            CONFIG["budgets"]["indeed"]["searches_per_run"],
            CONFIG["budgets"]["linkedin"]["searches_per_run"],
        )

    def test_tasks_carry_location_and_country_from_the_query(self) -> None:
        cells = make_cells("indeed", self.roles)
        tasks = select_cells(cells, CONFIG, self.roles, "indeed", NOW)
        for task in tasks:
            self.assertTrue(task.location_label)
            self.assertTrue(task.country)

    def test_backfill_maximises_results_and_window(self) -> None:
        cells = make_cells("indeed", self.roles)
        tasks = select_cells(cells, CONFIG, self.roles, "indeed", NOW, backfill=True)
        for task in tasks:
            self.assertEqual(task.results_wanted, CONFIG["budgets"]["indeed"]["max_results_wanted"])
            self.assertEqual(task.hours_old, CONFIG["backfill_hours_old"])


class StalenessFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = load_roles()

    def test_overdue_core_cell_is_forced_into_the_run(self) -> None:
        cells = make_cells("indeed", self.roles)
        for cell in cells:
            cell.last_scraped_at = NOW.isoformat()
            cell.last_success_at = NOW.isoformat()
            cell.total_scrapes = 10
            cell.ewma_new_per_scrape = 20.0
        overdue = cells[-1]
        overdue.last_success_at = (NOW - timedelta(days=5)).isoformat()

        tasks = select_cells(cells, CONFIG, self.roles, "indeed", NOW)
        self.assertIn(overdue.id, {t.cell_id for t in tasks})

    def test_overdue_keys_on_success_not_attempt(self) -> None:
        """A cell that keeps failing must still read as overdue.

        If the floor keyed on last_scraped_at, a persistently broken cell would look fresh
        and the dashboard would report coverage of data never collected.
        """
        cells = [
            CellState(
                1,
                "indeed",
                "ai_engineer",
                "us_remote",
                "q",
                active=True,
                last_scraped_at=NOW.isoformat(),
                last_success_at=(NOW - timedelta(days=5)).isoformat(),
            )
        ]
        self.assertEqual(len(overdue_cells(cells, CONFIG, NOW)), 1)

    def test_fresh_cells_are_not_overdue(self) -> None:
        cells = [
            CellState(
                1,
                "indeed",
                "ai_engineer",
                "us_remote",
                "q",
                active=True,
                last_scraped_at=NOW.isoformat(),
                last_success_at=NOW.isoformat(),
            )
        ]
        self.assertEqual(overdue_cells(cells, CONFIG, NOW), [])

    def test_low_weight_locations_are_not_floor_guaranteed(self) -> None:
        cells = [
            CellState(
                1,
                "indeed",
                "ai_engineer",
                "us_nat",
                "q",
                active=True,
                last_success_at=(NOW - timedelta(days=30)).isoformat(),
            )
        ]
        self.assertEqual(overdue_cells(cells, CONFIG, NOW), [])


class LocationWeightWiringTests(unittest.TestCase):
    """Weights are declared on target locations and consumed off the scraper config.

    These assert the *bridge* rather than the policy. Every weight-dependent test above
    hand-builds CONFIG with a "locations" key, so all of them passed while production
    passed a config that had none -- every location weighed 1.0 and the >= 1.0 filters
    matched everything. Testing the policy is not enough; the wiring needs its own test.
    """

    def setUp(self) -> None:
        self.roles = load_roles()

    def test_weights_are_populated_from_target_locations(self) -> None:
        merged = with_location_weights({"cadence_hours": 24}, self.roles)
        self.assertTrue(merged["locations"])
        for location_id, location in self.roles.locations.items():
            self.assertEqual(merged["locations"][location_id]["weight"], location.weight)

    def test_fixture_ids_match_target_locations(self) -> None:
        """Guards the specific rot that hid the bug: renamed ids, silent lookup misses."""
        self.assertEqual(set(CONFIG["locations"]), set(self.roles.locations))

    def test_bridge_changes_who_counts_as_overdue(self) -> None:
        """The regression itself: a low-weight cell is only excluded once weights arrive."""
        cells = [
            CellState(
                1,
                "indeed",
                "ai_engineer",
                "us_nat",
                "q",
                active=True,
                last_success_at=(NOW - timedelta(days=30)).isoformat(),
            )
        ]
        bare = {"max_staleness_hours": 72}
        self.assertEqual(len(overdue_cells(cells, bare, NOW)), 1)
        self.assertEqual(overdue_cells(cells, with_location_weights(bare, self.roles), NOW), [])

    def test_priority_is_damped_by_location_weight(self) -> None:
        merged = with_location_weights(CONFIG, self.roles)
        commutable = CellState(1, "indeed", "ai_engineer", "los_angeles", "q", active=True)
        relocation = CellState(2, "indeed", "ai_engineer", "us_nat", "q", active=True)
        self.assertGreater(
            cell_priority(commutable, merged, NOW),
            cell_priority(relocation, merged, NOW),
        )


class StarvationDeadlineTests(unittest.TestCase):
    """The explicit coverage guarantee under uniform cadence."""

    def setUp(self) -> None:
        self.roles = load_roles()
        self.config = dict(CONFIG, starvation_multiple=4)

    def _cell(self, hours_stale: float, **kwargs: Any) -> CellState:
        return CellState(
            1,
            "indeed",
            "ai_engineer",
            "us_remote",
            "q",
            active=True,
            last_scraped_at=(NOW - timedelta(hours=hours_stale)).isoformat(),
            **kwargs,
        )

    def test_deadline_scales_with_cadence(self) -> None:
        # uniform cadence 24h x 4 = 96h.
        self.assertTrue(starved_cells([self._cell(97)], self.config, NOW))
        self.assertFalse(starved_cells([self._cell(95)], self.config, NOW))

    def test_starved_cells_ignore_backoff(self) -> None:
        cell = self._cell(500, backoff_until=(NOW + timedelta(hours=5)).isoformat())
        self.assertEqual(starved_cells([cell], self.config, NOW), [])

    def test_starved_cells_are_scheduled_ahead_of_priority(self) -> None:
        cells = make_cells("indeed", self.roles)
        for cell in cells:
            cell.last_scraped_at = NOW.isoformat()
            cell.last_success_at = NOW.isoformat()
            cell.total_scrapes = 20
            cell.ewma_new_per_scrape = 30.0  # very productive, so high priority
        # One low-yield, repeatedly-empty cell past its deadline: the exact
        # profile that the priority formula alone pushes to the back forever.
        starving = cells[-1]
        starving.last_scraped_at = (NOW - timedelta(days=40)).isoformat()
        starving.consecutive_empty = 5
        starving.ewma_new_per_scrape = 0.0

        tasks = select_cells(cells, self.config, self.roles, "indeed", NOW)
        self.assertIn(starving.id, {t.cell_id for t in tasks})

    def test_most_overdue_comes_first(self) -> None:
        cell1 = self._cell(480)
        cell1.id = 1
        cell2 = CellState(
            2,
            "indeed",
            "devrel",
            "us_remote",
            "q",
            active=True,
            last_scraped_at=(NOW - timedelta(hours=700)).isoformat(),
        )
        order = starved_cells([cell1, cell2], self.config, NOW)
        self.assertEqual(order[0].id, cell2.id)


class EventualCoverageSimulationTests(unittest.TestCase):
    """Simulate many runs and assert the rotation actually covers the matrix."""

    def setUp(self) -> None:
        self.roles = load_roles()

    def _simulate(
        self,
        runs: int,
        source: str = "indeed",
        runs_per_day: int = 2,
        empties: tuple[str, ...] = (),
    ) -> tuple[dict[int, int], list[CellState]]:
        cells = make_cells(source, self.roles)
        by_id = {c.id: c for c in cells}
        visits = {c.id: 0 for c in cells}
        clock = NOW

        for _ in range(runs):
            tasks = select_cells(cells, CONFIG, self.roles, source, clock)
            for task in tasks:
                cell = by_id[task.cell_id]
                visits[cell.id] += 1
                cell.last_scraped_at = clock.isoformat()
                cell.total_scrapes += 1
                if cell.role_family in empties:
                    cell.consecutive_empty += 1
                    cell.last_result_count = 0
                    cell.last_success_at = clock.isoformat()
                else:
                    cell.consecutive_empty = 0
                    cell.last_success_at = clock.isoformat()
                    cell.last_result_count = 30
                    cell.ewma_new_per_scrape = update_ewma(cell.ewma_new_per_scrape, 5)
            clock += timedelta(hours=24 / runs_per_day)

        return visits, cells

    def test_every_cell_is_visited_within_a_month_of_runs(self) -> None:
        visits, _ = self._simulate(runs=60)  # 30 days at 2 runs/day
        unvisited = [cid for cid, count in visits.items() if count == 0]
        self.assertEqual(unvisited, [], f"{len(unvisited)} cells never visited")

    def test_every_cell_is_visited_repeatedly_over_a_long_horizon(self) -> None:
        visits, _ = self._simulate(runs=200)
        starved = [cid for cid, count in visits.items() if count < 3]
        self.assertEqual(starved, [], f"{len(starved)} cells visited fewer than 3 times")

    def test_all_cells_receive_coverage_under_uniform_cadence(self) -> None:
        visits, _ = self._simulate(runs=120)
        self.assertTrue(all(v > 0 for v in visits.values()))

    def test_full_matrix_cycle_completes_in_about_five_days(self) -> None:
        """The derived constraint behind analytics.min_window_days = 30."""
        cells = make_cells("indeed", self.roles)
        by_id = {c.id: c for c in cells}
        seen = set()
        clock = NOW
        runs = 0
        while len(seen) < len(cells) and runs < 100:
            for task in select_cells(cells, CONFIG, self.roles, "indeed", clock):
                seen.add(task.cell_id)
                cell = by_id[task.cell_id]
                cell.last_scraped_at = clock.isoformat()
                cell.last_success_at = clock.isoformat()
                cell.total_scrapes += 1
            clock += timedelta(hours=12)
            runs += 1
        days = (clock - NOW).total_seconds() / 86400
        self.assertLessEqual(days, 8, f"full cycle took {days:.1f} days")

    def test_persistently_empty_family_is_still_probed(self) -> None:
        """Otherwise "the market moved" and "my query broke" look the same."""
        visits, cells = self._simulate(runs=200, empties=("devrel",))
        by_id = {c.id: c for c in cells}
        devrel_visits = [v for cid, v in visits.items() if by_id[cid].role_family == "devrel"]
        self.assertTrue(devrel_visits)
        self.assertTrue(all(v >= 2 for v in devrel_visits), devrel_visits)


class SaturationTests(unittest.TestCase):
    def test_saturation_detected_at_the_threshold(self) -> None:
        self.assertTrue(is_saturated(75, 75))
        self.assertTrue(is_saturated(72, 75))  # within 95%
        self.assertFalse(is_saturated(37, 75))
        self.assertFalse(is_saturated(0, 75))

    def test_zero_requested_is_not_saturated(self) -> None:
        self.assertFalse(is_saturated(0, 0))


class EwmaTests(unittest.TestCase):
    def test_first_observation_seeds_the_average(self) -> None:
        self.assertEqual(update_ewma(None, 10), 10.0)

    def test_subsequent_observations_are_smoothed(self) -> None:
        self.assertAlmostEqual(update_ewma(10, 0, alpha=0.4), 6.0)


class UnitCostTests(unittest.TestCase):
    """What a cell is charged against `request_units`.

    The bug these pin down cost the LinkedIn budget an order of magnitude: while the
    estimate counted search pages only, a 50-posting description census was charged 5
    requests and spent 56, so `request_units` bought a tenth of the cells it claimed.
    """

    def test_pages_include_the_request_that_finds_the_end(self) -> None:
        # 75 results at 15 per page is 5 pages, plus the one that discovers exhaustion.
        self.assertEqual(estimate_pages("indeed", 75, CONFIG), 6)
        self.assertEqual(estimate_pages("linkedin", 75, CONFIG), 4)

    def test_pages_only_when_descriptions_are_not_fetched(self) -> None:
        self.assertEqual(estimate_units("linkedin", 75, CONFIG), 4)

    def test_a_description_census_is_charged_per_posting(self) -> None:
        self.assertEqual(estimate_units("linkedin", 75, CONFIG, fetch_descriptions=True), 79)

    def test_a_board_that_serves_descriptions_in_page_is_charged_nothing_extra(self) -> None:
        """Indeed returns descriptions inside the search page, so a census there is free."""
        self.assertEqual(estimate_units("indeed", 75, CONFIG, fetch_descriptions=True), 6)

    def test_the_estimate_matches_a_measured_cell(self) -> None:
        """56 requests measured on 2026-08-15 for a 50-posting LinkedIn census cell."""
        live = {"page_size": {"linkedin": 10}, "requests_per_description": {"linkedin": 1}}
        self.assertEqual(estimate_units("linkedin", 50, live, fetch_descriptions=True), 56)


class ErrorClassificationTests(unittest.TestCase):
    def test_rate_limit_is_recognised(self) -> None:
        for exc in [
            Exception("HTTP 429 Too Many Requests"),
            Exception("rate limited by server"),
            Exception("Response 429"),
        ]:
            self.assertEqual(classify_error(exc), ERROR_RATE_LIMIT, str(exc))

    def test_blocking_is_recognised(self) -> None:
        for exc in [
            Exception("403 Forbidden"),
            Exception("captcha challenge issued"),
            Exception("unusual traffic detected"),
        ]:
            self.assertEqual(classify_error(exc), ERROR_BLOCKED, str(exc))

    def test_transient_is_recognised(self) -> None:
        for exc in [
            Exception("503 Service Unavailable"),
            Exception("read timeout"),
            Exception("connection reset by peer"),
        ]:
            self.assertEqual(classify_error(exc), ERROR_TRANSIENT, str(exc))

    def test_unknown_is_fatal(self) -> None:
        self.assertEqual(classify_error(ValueError("bad shape")), ERROR_FATAL)


class FakeDb:
    def __init__(self) -> None:
        self.backoff: dict[str, str] = {}
        self.trips: dict[str, int] = {}
        self.reasons: dict[str, str | None] = {}

    def get_source_backoff(self, source: str) -> str | None:
        return self.backoff.get(source)

    def get_source_trips(self, source: str) -> int:
        return self.trips.get(source, 0)

    def set_source_backoff(
        self, source: str, until: str, reason: str | None = None, escalate: bool = False
    ) -> None:
        self.backoff[source] = until
        self.reasons[source] = reason
        if escalate:
            self.trips[source] = self.trips.get(source, 0) + 1

    def reset_source_trips(self, source: str) -> None:
        self.trips[source] = 0


class CircuitBreakerTests(unittest.TestCase):
    def _circuit(self, db: Any = None) -> SourceCircuit:
        return SourceCircuit("linkedin", CONFIG, db=db, now=lambda: NOW, sleep=lambda _s: None)

    def test_429_trips_immediately_and_is_not_retried(self) -> None:
        """Departs from http_client.py on purpose: a 429 means the IP is flagged."""
        db = FakeDb()
        circuit = self._circuit(db)
        error_class = circuit.on_error(Exception("HTTP 429 Too Many Requests"))
        self.assertEqual(error_class, ERROR_RATE_LIMIT)
        self.assertTrue(circuit.is_open)
        self.assertIn("linkedin", db.backoff)

    def test_open_circuit_refuses_further_requests(self) -> None:
        circuit = self._circuit(FakeDb())
        circuit.on_error(Exception("429"))
        with self.assertRaises(SourceTripped):
            circuit.before_request()

    def test_transient_errors_trip_only_after_the_threshold(self) -> None:
        circuit = self._circuit(FakeDb())
        circuit.on_error(Exception("timeout"))
        self.assertFalse(circuit.is_open)
        circuit.on_error(Exception("timeout"))
        self.assertFalse(circuit.is_open)
        circuit.on_error(Exception("timeout"))
        self.assertTrue(circuit.is_open)

    def test_success_resets_the_consecutive_error_count(self) -> None:
        circuit = self._circuit(FakeDb())
        circuit.on_error(Exception("timeout"))
        circuit.on_error(Exception("timeout"))
        circuit.on_success(10)
        circuit.on_error(Exception("timeout"))
        self.assertFalse(circuit.is_open)

    def test_empty_result_is_not_an_error(self) -> None:
        circuit = self._circuit(FakeDb())
        for _ in range(5):
            circuit.on_empty()
        self.assertFalse(circuit.is_open)

    def test_backoff_escalates_across_trips(self) -> None:
        db = FakeDb()
        first = self._circuit(db)
        first.on_error(Exception("429"))
        first_until = db.backoff["linkedin"]

        second = self._circuit(db)
        second.on_error(Exception("429"))
        second_until = db.backoff["linkedin"]
        self.assertGreater(second_until, first_until)

    def test_persisted_backoff_blocks_a_later_run(self) -> None:
        db = FakeDb()
        db.backoff["linkedin"] = (NOW + timedelta(hours=1)).isoformat()
        self.assertTrue(self._circuit(db).persisted_backoff_active())

    def test_expired_persisted_backoff_does_not_block(self) -> None:
        db = FakeDb()
        db.backoff["linkedin"] = (NOW - timedelta(hours=1)).isoformat()
        self.assertFalse(self._circuit(db).persisted_backoff_active())

    def test_clean_run_decays_the_escalation_counter(self) -> None:
        """Otherwise one bad afternoon pins the source at a 24h backoff forever."""
        db = FakeDb()
        db.trips["linkedin"] = 3
        circuit = self._circuit(db)
        for _ in range(5):
            circuit.on_success(10)
        circuit.note_clean_run()
        self.assertEqual(db.trips["linkedin"], 0)

    def test_cell_backoff_escalates(self) -> None:
        circuit = self._circuit(FakeDb())
        first = circuit.cell_backoff(1)
        third = circuit.cell_backoff(3)
        self.assertLess(first, third)

    def test_summary_reports_state(self) -> None:
        circuit = self._circuit(FakeDb())
        circuit.on_error(Exception("429 rate limited"))
        summary = circuit.summary()
        self.assertTrue(summary["tripped"])
        self.assertEqual(summary["source"], "linkedin")
        self.assertEqual(summary["rate_limit_hits"], 1)


if __name__ == "__main__":
    unittest.main()
