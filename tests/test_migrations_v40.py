"""Replacing source_state with per-endpoint proxy_state on upgrade to v40."""

import sqlite3
from unittest.mock import patch

from findajob.core import migrations


def test_upgrade_replaces_source_state_with_proxy_state() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:39]):
            migrations.migrate(conn)
        conn.execute(
            "INSERT INTO source_state (source, backoff_until, consecutive_trips, last_trip_reason) "
            "VALUES ('linkedin', '2026-11-05T11:56:16+00:00', 7, 'HTTP 429')"
        )
        conn.commit()

        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:40]):
            assert migrations.migrate(conn) == 1
        assert migrations.current_version(conn) == 40
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_state'"
            ).fetchone()
            is None
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(proxy_state)").fetchall()]
        assert columns == [
            "proxy_id",
            "source",
            "status",
            "backoff_until",
            "consecutive_failures",
            "last_error",
        ]
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_proxy_state_lookup'"
            ).fetchone()
            is not None
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:40]):
            assert migrations.migrate(conn) == 0
    finally:
        conn.close()
