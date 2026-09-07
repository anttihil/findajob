"""Per-cell scrape cost: measurement, storage, and view calculations.

The failure this guards against is a cost number that looks measured and is not. An
unmeasured cell must stay NULL rather than become a zero, a retried cell must carry what
every attempt spent, and the view must derive accurate ratios.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database
from careerradar.search.runner import _add_cost
from careerradar.search.sources.base import BaseJobSource
from careerradar.search.sources.jobspy_source import count_requests

TASK: dict[str, Any] = {
    "cell_id": 1,
    "source": "linkedin",
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
            "INSERT INTO scrape_cells (id, source, location_id, query, search_label, "
            "country, created_at) VALUES (1, 'linkedin', 'helsinki', 'Platform Engineer', "
            "'Helsinki, Finland', 'FI', '2026-08-15')"
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


if __name__ == "__main__":
    unittest.main()
