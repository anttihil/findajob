"""Removing the assessment view preserves stored verdicts and their history."""

import sqlite3
from unittest.mock import patch

from findajob.core import migrations


def test_upgrade_removes_assessment_view_without_changing_verdicts() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:37]):
            migrations.migrate(conn)
        conn.execute(
            "INSERT INTO jobs (id, job_key, title, url) VALUES (1, 'one', 'Engineer', 'https://example.test')"
        )
        conn.execute(
            "INSERT INTO job_verdicts "
            "(job_id, profile_version, model, fit, reason_type, created_at) "
            "VALUES (1, 1, 'old', 1, 'match', '2026-10-01')"
        )
        conn.execute("INSERT INTO job_verdict_history SELECT *, '2026-10-02' FROM job_verdicts")
        conn.commit()
        verdicts = conn.execute("SELECT * FROM job_verdicts").fetchall()
        history = conn.execute("SELECT * FROM job_verdict_history").fetchall()
        assert migrations.migrate(conn) == 1
        assert migrations.current_version(conn) == 38
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='v_job_assessments'").fetchone()
            is None
        )
        assert conn.execute("SELECT * FROM job_verdicts").fetchall() == verdicts
        assert conn.execute("SELECT * FROM job_verdict_history").fetchall() == history
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrations.migrate(conn) == 0
    finally:
        conn.close()
