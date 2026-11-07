"""The failure-category upgrade adds a column without reinterpreting proxy history."""

import sqlite3
from unittest.mock import patch

from findajob.core import migrations


def test_upgrade_preserves_existing_proxy_state_and_leaves_categories_unknown() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:40]):
            migrations.migrate(conn)
        errors = {
            "connect": "ProxyError: Unable to connect to proxy (ConnectTimeoutError: timed out)",
            "read": "Read timed out",
            "auth": "407 Proxy Authentication Required",
            "auth_timeout": "407 Proxy Authentication Required (timeout)",
            "ambiguous": "Unable to connect to proxy",
            "url": "Unable to connect to proxy for URL https://example.test/search?timeout=10",
            "missing": None,
        }
        for pid, error in errors.items():
            for source in ("indeed", "linkedin"):
                conn.execute(
                    "INSERT INTO proxy_state VALUES (?, ?, 'broken', NULL, 3, ?)",
                    (pid, source, error),
                )
        conn.execute(
            "INSERT INTO proxy_state VALUES "
            "('active', 'linkedin', 'backoff', '2099-01-01', 2, '429')"
        )
        conn.execute(
            "INSERT INTO proxy_state VALUES "
            "('mixed', 'indeed', 'backoff', '2099-01-01', 2, '407 Proxy Authentication Required')"
        )
        conn.commit()
        before = conn.execute("SELECT * FROM proxy_state ORDER BY proxy_id, source").fetchall()

        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:41]):
            assert migrations.migrate(conn) == 1
        assert migrations.current_version(conn) == 41
        after = conn.execute("SELECT * FROM proxy_state ORDER BY proxy_id, source").fetchall()
        assert after == [(*row, None) for row in before]
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:41]):
            assert migrations.migrate(conn) == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
