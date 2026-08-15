"""Per-cell scrape cost: measurement, storage, and the report over it.

The failure this guards against is a cost number that looks measured and is not. An
unmeasured cell must stay NULL rather than become a zero, a retried cell must carry what
every attempt spent, and the report must not average a run of NULLs into "0 requests".
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database
from careerradar.search.cost import collect, render
from careerradar.search.runner import _add_cost
from careerradar.search.sources.base import BaseJobSource
from careerradar.search.sources.jobspy_source import count_requests

TASK: dict[str, Any] = {
    "cell_id": 1,
    "source": "linkedin",
    "role_family": "platform",
    "location_id": "helsinki",
    "query": "Platform Engineer",
    "hours_old": 24,
    "results_wanted": 50,
    "desc_selection": "census",
}


class _FakeClient(BaseJobSource):
    """Stands in for JobSpySource, which needs the library and a network."""

    def __init__(self) -> None:
        super().__init__()

    def fetch_for_task(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        return []


class RequestCountingTests(unittest.TestCase):
    def test_counts_requests_and_restores_the_session_methods(self) -> None:
        from jobspy.util import RequestsRotating, create_session
        from requests.exceptions import RequestException

        before = RequestsRotating.request
        session = create_session(is_tls=False)
        with count_requests() as counter:
            for _ in range(3):
                # Port 9 (discard) refuses immediately: the request is issued, which is
                # what is counted, and no board is touched.
                with self.assertRaises(RequestException):
                    session.get("http://127.0.0.1:9/none", timeout=0.2)
        self.assertEqual(counter.count, 3)
        self.assertIs(RequestsRotating.request, before)

    def test_counters_do_not_leak_between_scrapes(self) -> None:
        with count_requests() as first:
            pass
        with count_requests() as second:
            pass
        self.assertEqual((first.count, second.count), (0, 0))


class CostAccumulationTests(unittest.TestCase):
    def test_attempts_are_summed(self) -> None:
        client = _FakeClient()
        cost = {"duration_ms": 0, "requests_made": 0}

        client.last_fetch = {"duration_ms": 1200, "requests_made": 3}
        _add_cost(cost, client)
        client.last_fetch = {"duration_ms": 800, "requests_made": 55}
        _add_cost(cost, client)

        self.assertEqual(cost, {"duration_ms": 2000, "requests_made": 58})

    def test_an_attempt_that_never_reached_the_board_is_not_counted_twice(self) -> None:
        """`_add_cost` clears, so a retry that dies before `fetch_for_task` adds nothing."""
        client = _FakeClient()
        cost = {"duration_ms": 0, "requests_made": 0}

        client.last_fetch = {"duration_ms": 1200, "requests_made": 3}
        _add_cost(cost, client)
        _add_cost(cost, client)

        self.assertEqual(cost, {"duration_ms": 1200, "requests_made": 3})


class ObservationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)
        self.db.conn.execute(
            "INSERT INTO sync_runs (id, started_at, mode, status) VALUES (1, '2026-08-15', "
            "'incremental', 'ok')"
        )
        self.db.conn.execute(
            "INSERT INTO scrape_cells (id, source, role_family, location_id, query, tier, "
            "created_at) VALUES (1, 'linkedin', 'platform', 'helsinki', 'Platform Engineer', "
            "'core', '2026-08-15')"
        )
        self.db.conn.commit()

    def tearDown(self) -> None:
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def _row(self) -> sqlite3.Row:
        return self.db.conn.execute("SELECT * FROM cell_cost").fetchone()

    def test_cost_is_stored_and_the_view_derives_the_ratios(self) -> None:
        self.db.record_observation(
            1,
            TASK,
            "2026-08-15T04:00:00+00:00",
            returned=50,
            duration_ms=60000,
            requests_made=55,
        )
        row = self._row()
        self.assertEqual((row["duration_ms"], row["requests_made"]), (60000, 55))
        self.assertAlmostEqual(row["seconds"], 60.0)
        self.assertAlmostEqual(row["ms_per_request"], 60000 / 55)
        self.assertAlmostEqual(row["requests_per_posting"], 55 / 50)

    def test_an_unmeasured_cell_stays_null_rather_than_zero(self) -> None:
        self.db.record_observation(1, TASK, "2026-08-15T04:00:00+00:00", returned=50)
        row = self._row()
        self.assertIsNone(row["duration_ms"])
        self.assertIsNone(row["requests_made"])
        self.assertIsNone(row["ms_per_request"])

    def test_the_report_separates_unmeasured_cells_from_free_ones(self) -> None:
        self.db.record_observation(
            1, TASK, "2026-08-15T04:00:00+00:00", returned=50, duration_ms=60000, requests_made=55
        )
        self.db.record_observation(1, TASK, "2026-08-15T04:02:00+00:00", returned=50)

        report = collect(self.db, run_id=1)
        source = report["sources"][0]
        self.assertEqual((source["cells"], source["unmeasured"]), (2, 1))
        # Averaged over the cell that was actually measured, not over both.
        self.assertAlmostEqual(source["seconds_per_cell"], 60.0)
        self.assertEqual(len(report["slowest"]), 1)

        # The estimate comes from scheduler.estimate_units, so it is charged for every
        # planned cell -- including the one whose true cost is unknown. Both cells are a
        # census of 50 postings: 6 search pages plus 50 description fetches, twice.
        self.assertEqual(source["est_requests"], 112)
        text = render(report)
        self.assertIn("1 cell(s) marked ? were scraped before cost was measured", text)

    def test_a_cell_that_matches_the_estimate_raises_no_warning(self) -> None:
        """56 planned, 55 spent -- the corrected description-aware model (scheduler.py)."""
        self.db.record_observation(
            1, TASK, "2026-08-15T04:00:00+00:00", returned=50, duration_ms=60000, requests_made=55
        )
        self.assertNotIn("where the scheduler planned", render(collect(self.db, run_id=1)))

    def test_a_cell_that_outspends_the_estimate_is_flagged(self) -> None:
        """The check that caught the pages-only model: 5 planned, 56 spent."""
        self.db.record_observation(
            1, TASK, "2026-08-15T04:00:00+00:00", returned=50, duration_ms=60000, requests_made=300
        )
        text = render(collect(self.db, run_id=1))
        self.assertIn("where the scheduler planned", text)
        self.assertIn("request_units", text)

    def test_a_run_with_no_measurements_reports_no_cost(self) -> None:
        report = collect(self.db, run_id=None)
        self.assertIsNone(report["run_id"])
        self.assertIn("No measured scrape yet", render(report))


if __name__ == "__main__":
    unittest.main()
