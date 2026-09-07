"""Migration v5 against a real v4 database.

The v5 migration drops two columns and adds a queue column that the whole pipeline keys
on, so the things worth asserting are: the corpus survives, the queue starts full, and the
"one active profile" rule is enforced by the database rather than by convention.
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
    current_version,
    migrate,
)


class MigrationV5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    def migrate_to(self, version: int) -> None:
        """Apply migrations up to `version`, the way a real older database arrives."""
        from careerradar.core.migrations import apply_pragmas

        apply_pragmas(self.conn)
        cursor = self.conn.cursor()
        for target, _description, fn in MIGRATIONS:
            if target > version:
                break
            fn(cursor)
            cursor.execute(f"PRAGMA user_version = {int(target)}")
        self.conn.commit()

    def columns(self, table: str = "jobs") -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def tables(self) -> set[str]:
        return {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    # --- the migration itself ---------------------------------------------------------

    def test_a_v4_database_migrates_to_the_current_version(self) -> None:
        self.migrate_to(4)
        self.assertEqual(current_version(self.conn), 4)
        migrate(self.conn)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)

    def test_existing_postings_survive_and_queue_for_scoring(self) -> None:
        self.migrate_to(4)
        self.conn.execute(
            "INSERT INTO jobs (job_key, title, company, url, match_score) "
            "VALUES ('k1', 'Platform Engineer', 'Acme', 'http://x/1', 55)"
        )
        self.conn.commit()

        migrate(self.conn)

        row = self.conn.execute("SELECT * FROM jobs WHERE job_key = 'k1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Platform Engineer")
        # Keyword score is kept: gap_analysis still measures against it.
        self.assertEqual(row["match_score"], 55)
        # And the posting is queued, because no verdict exists for it yet. Asserted
        # against `job_verdicts` rather than the old `jobs.fit_score`, which v13 dropped:
        # the verdict row is what "scored" means now.
        self.assertEqual(row["pipeline_state"], "new")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_verdicts WHERE job_id = ?", (row["id"],)
            ).fetchone()[0],
            0,
        )

    def test_superseded_columns_are_dropped(self) -> None:
        self.migrate_to(4)
        self.assertIn("llm_verdict", self.columns())
        self.assertIn("resume_match", self.columns())
        migrate(self.conn)
        self.assertNotIn("llm_verdict", self.columns())
        self.assertNotIn("resume_match", self.columns())

    def test_the_analytics_columns_the_market_module_needs_are_kept(self) -> None:
        self.migrate_to(4)
        migrate(self.conn)
        for column in (
            "match_score",
            "matched_skills",
            "desc_selection",
            "description_quality",
            "duplicate_of",
            "sync_run_id",
        ):
            self.assertIn(column, self.columns())

    def test_the_scrape_history_is_untouched(self) -> None:
        """The coverage cycle is expensive to rebuild -- ~5 days of scraping."""
        self.migrate_to(5)
        for table in (
            "scrape_cells",
            "cell_observations",
            "sync_runs",
            "source_state",
            "job_skills",
            "job_blockers",
            "skill_market_stats",
            "role_market_stats",
        ):
            self.assertIn(table, self.tables())

    def test_the_new_tables_exist(self) -> None:
        self.migrate_to(5)
        for table in (
            "profiles",
            "profile_documents",
            "interview_turns",
            "job_verdicts",
            "company_dossiers",
            "research_runs",
        ):
            self.assertIn(table, self.tables())

    def test_migration_is_idempotent(self) -> None:
        self.migrate_to(4)
        migrate(self.conn)
        self.assertEqual(migrate(self.conn), 0)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)

    def test_a_fresh_database_reaches_the_current_version(self) -> None:
        migrate(self.conn)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)
        self.assertIn("pipeline_state", self.columns())

    # --- invariants the schema is supposed to enforce ---------------------------------

    def test_only_one_profile_can_be_active(self) -> None:
        self.migrate_to(5)
        self.conn.execute(
            "INSERT INTO profiles (version, created_at, is_active, profile_json, "
            "summary_text) VALUES (1, 'now', 1, '{}', 's')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO profiles (version, created_at, is_active, profile_json, "
                "summary_text) VALUES (2, 'now', 1, '{}', 's')"
            )

    def test_a_posting_has_at_most_one_verdict_per_profile_version(self) -> None:
        self.migrate_to(5)
        self.conn.execute("INSERT INTO jobs (id, job_key, title) VALUES (1, 'k', 'T')")
        insert = (
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit_score, "
            "verdict, created_at) VALUES (1, 1, 'm', ?, 'stretch', 'now')"
        )
        self.conn.execute(insert, (50,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (60,))

    def test_verdicts_are_removed_with_their_posting(self) -> None:
        self.migrate_to(5)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("INSERT INTO jobs (id, job_key, title) VALUES (1, 'k', 'T')")
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit_score, "
            "verdict, created_at) VALUES (1, 1, 'm', 50, 'stretch', 'now')"
        )
        self.conn.execute("DELETE FROM jobs WHERE id = 1")
        remaining = self.conn.execute("SELECT COUNT(*) FROM job_verdicts").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_the_research_tables_do_not_survive_to_head(self) -> None:
        """v5 created them; v24 removed the feature that wrote them."""
        migrate(self.conn)
        self.assertNotIn("company_dossiers", self.tables())
        self.assertNotIn("research_runs", self.tables())


if __name__ == "__main__":
    unittest.main()
