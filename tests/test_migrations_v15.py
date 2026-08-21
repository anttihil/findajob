"""Tests for Migration v15: dropping legacy 5-ordinal job_verdict columns.

Verifies that legacy ordinal and blocker columns and idx_verdicts_score are removed,
while active simplified columns (fit, reason_type, reason_description) and their
metadata survive intact with proper indexes.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_pragmas,
    current_version,
    migrate,
)

LEGACY_COLUMNS = {
    "fit_score",
    "verdict",
    "seniority_fit",
    "hard_blockers",
    "key_gaps",
    "strengths",
    "reasoning",
    "research_worthy",
    "role_summary",
    "eligibility",
    "role_match",
    "capability_match",
    "seniority_gap",
    "evidence_quality",
    "core_requirements",
    "requirement_assessments",
    "audit_flags",
    "scale_version",
    "pareto_tier",
}

EXPECTED_COLUMNS = {
    "id",
    "job_id",
    "profile_version",
    "model",
    "fit",
    "reason_type",
    "reason_description",
    "verdict_schema_version",
    "prompt_hash",
    "tokens_in",
    "tokens_cached",
    "tokens_out",
    "cost_usd",
    "created_at",
}


class MigrationV15Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        apply_pragmas(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    def migrate_to(self, target: int) -> None:
        for version, _, fn in MIGRATIONS:
            if version > target:
                break
            fn(self.conn.cursor())
            self.conn.execute(f"PRAGMA user_version = {version}")
        self.conn.commit()

    def columns(self, table: str = "job_verdicts") -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def indexes(self, table: str = "job_verdicts") -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA index_list({table})")}

    def seed_v14(self) -> None:
        self.migrate_to(14)
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, company, url, pipeline_state, sync_run_id) "
            "VALUES (1, 'k1', 'Platform Engineer', 'Acme', 'http://x/1', 'scored', 1)"
        )
        self.conn.execute(
            "INSERT INTO profiles (version, created_at, is_active, profile_json, summary_text) "
            "VALUES (4, '2026-08-12T00:00:00Z', 1, '{}', '')"
        )
        self.conn.execute(
            """
            INSERT INTO job_verdicts (
                job_id, profile_version, model, fit_score, verdict,
                fit, reason_type, reason_description,
                verdict_schema_version, prompt_hash, tokens_in, tokens_cached, tokens_out,
                cost_usd, created_at, pareto_tier, eligibility
            ) VALUES (
                1, 4, 'deepseek-v4-flash', 85, 'strong',
                1, 'match', 'Strong infrastructure alignment.',
                3, 'hash123', 5000, 4000, 300,
                0.0012, '2026-08-21T00:00:00Z', 1, 'eligible'
            )
            """
        )
        self.conn.commit()

    def test_reaches_schema_version_15(self) -> None:
        self.seed_v14()
        self.assertEqual(current_version(self.conn), 14)
        migrate(self.conn)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 15)

    def test_legacy_columns_are_dropped(self) -> None:
        self.seed_v14()
        cols_before = self.columns()
        self.assertTrue(cols_before >= LEGACY_COLUMNS)

        migrate(self.conn)
        cols_after = self.columns()
        for col in LEGACY_COLUMNS:
            self.assertNotIn(col, cols_after)
        self.assertEqual(cols_after, EXPECTED_COLUMNS)

    def test_simplified_verdict_data_survives(self) -> None:
        self.seed_v14()
        migrate(self.conn)

        row = self.conn.execute("SELECT * FROM job_verdicts WHERE job_id = 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["job_id"], 1)
        self.assertEqual(row["profile_version"], 4)
        self.assertEqual(row["model"], "deepseek-v4-flash")
        self.assertEqual(row["fit"], 1)
        self.assertEqual(row["reason_type"], "match")
        self.assertEqual(row["reason_description"], "Strong infrastructure alignment.")
        self.assertEqual(row["verdict_schema_version"], 3)
        self.assertEqual(row["prompt_hash"], "hash123")
        self.assertEqual(row["tokens_in"], 5000)
        self.assertEqual(row["tokens_cached"], 4000)
        self.assertEqual(row["tokens_out"], 300)
        self.assertAlmostEqual(row["cost_usd"], 0.0012)
        self.assertEqual(row["created_at"], "2026-08-21T00:00:00Z")

    def test_indexes_are_updated(self) -> None:
        self.seed_v14()
        migrate(self.conn)

        idxs = self.indexes()
        self.assertNotIn("idx_verdicts_score", idxs)
        self.assertIn("idx_verdicts_fit", idxs)
        self.assertIn("idx_verdicts_reason", idxs)

    def test_idempotent(self) -> None:
        self.seed_v14()
        migrate(self.conn)
        self.assertEqual(migrate(self.conn), 0)

    def test_fresh_database(self) -> None:
        migrate(self.conn)
        self.assertEqual(self.columns(), EXPECTED_COLUMNS)
        self.assertEqual(current_version(self.conn), 15)


if __name__ == "__main__":
    unittest.main()
