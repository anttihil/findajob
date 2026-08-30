"""Tests for model observability stats and verdict inspection queries."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from careerradar.core.database import Database
from careerradar.core.migrations import migrate
from careerradar.web.app import app


def _seed_sample_data(db: Database) -> None:
    cur = db.conn.cursor()
    # Seed jobs
    cur.execute(
        """
        INSERT INTO jobs (
            id, job_key, title, company, location, url, source, pipeline_state, description
        ) VALUES (
            1, 'k1', 'Staff Platform Engineer', 'Datadog', 'San Francisco, CA',
            'https://example.com/1', 'indeed', 'scored', 'Some long description about platforms'
        )
        """
    )
    cur.execute(
        """
        INSERT INTO jobs (
            id, job_key, title, company, location, url, source, pipeline_state, description
        ) VALUES (
            2, 'k2', 'Senior Backend Engineer', 'Stripe', 'Remote',
            'https://example.com/2', 'linkedin', 'scored',
            'Some description about payments and python'
        )
        """
    )
    cur.execute(
        """
        INSERT INTO jobs (
            id, job_key, title, company, location, url, source, pipeline_state, description
        ) VALUES (
            3, 'k3', 'Junior Frontend Dev', 'Startup', 'New York, NY',
            'https://example.com/3', 'indeed', 'scored',
            'React typescript html css frontend'
        )
        """
    )

    # Seed verdicts
    cur.execute(
        """
        INSERT INTO job_verdicts (
            job_id, profile_version, model, fit, reason_type, reason_description,
            tokens_in, tokens_cached, tokens_out, cost_usd, created_at,
            verdict_schema_version
        ) VALUES (
            1, 1, 'deepseek-v4-flash', 1, 'skills',
            'Strong match on distributed systems and Kubernetes infrastructure experience.',
            5200, 4100, 115, 0.00062, '2026-08-21T10:00:00', 3
        )
        """
    )
    cur.execute(
        """
        INSERT INTO job_verdicts (
            job_id, profile_version, model, fit, reason_type, reason_description,
            tokens_in, tokens_cached, tokens_out, cost_usd, created_at,
            verdict_schema_version
        ) VALUES (
            2, 1, 'deepseek-v4-flash', 1, 'domain',
            'Payments background and Python systems match the core criteria.',
            5400, 4200, 130, 0.00068, '2026-08-21T10:05:00', 3
        )
        """
    )
    cur.execute(
        """
        INSERT INTO job_verdicts (
            job_id, profile_version, model, fit, reason_type, reason_description,
            tokens_in, tokens_cached, tokens_out, cost_usd, created_at,
            verdict_schema_version
        ) VALUES (
            3, 1, 'deepseek-v4-flash', 0, 'seniority',
            'Candidate seniority exceeds junior role requirements by several years.',
            5100, 4000, 260, 0.00095, '2026-08-21T10:10:00', 3
        )
        """
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
        self.assertEqual(stats["fit_count"], 2)
        self.assertEqual(stats["no_fit_count"], 1)
        self.assertGreater(stats["cache_hit_rate"], 0.7)
        self.assertEqual(len(stats["reasons"]), 3)
        self.assertEqual(len(stats["models"]), 1)

    def test_query_observability_verdicts_all(self) -> None:
        _seed_sample_data(self.db)
        res = self.db.query_observability_verdicts(sort="tokens_out_desc")
        self.assertEqual(res["total"], 3)
        self.assertEqual(len(res["items"]), 3)
        # First item is outlier / highest tokens_out
        self.assertEqual(res["items"][0]["tokens_out"], 260)
        self.assertEqual(res["items"][0]["title"], "Junior Frontend Dev")
        self.assertEqual(res["items"][0]["fit"], False)
        self.assertEqual(res["items"][0]["reason_type"], "seniority")

    def test_query_observability_verdicts_filtered_by_fit(self) -> None:
        _seed_sample_data(self.db)
        res_fit = self.db.query_observability_verdicts(fit="fit")
        self.assertEqual(res_fit["total"], 2)
        for it in res_fit["items"]:
            self.assertTrue(it["fit"])

        res_nofit = self.db.query_observability_verdicts(fit="no_fit")
        self.assertEqual(res_nofit["total"], 1)
        self.assertFalse(res_nofit["items"][0]["fit"])

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

        from careerradar.web.app import _PooledDatabase, _thread_state

        def _get_test_db() -> _PooledDatabase:
            db = getattr(_thread_state, "db", None)
            if db is None or db.db_path != self.tmp.name:
                db = _PooledDatabase(self.tmp.name)
                _thread_state.db = db
            return db

        self.patcher = patch("careerradar.web.app.get_db", side_effect=_get_test_db)
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
        self.assertEqual(data["reasons"], [])

    def test_observability_stats_endpoint_populated(self) -> None:
        _seed_sample_data(self.db)
        response = self.client.get("/api/observability/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_verdicts"], 3)
        self.assertEqual(data["total_tokens_out"], 115 + 130 + 260)
        self.assertEqual(data["fit_count"], 2)
        self.assertEqual(data["no_fit_count"], 1)
        self.assertGreater(data["cache_hit_rate"], 0.7)
        self.assertEqual(len(data["reasons"]), 3)
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
        self.assertEqual(data["items"][0]["title"], "Junior Frontend Dev")

    def test_observability_verdicts_endpoint_filtered_and_searched(self) -> None:
        _seed_sample_data(self.db)
        response_fit = self.client.get("/api/observability/verdicts?fit=fit")
        self.assertEqual(response_fit.status_code, 200)
        data_fit = response_fit.json()
        self.assertEqual(data_fit["total"], 2)
        for item in data_fit["items"]:
            self.assertTrue(item["fit"])

        response_search = self.client.get("/api/observability/verdicts?q=Kubernetes")
        self.assertEqual(response_search.status_code, 200)
        data_search = response_search.json()
        self.assertEqual(data_search["total"], 1)
        self.assertEqual(data_search["items"][0]["company"], "Datadog")

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
