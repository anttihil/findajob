"""Dropping `jobs.fit_score`: one score, and it belongs to a profile version.

v5 created `job_verdicts` -- keyed on `(job_id, profile_version)` -- and added a
`fit_score` column to `jobs` in the same migration, so a caller could rank by score
without the join. The copy carried no version, and the readers of it joined the verdict
table on the ACTIVE profile, so the two disagreed for the whole window between a
`profile build` and the re-score that followed it.

What is pinned here is that the migration removes the column WITHOUT touching the record:
the verdicts, and the scores in them, have to survive intact. A migration that dropped the
column by rebuilding the table is exactly the kind that would take the verdict rows with
it.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.migrations import MIGRATIONS, apply_pragmas, current_version, migrate


class DropDenormalizedFitScoreTests(unittest.TestCase):
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

    def columns(self) -> set[str]:
        return {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}

    def seed_v12(self) -> None:
        """A v12 database holding one scored posting, the copy in step with the record."""
        self.migrate_to(12)
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, company, url, fit_score, pipeline_state) "
            "VALUES (1, 'k1', 'Platform Engineer', 'Acme', 'http://x/1', 87, 'scored')"
        )
        self.conn.execute(
            "INSERT INTO profiles (version, created_at, is_active, profile_json, summary_text) "
            "VALUES (4, '2026-08-12T00:00:00Z', 1, '{}', '')"
        )
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit_score, verdict, "
            "created_at, pareto_tier, eligibility) "
            "VALUES (1, 4, 'm', 87, 'strong', '2026-08-12T00:00:00Z', 2, 'eligible')"
        )
        self.conn.commit()

    def test_the_column_is_gone(self) -> None:
        self.seed_v12()
        self.assertIn("fit_score", self.columns())
        migrate(self.conn)
        self.assertNotIn("fit_score", self.columns())

    def test_the_verdict_record_survives_untouched(self) -> None:
        self.seed_v12()
        self.migrate_to(13)
        row = self.conn.execute("SELECT * FROM job_verdicts WHERE job_id = 1").fetchone()
        self.assertEqual(row["fit_score"], 87)
        self.assertEqual(row["verdict"], "strong")
        self.assertEqual(row["profile_version"], 4)
        self.assertEqual(row["pareto_tier"], 2)
        # The posting itself is not disturbed either -- only one of its columns goes.
        job = self.conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
        self.assertEqual(job["title"], "Platform Engineer")
        self.assertEqual(job["pipeline_state"], "scored")

    def test_the_backlog_index_is_rebuilt_without_the_column(self) -> None:
        """SQLite refuses to drop an indexed column, so the index has to go first."""
        self.seed_v12()
        migrate(self.conn)
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_jobs_pipeline_state'"
        ).fetchone()["sql"]
        self.assertNotIn("fit_score", sql)
        self.assertIn("pipeline_state", sql)

    def test_a_fresh_database_never_grows_the_column(self) -> None:
        migrate(self.conn)
        self.assertNotIn("fit_score", self.columns())
        self.assertEqual(current_version(self.conn), MIGRATIONS[-1][0])

    def test_running_twice_is_a_no_op(self) -> None:
        self.seed_v12()
        migrate(self.conn)
        self.assertEqual(migrate(self.conn), 0)
        self.assertNotIn("fit_score", self.columns())


if __name__ == "__main__":
    unittest.main()
