"""Detail reuse metadata upgrades without inventing fetch dates for old content."""

import sqlite3
from unittest.mock import patch

from findajob.core import migrations


def test_upgrade_preserves_legacy_descriptions_and_adds_budget_metadata():
    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:38]):
            migrations.migrate(conn)
        conn.execute(
            "INSERT INTO jobs (job_key, title, source, description, last_seen_at, times_seen) "
            "VALUES ('linkedin-li-1', 'Engineer', 'linkedin', "
            "'Original description', '2026-10-01', 9)"
        )
        conn.commit()
        assert migrations.migrate(conn) == 1
        assert conn.execute(
            "SELECT description, last_seen_at, times_seen, "
            "details_fetched_at, details_attempted_at "
            "FROM jobs"
        ).fetchone() == ("Original description", "2026-10-01", 9, None, None)
        assert migrations.current_version(conn) == 39
        assert migrations.migrate(conn) == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
