"""Tests for 'date_posted' time window filtering in Database.query_jobs,
Database.job_ids_for, and FastAPI web endpoints.
"""

import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database
from careerradar.web.app import app


def ago(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class DatePostedDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self) -> None:
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def job(
        self,
        job_id: int,
        date_posted: str | None,
        date_found: str | None = None,
        status: str = "unread",
    ) -> None:
        if date_found is None:
            date_found = ago(days=60)
        self.db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, status, "
            "date_posted, date_found, sync_run_id) "
            "VALUES (?, ?, 'Software Engineer', ?, 'desc', ?, ?, ?, 1)",
            (
                job_id,
                f"key_{job_id}",
                f"https://example.test/{job_id}",
                status,
                date_posted,
                date_found,
            ),
        )
        self.db.conn.commit()

    def test_filter_windows(self) -> None:
        self.job(1, ago(hours=10))  # < 24h
        self.job(2, ago(days=2))  # < 3d
        self.job(3, ago(days=5))  # < 7d
        self.job(4, ago(days=10))  # < 14d
        self.job(5, ago(days=20))  # < 30d
        self.job(6, ago(days=45))  # > 30d

        # 24h
        res_24h = self.db.query_jobs(date_posted="24h")
        self.assertEqual([j["id"] for j in res_24h["jobs"]], [1])

        # 3d
        res_3d = self.db.query_jobs(date_posted="3d")
        self.assertEqual([j["id"] for j in res_3d["jobs"]], [1, 2])

        # 7d
        res_7d = self.db.query_jobs(date_posted="7d")
        self.assertEqual([j["id"] for j in res_7d["jobs"]], [1, 2, 3])

        # 14d
        res_14d = self.db.query_jobs(date_posted="14d")
        self.assertEqual([j["id"] for j in res_14d["jobs"]], [1, 2, 3, 4])

        # 30d
        res_30d = self.db.query_jobs(date_posted="30d")
        self.assertEqual([j["id"] for j in res_30d["jobs"]], [1, 2, 3, 4, 5])

        # All (empty / None)
        res_none = self.db.query_jobs(date_posted=None)
        self.assertEqual(len(res_none["jobs"]), 6)

        res_empty = self.db.query_jobs(date_posted="")
        self.assertEqual(len(res_empty["jobs"]), 6)

    def test_filter_fallback_to_date_found_when_date_posted_is_null(self) -> None:
        # job 1: no date_posted, but found 5 hours ago
        self.job(1, date_posted=None, date_found=ago(hours=5))
        # job 2: no date_posted, found 10 days ago
        self.job(2, date_posted=None, date_found=ago(days=10))

        res_24h = self.db.query_jobs(date_posted="24h")
        self.assertEqual([j["id"] for j in res_24h["jobs"]], [1])

        res_14d = self.db.query_jobs(date_posted="14d")
        self.assertEqual([j["id"] for j in res_14d["jobs"]], [1, 2])

    def test_job_ids_for_matches_query_jobs_with_date_posted(self) -> None:
        self.job(1, ago(hours=5))
        self.job(2, ago(days=2))
        self.job(3, ago(days=20))

        ids_24h = self.db.job_ids_for(date_posted="24h")
        q_24h = [j["id"] for j in self.db.query_jobs(date_posted="24h")["jobs"]]
        self.assertEqual(ids_24h, q_24h)
        self.assertEqual(ids_24h, [1])

        ids_3d = self.db.job_ids_for(date_posted="3d")
        q_3d = [j["id"] for j in self.db.query_jobs(date_posted="3d")["jobs"]]
        self.assertEqual(ids_3d, q_3d)
        self.assertEqual(ids_3d, [1, 2])

    def test_filter_date_normalized_to_noon(self) -> None:
        today_noon = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00Z")
        yesterday_noon = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%dT12:00:00Z"
        )
        five_days_ago_noon = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT12:00:00Z"
        )

        self.job(1, date_posted=today_noon)
        self.job(2, date_posted=yesterday_noon)
        self.job(3, date_posted=five_days_ago_noon)

        res_24h = self.db.query_jobs(date_posted="24h")
        self.assertIn(1, [j["id"] for j in res_24h["jobs"]])

        res_7d = self.db.query_jobs(date_posted="7d")
        self.assertEqual([j["id"] for j in res_7d["jobs"]], [1, 2, 3])

    def test_date_posted_parameter_signature_parity(self) -> None:
        query_sig = inspect.signature(self.db.query_jobs)
        ids_sig = inspect.signature(self.db.job_ids_for)
        self.assertIn("date_posted", query_sig.parameters)
        self.assertIn("date_posted", ids_sig.parameters)


class DatePostedEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

        def _get_test_db() -> Database:
            return Database(self.tmp.name)

        self.patcher = patch("careerradar.web.app.get_db", side_effect=_get_test_db)
        self.patcher.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.patcher.stop()
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def job(self, job_id: int, date_posted: str | None, date_found: str | None = None) -> None:
        if date_found is None:
            date_found = ago(days=60)
        self.db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, status, "
            "date_posted, date_found, sync_run_id) "
            "VALUES (?, ?, 'Software Engineer', ?, 'desc', 'unread', ?, ?, 1)",
            (
                job_id,
                f"key_{job_id}",
                f"https://example.test/{job_id}",
                date_posted,
                date_found,
            ),
        )
        self.db.conn.commit()

    def test_api_jobs_date_posted(self) -> None:
        self.job(1, ago(hours=6))
        self.job(2, ago(days=4))

        resp_24h = self.client.get("/api/jobs?date_posted=24h")
        self.assertEqual(resp_24h.status_code, 200)
        data_24h = resp_24h.json()
        self.assertEqual([j["id"] for j in data_24h["jobs"]], [1])

        resp_7d = self.client.get("/api/jobs?date_posted=7d")
        self.assertEqual(resp_7d.status_code, 200)
        data_7d = resp_7d.json()
        self.assertEqual([j["id"] for j in data_7d["jobs"]], [1, 2])

    def test_api_jobs_invalid_date_posted_fails_validation(self) -> None:
        resp = self.client.get("/api/jobs?date_posted=invalid")
        self.assertEqual(resp.status_code, 422)

    def test_api_job_context_next_id_respects_date_posted(self) -> None:
        self.job(1, ago(hours=2))
        self.job(2, ago(hours=4))
        self.job(3, ago(days=5))

        # With date_posted=24h, job 1's next job is job 2
        resp = self.client.get("/api/jobs/1/context?date_posted=24h")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["next_job_id"], 2)

        # Job 2 is the last in the 24h window, so next is None
        resp2 = self.client.get("/api/jobs/2/context?date_posted=24h")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertIsNone(data2["next_job_id"])

        # Without date filter, job 2's next is job 3
        resp3 = self.client.get("/api/jobs/2/context")
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertEqual(data3["next_job_id"], 3)


if __name__ == "__main__":
    unittest.main()
