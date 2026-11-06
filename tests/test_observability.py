"""Tests for model observability stats and verdict inspection queries."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from findajob.core.database import Database
from findajob.core.migrations import migrate
from findajob.web.app import app


def _seed_sample_data(db: Database) -> None:
    samples = [
        (
            1,
            "Staff Platform Engineer",
            "Datadog",
            "first",
            "Strong match on distributed systems and Kubernetes infrastructure experience.",
            5200,
            4100,
            115,
            0.00062,
            "2026-08-21T10:15:00",
        ),
        (
            2,
            "Senior Backend Engineer",
            "Stripe",
            "consider",
            "Payments background and Python systems match the core criteria.",
            5400,
            4200,
            130,
            0.00068,
            "2026-08-21T10:05:00",
        ),
        (
            3,
            "Junior Frontend Dev",
            "Startup",
            "low",
            "Candidate seniority exceeds junior role requirements by several years.",
            5100,
            4000,
            260,
            0.00095,
            "2026-08-21T10:10:00",
        ),
    ]
    for job_id, title, company, priority, reason, tin, cached, tout, cost, date in samples:
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, company, url, source, pipeline_state) "
            "VALUES (?, ?, ?, ?, ?, 'indeed', 'scored')",
            (job_id, f"k{job_id}", title, company, f"https://example.com/{job_id}"),
        )
        db.conn.execute(
            "INSERT INTO job_verdicts "
            "(job_id, profile_version, model, priority, reason_description, "
            "tokens_in, tokens_cached, tokens_out, cost_usd, created_at, "
            "verdict_schema_version) VALUES (?, 1, 'deepseek-v4-flash', ?, ?, ?, ?, ?, ?, ?, 4)",
            (job_id, priority, reason, tin, cached, tout, cost, date),
        )
    # A newer, expensive historical verdict must not affect any priority view.
    db.conn.execute(
        "INSERT INTO jobs (id, job_key, title, company, source, pipeline_state) "
        "VALUES (4, 'legacy', 'Historical role', 'Legacy company', 'indeed', 'scored')"
    )
    db.conn.execute(
        "INSERT INTO job_verdicts "
        "(job_id, profile_version, model, fit, reason_type, reason_description, "
        "tokens_in, tokens_cached, tokens_out, cost_usd, created_at, verdict_schema_version) "
        "VALUES (4, 1, 'legacy-model', 1, 'skills', 'Legacy Kubernetes fit', "
        "10000, 0, 9000, 10, '2099-01-01T00:00:00', 3)"
    )
    db.conn.commit()


class ObservabilityDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        migrate(self.db.conn)

    def tearDown(self) -> None:
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_get_observability_stats_empty(self) -> None:
        stats = self.db.get_observability_stats()
        self.assertEqual(stats["total_verdicts"], 0)
        self.assertEqual(stats["total_tokens_out"], 0)
        self.assertEqual(stats["avg_tokens_out"], 0.0)

    def test_get_observability_stats_populated(self) -> None:
        _seed_sample_data(self.db)
        stats = self.db.get_observability_stats()
        self.assertEqual(stats["total_verdicts"], 3)
        self.assertEqual(stats["total_tokens_out"], 115 + 130 + 260)
        self.assertEqual(stats["min_tokens_out"], 115)
        self.assertEqual(stats["max_tokens_out"], 260)
        self.assertAlmostEqual(stats["avg_tokens_out"], (115 + 130 + 260) / 3.0, places=1)
        self.assertGreater(stats["cache_hit_rate"], 0.7)
        self.assertEqual(
            stats["priorities"],
            [
                {"priority": priority, "count": 1, "percentage": 0.3333}
                for priority in ("first", "consider", "low")
            ],
        )
        self.assertEqual(stats["total_tokens_in"], 15700)
        self.assertEqual(stats["total_tokens_cached"], 12300)
        self.assertEqual(stats["total_cost_usd"], 0.0023)
        self.assertNotIn("fit_count", stats)
        self.assertNotIn("reasons", stats)
        self.assertEqual(len(stats["models"]), 1)

    def test_query_observability_verdicts_all(self) -> None:
        _seed_sample_data(self.db)
        res = self.db.query_observability_verdicts(sort="tokens_out_desc")
        self.assertEqual(res["total"], 3)
        self.assertEqual(len(res["items"]), 3)
        # First item is outlier / highest tokens_out
        self.assertEqual(res["items"][0]["tokens_out"], 260)
        self.assertEqual(res["items"][0]["title"], "Junior Frontend Dev")
        self.assertEqual(res["items"][0]["priority"], "low")
        for item in res["items"]:
            self.assertNotIn("fit", item)
            self.assertNotIn("reason_type", item)
            self.assertTrue(item["reason_description"])

    def test_query_observability_verdicts_filtered_by_priority(self) -> None:
        _seed_sample_data(self.db)
        for priority in ("first", "consider", "low"):
            with self.subTest(priority=priority):
                result = self.db.query_observability_verdicts(priority=priority)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["priority"], priority)
        result = self.db.query_observability_verdicts(priority="low", q="Kubernetes")
        self.assertEqual(result["total"], 0)
        self.assertEqual(self.db.query_observability_verdicts(model="legacy-model")["total"], 0)

    def test_sorting_and_pagination(self) -> None:
        _seed_sample_data(self.db)
        self.assertEqual(self.db.query_observability_verdicts()["items"][0]["job_id"], 1)
        for sort, expected in (
            ("date_desc", [1, 3, 2]),
            ("date_asc", [2, 3, 1]),
            ("tokens_out_desc", [3, 2, 1]),
            ("tokens_out_asc", [1, 2, 3]),
            ("cost_desc", [3, 2, 1]),
        ):
            with self.subTest(sort=sort):
                result = self.db.query_observability_verdicts(sort=sort, limit=1, offset=1)
                self.assertEqual(result["total"], 3)
                self.assertEqual([item["job_id"] for item in result["items"]], expected[1:2])
                self.assertEqual(result["limit"], 1)
                self.assertEqual(result["offset"], 1)
        self.db.conn.execute("UPDATE job_verdicts SET created_at='2026-08-21T10:00:00'")
        self.assertEqual(
            [item["job_id"] for item in self.db.query_observability_verdicts()["items"]],
            [3, 2, 1],
        )

    def test_only_historical_verdicts(self) -> None:
        _seed_sample_data(self.db)
        self.db.conn.execute("DELETE FROM job_verdicts WHERE priority IS NOT NULL")
        stats = self.db.get_observability_stats()
        self.assertEqual(stats["total_verdicts"], 0)
        self.assertEqual(stats["total_cost_usd"], 0)
        self.assertEqual(stats["models"], [])
        self.assertTrue(all(item["count"] == 0 for item in stats["priorities"]))
        self.assertTrue(all(item["percentage"] == 0 for item in stats["priorities"]))
        self.assertEqual(self.db.query_observability_verdicts()["items"], [])

    def test_missing_reason_and_usage(self) -> None:
        _seed_sample_data(self.db)
        self.db.conn.execute(
            "UPDATE job_verdicts SET reason_description=NULL, tokens_in=NULL, "
            "tokens_cached=NULL, tokens_out=NULL, cost_usd=NULL WHERE priority='first'"
        )
        item = self.db.query_observability_verdicts(priority="first")["items"][0]
        self.assertEqual(item["reason_description"], "")
        for key in ("tokens_in", "tokens_cached", "tokens_out", "cost_usd"):
            self.assertEqual(item[key], 0)

    def test_query_observability_verdicts_search_text(self) -> None:
        _seed_sample_data(self.db)
        res = self.db.query_observability_verdicts(q="Kubernetes")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["items"][0]["company"], "Datadog")

        res2 = self.db.query_observability_verdicts(q="Stripe")
        self.assertEqual(res2["total"], 1)
        self.assertEqual(res2["items"][0]["title"], "Senior Backend Engineer")


class ObservabilityApiEndpointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        migrate(self.db.conn)

        from findajob.web.app import _PooledDatabase, _thread_state

        def _get_test_db() -> _PooledDatabase:
            db = getattr(_thread_state, "db", None)
            if db is None or db.db_path != self.tmp.name:
                db = _PooledDatabase(self.tmp.name)
                _thread_state.db = db
            return db

        self.patcher = patch("findajob.web.app.get_db", side_effect=_get_test_db)
        self.patcher.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.patcher.stop()
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_observability_stats_endpoint_empty(self) -> None:
        response = self.client.get("/api/observability/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_verdicts"], 0)
        self.assertEqual(data["total_tokens_out"], 0)
        self.assertEqual(data["cache_hit_rate"], 0.0)
        self.assertEqual(
            data["priorities"],
            [
                {"priority": priority, "count": 0, "percentage": 0.0}
                for priority in ("first", "consider", "low")
            ],
        )

    def test_observability_stats_endpoint_populated(self) -> None:
        _seed_sample_data(self.db)
        response = self.client.get("/api/observability/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_verdicts"], 3)
        self.assertEqual(data["total_tokens_out"], 115 + 130 + 260)
        self.assertGreater(data["cache_hit_rate"], 0.7)
        self.assertEqual([item["count"] for item in data["priorities"]], [1, 1, 1])
        self.assertEqual(len(data["models"]), 1)

    def test_observability_verdicts_endpoint_all(self) -> None:
        _seed_sample_data(self.db)
        response = self.client.get("/api/observability/verdicts?limit=10&offset=0")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(data["limit"], 10)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["items"][0]["title"], "Staff Platform Engineer")

    def test_observability_verdicts_endpoint_filtered_and_searched(self) -> None:
        _seed_sample_data(self.db)
        for priority in ("first", "consider", "low"):
            response = self.client.get(f"/api/observability/verdicts?priority={priority}")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["priority"], priority)
            self.assertNotIn("fit", data["items"][0])
            self.assertNotIn("reason_type", data["items"][0])

        response_search = self.client.get("/api/observability/verdicts?q=Kubernetes")
        self.assertEqual(response_search.status_code, 200)
        data_search = response_search.json()
        self.assertEqual(data_search["total"], 1)
        self.assertEqual(data_search["items"][0]["company"], "Datadog")

    def test_observability_validation(self) -> None:
        for query in ("priority=fit", "priority=unknown", "sort=unknown", "limit=0", "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(f"/api/observability/verdicts?{query}").status_code, 422
                )

    def test_observability_prompt_endpoint(self) -> None:
        response = self.client.get("/api/observability/prompt")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("prompt_hash", data)
        self.assertIn("rules", data)
        self.assertIn("summary_text", data)
        self.assertIn("system_prompt", data)
        self.assertEqual(len(data["prompt_hash"]), 16)


if __name__ == "__main__":
    unittest.main()
