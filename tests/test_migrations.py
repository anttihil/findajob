"""Migration tests.

The important cases are the ones that only show up against a *pre-existing* database:
a v1 database with real rows must gain the new schema without losing data, and the runner
must be safe to call on every connection.
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

V1_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT UNIQUE,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    country TEXT,
    url TEXT UNIQUE,
    description TEXT,
    source TEXT,
    match_score INTEGER DEFAULT 0,
    matched_skills TEXT,
    resume_match TEXT,
    status TEXT DEFAULT 'unread',
    date_found TEXT,
    date_applied TEXT
);
"""


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path + suffix
            if os.path.exists(candidate):
                os.unlink(candidate)

    def _v1_db_with_rows(self):
        """A database shaped like the one already in production, with representative rows."""
        conn = sqlite3.connect(self.path)
        conn.executescript(V1_SCHEMA)
        rows = [
            (
                "jt-1",
                "Senior Fullstack Engineer",
                "Spotify",
                "Stockholm",
                "SE",
                "https://example.test/1",
                "x" * 3000,
                "JobTech Sweden",
            ),
            (
                "hn-2",
                "Platform Engineer",
                "Acme",
                "Remote",
                "US",
                "https://example.test/2",
                "y" * 500,
                "HackerNews",
            ),
            # A genuinely short description, to prove the quality backfill discriminates.
            (
                "wwr-3",
                "Product Manager",
                "Globex",
                "Remote",
                "US",
                "https://example.test/3",
                "short",
                "WeWorkRemotely",
            ),
        ]
        conn.executemany(
            "INSERT INTO jobs (job_key, title, company, location, country, url,"
            " description, source) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        return conn

    def test_fresh_database_reaches_head(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            applied = migrate(conn)
            self.assertEqual(applied, len(MIGRATIONS))
            self.assertEqual(current_version(conn), SCHEMA_VERSION)
        finally:
            conn.close()

    def test_v1_database_is_upgraded_without_data_loss(self) -> None:
        conn = self._v1_db_with_rows()
        try:
            migrate(conn)
            self.assertEqual(current_version(conn), SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)
            # Original values survive the ALTER TABLEs.
            title = conn.execute("SELECT title FROM jobs WHERE job_key = 'jt-1'").fetchone()[0]
            self.assertEqual(title, "Senior Fullstack Engineer")
        finally:
            conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            migrate(conn)
            self.assertEqual(migrate(conn), 0, "second run should apply nothing")
            self.assertEqual(migrate(conn), 0, "third run should apply nothing")
        finally:
            conn.close()

    def test_description_quality_backfill_discriminates(self) -> None:
        conn = self._v1_db_with_rows()
        try:
            migrate(conn)
            quality = dict(conn.execute("SELECT job_key, description_quality FROM jobs").fetchall())
            self.assertEqual(quality["jt-1"], "full")
            self.assertEqual(quality["hn-2"], "full")
            self.assertEqual(quality["wwr-3"], "snippet")
        finally:
            conn.close()

    def test_legacy_rows_excluded_from_both_eligibility_views(self) -> None:
        """Legacy rows have full descriptions but no cell provenance.

        They must stay browsable yet contribute to no statistic -- enforced by
        sync_run_id IS NULL rather than by pretending their descriptions are snippets.
        """
        conn = self._v1_db_with_rows()
        try:
            migrate(conn)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM v_supply_eligible").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v_skill_eligible").fetchone()[0], 0)
        finally:
            conn.close()

    def test_skill_eligible_requires_census_descriptions(self) -> None:
        """A top_k row must never reach skill demand, even with a full description.

        top_k descriptions are selected by pre-score, which correlates with the user's own
        skills -- pooling them would inflate demand for skills they already have.
        """
        conn = sqlite3.connect(self.path)
        try:
            migrate(conn)
            conn.execute(
                "INSERT INTO sync_runs (id, started_at, mode) VALUES (1, '2026-07-29', 'test')"
            )
            conn.executemany(
                "INSERT INTO jobs (job_key, title, url, sync_run_id, role_family,"
                " description_quality, desc_selection) VALUES (?,?,?,1,'x','full',?)",
                [
                    ("c1", "Census Row", "https://example.test/c1", "census"),
                    ("t1", "TopK Row", "https://example.test/t1", "top_k"),
                ],
            )
            conn.commit()

            # Both count for role supply: titles are complete regardless of selection.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM v_supply_eligible").fetchone()[0], 2
            )
            # Only the census row counts for skill demand.
            skill_keys = [r[0] for r in conn.execute("SELECT job_key FROM v_skill_eligible")]
            self.assertEqual(skill_keys, ["c1"])
        finally:
            conn.close()

    def test_wal_and_busy_timeout_enabled(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            migrate(conn)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertGreaterEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        finally:
            conn.close()

    def test_rows_with_no_role_family_are_excluded(self) -> None:
        """Off-target postings (a 'Maintenance Technician' from a Platform Engineer query)
        are retained for audit but must not reach any statistic."""
        conn = sqlite3.connect(self.path)
        try:
            migrate(conn)
            conn.execute(
                "INSERT INTO sync_runs (id, started_at, mode) VALUES (1, '2026-07-29', 'test')"
            )
            conn.execute(
                "INSERT INTO jobs (job_key, title, url, sync_run_id, role_family,"
                " description_quality, desc_selection)"
                " VALUES ('off','Maintenance Technician','https://example.test/o',1,"
                " NULL,'full','census')"
            )
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM v_supply_eligible").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v_skill_eligible").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
