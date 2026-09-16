"""Tests for migration v16: timestamp normalization and unixepoch liveness view."""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.core.database import Database
from findajob.core.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_pragmas,
    current_version,
    migrate,
)


def ago(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrationV16Tests(unittest.TestCase):
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

    def test_backfills_date_only_postings(self) -> None:
        self.migrate_to(15)
        # Insert raw rows as they existed in v15
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, date_posted, sync_run_id) "
            "VALUES (1, 'k1', 'Eng 1', 'http://x/1', 'desc', '2026-08-20', 1)"
        )
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, date_posted, sync_run_id) "
            "VALUES (2, 'k2', 'Eng 2', 'http://x/2', 'desc', '2026-08-21T08:30:00Z', 1)"
        )
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, date_posted, sync_run_id) "
            "VALUES (3, 'k3', 'Eng 3', 'http://x/3', 'desc', NULL, 1)"
        )
        self.conn.commit()

        migrate(self.conn)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 16)

        query = "SELECT id, date_posted FROM jobs"
        jobs = {row["id"]: row["date_posted"] for row in self.conn.execute(query)}
        self.assertEqual(jobs[1], "2026-08-20T12:00:00Z")
        self.assertEqual(jobs[2], "2026-08-21T08:30:00Z")
        self.assertIsNone(jobs[3])

    def test_liveness_view_states_with_unixepoch(self) -> None:
        migrate(self.conn)

        cell_insert = (
            "INSERT INTO scrape_cells (id, source, location_id, query, search_label, "
            "country, last_success_at, last_hours_old, created_at) "
            "VALUES (?, 'indeed', 'los_angeles', ?, 'Los Angeles, CA', 'US', ?, 24, "
            "'2026-07-01T00:00:00Z')"
        )
        self.conn.execute(cell_insert, (1, "q1", ago(hours=24)))
        self.conn.execute(cell_insert, (2, "q2", ago(days=8)))
        self.conn.execute(cell_insert, (3, "q3", ago(minutes=1)))

        job_insert = (
            "INSERT INTO jobs (id, job_key, title, url, description, date_posted, "
            "scrape_cell_id, last_seen_at, sync_run_id) "
            "VALUES (?, ?, 'Eng', ?, 'desc', ?, ?, ?, 1)"
        )
        # Job 1: likely_closed (cell scraped 24h ago with 24h window, posted 30h ago, seen 3d ago)
        self.conn.execute(job_insert, (1, "k1", "http://x/1", ago(hours=30), 1, ago(days=3)))
        # Job 2: stale (cell 2 last scraped 8 days ago)
        self.conn.execute(job_insert, (2, "k2", "http://x/2", ago(days=8), 2, ago(days=8)))
        # Job 3: live (cell 3 scraped 1m ago, seen 10m ago)
        self.conn.execute(job_insert, (3, "k3", "http://x/3", ago(hours=5), 3, ago(minutes=10)))
        # Job 4: unknown (aged out of window)
        self.conn.execute(job_insert, (4, "k4", "http://x/4", ago(days=10), 1, ago(days=9)))
        self.conn.commit()

        # Check v_job_liveness
        liveness_rows = {
            row["job_id"]: row["liveness"]
            for row in self.conn.execute("SELECT job_id, liveness FROM v_job_liveness")
        }
        self.assertEqual(liveness_rows[1], "likely_closed")
        self.assertEqual(liveness_rows[2], "stale")
        self.assertEqual(liveness_rows[3], "live")
        self.assertEqual(liveness_rows[4], "unknown")

        # Check inlined _LIVENESS_CASE agreement
        inlined = {
            row["id"]: row["liveness"]
            for row in self.conn.execute(
                f"SELECT jobs.id, {Database._LIVENESS_CASE} AS liveness "
                "FROM jobs LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id"
            )
        }
        self.assertEqual(inlined, liveness_rows)


if __name__ == "__main__":
    unittest.main()
