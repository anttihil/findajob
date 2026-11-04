"""Scheduler and circuit-breaker tests.

Two invariants get simulation tests rather than single-call assertions, because both fail
silently in production and would be invisible in a spot check:

  Eventual coverage -- a rotating scheduler that permanently starves some cells produces a
  dashboard where "no Kubernetes targets in Helsinki" and "my Helsinki query broke" look
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

from findajob.core.database import Database
from findajob.search.guard import (
    ERROR_BLOCKED,
    ERROR_FATAL,
    ERROR_PROXY,
    ERROR_RATE_LIMIT,
    ERROR_TRANSIENT,
    SourceCircuit,
    SourceTripped,
    classify_error,
)
from findajob.search.proxies import proxy_id
from findajob.search.scheduler import (
    CellState,
    adaptive_hours_old,
    estimate_pages,
    estimate_units,
    is_eligible,
    is_saturated,
    overdue_cells,
    select_cells,
)
from findajob.search.targets import SearchTargets

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

SCHEDULER_TEST_TARGETS_SPEC = {
    "queries": [
        "AI Engineer",
        "Machine Learning Engineer",
        "Platform Engineer",
        "Developer Relations",
    ],
    "locations": [
        {
            "id": "us_remote",
            "label": "United States (remote)",
            "search_label": "Remote",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": True,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "us_nat",
            "label": "United States (onsite, nationwide)",
            "search_label": "United States",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": False,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "los_angeles",
            "label": "Los Angeles, CA",
            "search_label": "Los Angeles, CA",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": False,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "helsinki",
            "label": "Helsinki, Finland",
            "search_label": "Helsinki",
            "country": "FI",
            "indeed_country": "finland",
            "is_remote": False,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "stockholm",
            "label": "Stockholm, Sweden",
            "search_label": "Stockholm",
            "country": "SE",
            "indeed_country": "sweden",
            "is_remote": False,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "oslo",
            "label": "Oslo, Norway",
            "search_label": "Oslo",
            "country": "NO",
            "indeed_country": "norway",
            "is_remote": False,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "copenhagen",
            "label": "Copenhagen, Denmark",
            "search_label": "Copenhagen",
            "country": "DK",
            "indeed_country": "denmark",
            "is_remote": False,
            "distance": 50,
            "enabled": True,
        },
    ],
}

CONFIG = {
    "page_size": {"indeed": 15, "linkedin": 25},
    "requests_per_description": {"indeed": 0, "linkedin": 1},
    "cadence_hours": 24,
    "hours_old_floor": 72,
    "max_hours_old": 336,
    "backfill_hours_old": 336,
    "max_staleness_hours": 72,
    "policies": {
        "indeed": {
            "searches_per_run": 20,
            "request_units": 200,
            "results_wanted": 75,
            "max_results_wanted": 200,
            "fetch_descriptions": True,
        },
        "linkedin": {
            "searches_per_run": 6,
            "request_units": 60,
            "results_wanted": 50,
            "max_results_wanted": 100,
            "fetch_descriptions": False,
        },
    },
}


def make_cells(source: str = "indeed", targets: SearchTargets | None = None) -> list[CellState]:
    """Build the real cell matrix for one source."""
    targets = targets or SearchTargets.from_spec(SCHEDULER_TEST_TARGETS_SPEC)
    cells: list[CellState] = []
    for index, spec in enumerate(targets.cell_specs(sources=(source,)), start=1):
        cells.append(
            CellState(
                id=index,
                source=spec["source"],
                location_id=spec["location_id"],
                query=spec["query"],
                search_label=spec["search_label"],
                country=spec["country"],
                indeed_country=spec["indeed_country"],
                is_remote=bool(spec["is_remote"]),
                distance=spec["distance"],
            )
        )
    return cells


class EligibilityTests(unittest.TestCase):
    def test_disabled_cell_is_ineligible(self) -> None:
        cell = CellState(1, "indeed", "la", "q", enabled=0)
        self.assertFalse(is_eligible(cell, NOW))

    def test_cell_in_backoff_is_ineligible(self) -> None:
        cell = CellState(
            1, "indeed", "la", "q", backoff_until=(NOW + timedelta(hours=2)).isoformat()
        )
        self.assertFalse(is_eligible(cell, NOW))

    def test_expired_backoff_is_eligible_again(self) -> None:
        cell = CellState(
            1, "indeed", "la", "q", backoff_until=(NOW - timedelta(hours=1)).isoformat()
        )
        self.assertTrue(is_eligible(cell, NOW))


class OrderingTests(unittest.TestCase):
    """Ordering is oldest-attempt-first, and nothing else.

    The six-term priority product this replaced could not outweigh staleness for more than
    a few runs -- staleness was unbounded and every other term was clamped -- so the
    schedule converged on this rotation anyway.
    """

    def setUp(self) -> None:
        self.targets = SearchTargets.from_spec(SCHEDULER_TEST_TARGETS_SPEC)

    def test_the_more_stale_cell_is_scheduled_first(self) -> None:
        fresh = CellState(1, "indeed", "la", "q", last_scraped_at=NOW.isoformat())
        stale = CellState(
            2, "indeed", "la", "q", last_scraped_at=(NOW - timedelta(days=5)).isoformat()
        )
        tasks = select_cells([fresh, stale], CONFIG, "indeed", NOW)
        self.assertEqual([t.cell_id for t in tasks], [2, 1])

    def test_a_never_scraped_cell_sorts_ahead_of_every_scraped_one(self) -> None:
        never = CellState(1, "indeed", "la", "q")
        scraped = CellState(
            2, "indeed", "la", "q", last_scraped_at=(NOW - timedelta(days=5)).isoformat()
        )
        tasks = select_cells([never, scraped], CONFIG, "indeed", NOW)
        self.assertEqual([t.cell_id for t in tasks], [1, 2])

    def test_ties_break_on_id_so_the_order_is_deterministic(self) -> None:
        cells = [
            CellState(i, "indeed", "la", f"q{i}", last_scraped_at=NOW.isoformat())
            for i in (3, 1, 2)
        ]
        tasks = select_cells(cells, CONFIG, "indeed", NOW)
        self.assertEqual([t.cell_id for t in tasks], [1, 2, 3])

    def test_ordering_keys_on_the_attempt_not_the_success(self) -> None:
        """A cell that keeps failing must back off, not monopolise the budget.

        `record_cell_attempt` advances last_scraped_at on every attempt and last_success_at
        only on success. Sorting on success would put a permanently broken cell at the
        front of every run forever.
        """
        broken = CellState(
            1,
            "indeed",
            "la",
            "q",
            last_scraped_at=NOW.isoformat(),
            last_success_at=(NOW - timedelta(days=30)).isoformat(),
        )
        waiting = CellState(
            2,
            "indeed",
            "la",
            "q",
            last_scraped_at=(NOW - timedelta(days=2)).isoformat(),
            last_success_at=(NOW - timedelta(days=2)).isoformat(),
        )
        tasks = select_cells([broken, waiting], CONFIG, "indeed", NOW)
        self.assertEqual([t.cell_id for t in tasks], [2, 1])

    def test_a_cell_skipped_for_budget_leads_the_next_run(self) -> None:
        """The self-correcting property that makes a priority function unnecessary."""
        cells = make_cells("indeed", self.targets)
        first = select_cells(cells, CONFIG, "indeed", NOW)[:3]
        picked = {t.cell_id for t in first}
        skipped = {c.id for c in cells if c.id not in picked}
        self.assertTrue(skipped, "budget must be tighter than the matrix for this test")
        for cell in cells:
            if cell.id in picked:
                cell.last_scraped_at = NOW.isoformat()

        second = select_cells(cells, CONFIG, "indeed", NOW + timedelta(hours=12))
        self.assertTrue(skipped <= {t.cell_id for t in second})


class AdaptiveHoursOldTests(unittest.TestCase):
    def test_meets_the_one_point_five_times_gap_invariant(self) -> None:
        """The core correctness property: never leave an unobserved gap."""
        for gap_hours in (12, 24, 48, 72, 100, 150):
            cell = CellState(
                1,
                "indeed",
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
            "la",
            "q",
            active=True,
            last_success_at=(NOW - timedelta(days=90)).isoformat(),
        )
        self.assertLessEqual(adaptive_hours_old(cell, CONFIG, NOW), 336)

    def test_never_succeeded_cell_uses_the_backfill_window(self) -> None:
        cell = CellState(1, "indeed", "la", "q", active=True)
        self.assertEqual(adaptive_hours_old(cell, CONFIG, NOW), 336)


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = SearchTargets.from_spec(SCHEDULER_TEST_TARGETS_SPEC)

    def test_all_eligible_cells_are_candidates(self) -> None:
        cells = make_cells("indeed", self.targets)
        tasks = select_cells(cells, CONFIG, "indeed", NOW)
        self.assertEqual(len(tasks), len(cells))

    def test_estimated_requests_do_not_exclude_candidate_cells(self) -> None:
        cells = make_cells("indeed", self.targets)
        cfg = dict(CONFIG, budgets={"indeed": dict(CONFIG["policies"]["indeed"], request_units=1)})
        tasks = select_cells(cells, cfg, "indeed", NOW)
        units = sum(estimate_units(t.source, t.results_wanted, cfg) for t in tasks)
        self.assertGreater(units, 1)
        self.assertEqual(len(tasks), len(cells))

    def test_legacy_estimated_page_ceiling_does_not_exclude_candidate_cells(self) -> None:
        cells = make_cells("linkedin", self.targets)
        cfg = dict(CONFIG)
        cfg["policies"] = dict(
            CONFIG["policies"],
            linkedin=dict(CONFIG["policies"]["linkedin"], max_pages_per_run=10),
        )
        tasks = select_cells(cells, cfg, "linkedin", NOW)
        pages = sum(estimate_pages("linkedin", t.results_wanted, cfg) for t in tasks)
        self.assertGreater(pages, 10)
        self.assertEqual(len(tasks), len(cells))

    def test_indeed_tasks_request_a_description_census(self) -> None:
        cells = make_cells("indeed", self.targets)
        tasks = select_cells(cells, CONFIG, "indeed", NOW)
        for task in tasks:
            self.assertTrue(task.fetch_description)
            self.assertEqual(task.desc_selection, "census")

    def test_linkedin_without_proxies_fetches_descriptions(self) -> None:
        cells = make_cells("linkedin", self.targets)
        tasks = select_cells(cells, CONFIG, "linkedin", NOW)
        for task in tasks:
            self.assertTrue(task.fetch_description)
            self.assertEqual(task.desc_selection, "census")

    def test_proxies_turn_linkedin_into_a_description_census(self) -> None:
        cells = make_cells("linkedin", self.targets)
        cfg = dict(CONFIG)
        cfg["policies"] = dict(
            CONFIG["policies"],
            linkedin=dict(
                CONFIG["policies"]["linkedin"],
                fetch_descriptions=True,
                desc_selection="census",
            ),
        )
        tasks = select_cells(cells, cfg, "linkedin", NOW)
        for task in tasks:
            self.assertTrue(task.fetch_description)
            self.assertEqual(task.desc_selection, "census")

    def test_linkedin_is_budgeted_far_below_indeed(self) -> None:
        """A budget sanity check: Indeed does 20 searches/run against LinkedIn's 6."""
        self.assertGreater(
            CONFIG["policies"]["indeed"]["searches_per_run"],
            CONFIG["policies"]["linkedin"]["searches_per_run"],
        )

    def test_tasks_carry_location_and_country_from_the_query(self) -> None:
        cells = make_cells("indeed", self.targets)
        tasks = select_cells(cells, CONFIG, "indeed", NOW)
        for task in tasks:
            self.assertTrue(task.location_label)
            self.assertTrue(task.country)

    def test_backfill_preserves_fixed_depth_and_uses_backfill_window(self) -> None:
        cells = make_cells("indeed", self.targets)
        tasks = select_cells(cells, CONFIG, "indeed", NOW, backfill=True)
        for task in tasks:
            self.assertEqual(task.results_wanted, CONFIG["policies"]["indeed"]["results_wanted"])
            self.assertEqual(task.hours_old, CONFIG["backfill_hours_old"])


class StalenessFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = SearchTargets.from_spec(SCHEDULER_TEST_TARGETS_SPEC)

    def test_overdue_keys_on_success_not_attempt(self) -> None:
        """A cell that keeps failing must still read as overdue.

        If the floor keyed on last_scraped_at, a persistently broken cell would look fresh
        and the dashboard would report coverage of data never collected.
        """
        cells = [
            CellState(
                1,
                "indeed",
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
                "us_remote",
                "q",
                active=True,
                last_scraped_at=NOW.isoformat(),
                last_success_at=NOW.isoformat(),
            )
        ]
        self.assertEqual(overdue_cells(cells, CONFIG, NOW), [])

    def test_disabled_cells_are_not_overdue(self) -> None:
        """A paused cell is not expected back, so it cannot fall behind."""
        cells = [
            CellState(
                1,
                "indeed",
                "us_nat",
                "q",
                enabled=0,
                last_success_at=(NOW - timedelta(days=30)).isoformat(),
            )
        ]
        self.assertEqual(overdue_cells(cells, CONFIG, NOW), [])


class LongStaleCellTests(unittest.TestCase):
    """Cells the old priority product could push to the back of the queue."""

    def setUp(self) -> None:
        self.targets = SearchTargets.from_spec(SCHEDULER_TEST_TARGETS_SPEC)

    def test_a_long_stale_cell_is_scheduled_first(self) -> None:
        cells = make_cells("indeed", self.targets)
        for cell in cells:
            cell.last_scraped_at = NOW.isoformat()
            cell.last_success_at = NOW.isoformat()
        starving = cells[-1]
        starving.last_scraped_at = (NOW - timedelta(days=40)).isoformat()

        tasks = select_cells(cells, CONFIG, "indeed", NOW)
        self.assertEqual(tasks[0].cell_id, starving.id)

    def test_backoff_still_wins_over_staleness(self) -> None:
        """A blocked source must not be hammered just because its cells went stale."""
        cells = make_cells("indeed", self.targets)
        for cell in cells:
            cell.last_scraped_at = NOW.isoformat()
        blocked = cells[-1]
        blocked.last_scraped_at = (NOW - timedelta(days=40)).isoformat()
        blocked.backoff_until = (NOW + timedelta(hours=5)).isoformat()

        tasks = select_cells(cells, CONFIG, "indeed", NOW)
        self.assertNotIn(blocked.id, {t.cell_id for t in tasks})


class EventualCoverageSimulationTests(unittest.TestCase):
    """Simulate many runs and assert the rotation actually covers the matrix."""

    def setUp(self) -> None:
        self.targets = SearchTargets.from_spec(SCHEDULER_TEST_TARGETS_SPEC)

    def _simulate(
        self,
        runs: int,
        source: str = "indeed",
        runs_per_day: int = 2,
    ) -> tuple[dict[int, int], list[CellState]]:
        cells = make_cells(source, self.targets)
        by_id = {c.id: c for c in cells}
        visits = {c.id: 0 for c in cells}
        clock = NOW

        for _ in range(runs):
            tasks = select_cells(cells, CONFIG, source, clock)
            for task in tasks:
                cell = by_id[task.cell_id]
                visits[cell.id] += 1
                cell.last_scraped_at = clock.isoformat()
                cell.last_success_at = clock.isoformat()
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
        cells = make_cells("indeed", self.targets)
        by_id = {c.id: c for c in cells}
        seen = set()
        clock = NOW
        runs = 0
        while len(seen) < len(cells) and runs < 100:
            for task in select_cells(cells, CONFIG, "indeed", clock):
                seen.add(task.cell_id)
                cell = by_id[task.cell_id]
                cell.last_scraped_at = clock.isoformat()
                cell.last_success_at = clock.isoformat()
            clock += timedelta(hours=12)
            runs += 1
        days = (clock - NOW).total_seconds() / 86400
        self.assertLessEqual(days, 8, f"full cycle took {days:.1f} days")

    def test_a_quiet_query_is_probed_as_often_as_any_other(self) -> None:
        """Otherwise "the market moved" and "my query broke" look the same.

        Nothing in the rotation reads yield any more, so this is a guard against
        reintroducing a productivity term that silently strands a quiet query.
        """
        visits, cells = self._simulate(runs=200)
        by_id = {c.id: c for c in cells}
        quiet = [v for cid, v in visits.items() if by_id[cid].query == "Developer Relations"]
        self.assertTrue(quiet)
        self.assertTrue(all(v >= 2 for v in quiet), quiet)


class SaturationTests(unittest.TestCase):
    def test_saturation_detected_at_the_threshold(self) -> None:
        self.assertTrue(is_saturated(75, 75))
        self.assertTrue(is_saturated(72, 75))  # within 95%
        self.assertFalse(is_saturated(37, 75))
        self.assertFalse(is_saturated(0, 75))

    def test_zero_requested_is_not_saturated(self) -> None:
        self.assertFalse(is_saturated(0, 0))


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

    def test_proxy_transport_is_recognised_before_transient(self) -> None:
        class ProxyError(Exception):
            pass

        for exc in [
            Exception("407 Proxy Authentication Required"),
            ProxyError("('Unable to connect to proxy', ConnectTimeoutError('timed out'))"),
            Exception("Tunnel connection failed: 407"),
            Exception("bad proxy"),
        ]:
            self.assertEqual(classify_error(exc), ERROR_PROXY, str(exc))

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
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes = 0

    def get_proxy_states(self, source: str | None = None) -> list[dict[str, Any]]:
        return [dict(row) for (_, src), row in self.rows.items() if source is None or src == source]

    def set_proxy_failure(
        self,
        pid: str,
        sources: Any,
        *,
        status: str,
        backoff_until: str | None,
        consecutive_failures: int,
        last_error: str,
    ) -> None:
        self.writes += 1
        for source in sources:
            self.rows[(pid, source)] = {
                "proxy_id": pid,
                "source": source,
                "status": status,
                "backoff_until": backoff_until,
                "consecutive_failures": consecutive_failures,
                "last_error": last_error,
            }

    def clear_proxy_state(
        self,
        *,
        proxy_id: str | None = None,
        source: str | None = None,
        broken_only: bool = False,
    ) -> int:
        self.writes += 1
        removed = 0
        for key in list(self.rows):
            pid, src = key
            if proxy_id is not None and pid != proxy_id:
                continue
            if source is not None and src != source:
                continue
            if broken_only and self.rows[key]["status"] != "broken":
                continue
            del self.rows[key]
            removed += 1
        return removed


class CircuitBreakerTests(unittest.TestCase):
    def _circuit(
        self, db: Any = None, proxies: list[str] | None = None, source: str = "linkedin"
    ) -> SourceCircuit:
        return SourceCircuit(source, CONFIG, db=db, now=lambda: NOW, proxies=proxies)

    def test_429_on_single_endpoint_backs_off_and_opens_circuit(self) -> None:
        db = FakeDb()
        circuit = self._circuit(db)
        error_class = circuit.on_error(Exception("HTTP 429 Too Many Requests"))
        self.assertEqual(error_class, ERROR_RATE_LIMIT)
        self.assertTrue(circuit.is_open)
        self.assertIn(("direct", "linkedin"), db.rows)
        self.assertEqual(
            db.rows[("direct", "linkedin")]["backoff_until"],
            (NOW + timedelta(minutes=60)).isoformat(),
        )

    def test_429_on_one_proxy_isolates_that_proxy_and_source(self) -> None:
        db = Database(":memory:")
        try:
            proxies = ["u:p@10.0.0.1:8000", "u:p@10.0.0.2:8000"]
            circuit = self._circuit(db, proxies=proxies, source="linkedin")
            circuit.on_request_error(Exception("HTTP 429 Too Many Requests"), proxies[0])
            self.assertFalse(circuit.is_open)
            self.assertFalse(circuit.is_usable(proxies[0]))
            self.assertTrue(circuit.is_usable(proxies[1]))

            indeed = self._circuit(db, proxies=proxies, source="indeed")
            self.assertTrue(indeed.is_usable(proxies[0]))
        finally:
            db.close()

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

    def test_multi_proxy_transient_gives_one_free_strike_then_backs_off(self) -> None:
        db = FakeDb()
        proxies = ["10.0.0.1:8000", "10.0.0.2:8000"]
        circuit = self._circuit(db, proxies=proxies)
        circuit.on_request_error(Exception("read timeout"), proxies[0])
        self.assertTrue(circuit.is_usable(proxies[0]))
        self.assertEqual(db.writes, 0)

        circuit.on_request_error(Exception("read timeout"), proxies[0])
        self.assertFalse(circuit.is_usable(proxies[0]))
        self.assertEqual(
            db.rows[(proxy_id(proxies[0]), "linkedin")]["backoff_until"],
            (NOW + timedelta(minutes=15)).isoformat(),
        )

    def test_success_clears_proxy_state_and_healthy_requests_do_not_write(self) -> None:
        db = FakeDb()
        circuit = self._circuit(db)
        circuit.on_request_success(None)
        self.assertEqual(db.writes, 0)

        circuit.on_error(Exception("timeout"))
        circuit.on_error(Exception("timeout"))
        circuit.on_success(10)
        circuit.on_error(Exception("timeout"))
        self.assertFalse(circuit.is_open)

        proxies = ["10.0.0.1:8000", "10.0.0.2:8000"]
        proxied = self._circuit(db, proxies=proxies)
        proxied.on_request_error(Exception("429"), proxies[0])
        self.assertIn((proxy_id(proxies[0]), "linkedin"), db.rows)
        proxied.on_request_success(proxies[0])
        self.assertNotIn((proxy_id(proxies[0]), "linkedin"), db.rows)

    def test_empty_result_and_content_value_error_do_not_penalise_proxy(self) -> None:
        circuit = self._circuit(FakeDb())
        for _ in range(5):
            circuit.on_empty()
            circuit.on_error(ValueError("LinkedIn detail response has no description"))
        self.assertFalse(circuit.is_open)
        self.assertTrue(circuit.is_usable(None))

    def test_rate_limit_backoff_escalates_60_240_1440_and_never_marks_broken(self) -> None:
        db = FakeDb()
        expected = [60, 240, 1440, 1440]
        for minutes in expected:
            circuit = self._circuit(db)
            circuit.on_error(Exception("429"))
            row = db.rows[("direct", "linkedin")]
            self.assertEqual(row["status"], "backoff")
            self.assertEqual(row["backoff_until"], (NOW + timedelta(minutes=minutes)).isoformat())

    def test_proxy_transport_failure_applies_across_sources_and_marks_broken_at_three(self) -> None:
        db = Database(":memory:")
        try:
            proxy = "u:p@10.0.0.9:8000"
            others = ["u:p@10.0.0.10:8000"]
            pid = proxy_id(proxy)

            r1 = SourceCircuit("linkedin", CONFIG, db=db, now=lambda: NOW, proxies=[proxy, *others])
            r1.on_request_error(Exception("407 Proxy Authentication Required"), proxy)
            states = {r["source"]: r for r in db.get_proxy_states()}
            self.assertEqual(set(states), {"indeed", "linkedin"})
            self.assertEqual(states["indeed"]["status"], "backoff")
            self.assertEqual(
                states["indeed"]["backoff_until"], (NOW + timedelta(minutes=60)).isoformat()
            )

            t2 = NOW + timedelta(minutes=61)
            r2 = SourceCircuit("indeed", CONFIG, db=db, now=lambda: t2, proxies=[proxy, *others])
            self.assertTrue(r2.is_usable(proxy))
            r2.on_request_error(Exception("Unable to connect to proxy"), proxy)
            states = {r["source"]: r for r in db.get_proxy_states()}
            self.assertEqual(states["linkedin"]["consecutive_failures"], 2)
            self.assertEqual(
                states["linkedin"]["backoff_until"], (t2 + timedelta(minutes=300)).isoformat()
            )

            t3 = t2 + timedelta(minutes=301)
            r3 = SourceCircuit("linkedin", CONFIG, db=db, now=lambda: t3, proxies=[proxy, *others])
            r3.on_request_error(Exception("407 Proxy Authentication Required"), proxy)
            for source in ("indeed", "linkedin"):
                later = SourceCircuit(
                    source, CONFIG, db=db, now=lambda: t3 + timedelta(days=30), proxies=[proxy]
                )
                self.assertFalse(later.is_usable(proxy))
                self.assertEqual(later.proxy_states[pid]["status"], "broken")
        finally:
            db.close()

    def test_direct_endpoint_never_becomes_permanently_broken(self) -> None:
        db = FakeDb()
        for _ in range(4):
            circuit = self._circuit(db)
            circuit.on_error(Exception("Unable to connect to proxy"))
        self.assertEqual(db.rows[("direct", "linkedin")]["status"], "backoff")

    def test_systemic_tripwire_stops_run_after_eight_distinct_endpoints_fail(self) -> None:
        db = FakeDb()
        proxies = [f"10.0.0.{i}:8000" for i in range(1, 21)]
        circuit = self._circuit(db, proxies=proxies)
        for i in range(7):
            circuit.on_request_error(Exception("429 Too Many Requests"), proxies[i])
            self.assertFalse(circuit.is_open)
        circuit.on_request_error(Exception("429 Too Many Requests"), proxies[7])
        self.assertTrue(circuit.is_open)
        self.assertEqual(len(db.rows), 8)

    def test_persisted_backoff_blocks_only_when_all_endpoints_unavailable(self) -> None:
        db = FakeDb()
        proxies = ["10.0.0.1:8000", "10.0.0.2:8000"]
        db.set_proxy_failure(
            proxy_id(proxies[0]),
            ("linkedin",),
            status="backoff",
            backoff_until=(NOW + timedelta(hours=1)).isoformat(),
            consecutive_failures=1,
            last_error="429",
        )
        self.assertFalse(self._circuit(db, proxies=proxies).persisted_backoff_active())

        db.set_proxy_failure(
            proxy_id(proxies[1]),
            ("linkedin",),
            status="backoff",
            backoff_until=(NOW + timedelta(minutes=30)).isoformat(),
            consecutive_failures=1,
            last_error="429",
        )
        blocked = self._circuit(db, proxies=proxies)
        self.assertTrue(blocked.persisted_backoff_active())
        self.assertEqual(blocked.backoff_until(), (NOW + timedelta(minutes=30)).isoformat())

    def test_expired_persisted_backoff_does_not_block(self) -> None:
        db = FakeDb()
        db.set_proxy_failure(
            "direct",
            ("linkedin",),
            status="backoff",
            backoff_until=(NOW - timedelta(hours=1)).isoformat(),
            consecutive_failures=1,
            last_error="429",
        )
        self.assertFalse(self._circuit(db).persisted_backoff_active())

    def test_cell_backoff_escalates(self) -> None:
        circuit = self._circuit(FakeDb())
        first = circuit.cell_backoff(1)
        third = circuit.cell_backoff(3)
        self.assertLess(first, third)

    def test_summary_reports_state_and_pool_counts(self) -> None:
        circuit = self._circuit(FakeDb())
        circuit.on_error(Exception("429 rate limited"))
        summary = circuit.summary()
        self.assertTrue(summary["tripped"])
        self.assertEqual(summary["source"], "linkedin")
        self.assertEqual(summary["rate_limit_hits"], 1)
        self.assertEqual(
            summary["pool"],
            {"configured": 1, "usable": 0, "backoff": 1, "broken": 0},
        )


if __name__ == "__main__":
    unittest.main()
