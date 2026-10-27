"""Retiring screenings preserves postings and completed verdicts on upgrade."""

import sqlite3
from unittest.mock import patch

from findajob.core import migrations


def test_upgrade_drops_screenings_and_preserves_verdicts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:36]):
            migrations.migrate(conn)
        conn.executemany(
            "INSERT INTO jobs (id, job_key, title, url, description, status) "
            "VALUES (?, ?, 'Engineer', ?, 'full description', 'unread')",
            [(1, "one", "https://example.test/1"), (2, "two", "https://example.test/2")],
        )
        conn.executemany(
            "INSERT INTO job_screenings (job_id, profile_version, advance, reason, stage, "
            "model, review_model, profile_hash, posting_hash, prompt_hash, prompt_version, "
            "schema_version, state_json, created_at) "
            "VALUES (?, 1, 0, 'Old screening', 'screening_excluded', 'm', 'm', 'p', 'j', "
            "'h', '1', 1, '{}', '2026-10-01')",
            [(1,), (2,)],
        )
        conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, priority, stage, "
            "reason_description, created_at) "
            "VALUES (2, 1, 'm', 'first', 'assessed', 'Completed assessment', '2026-10-02')"
        )
        conn.commit()
        verdict = dict(conn.execute("SELECT * FROM job_verdicts").fetchone())
        assert migrations.migrate(conn) == 1
        assert migrations.current_version(conn) == 37
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'job_screenings'").fetchone()
            is None
        )
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert dict(conn.execute("SELECT * FROM job_verdicts").fetchone()) == verdict
        assessments = conn.execute("SELECT * FROM v_job_assessments ORDER BY job_id").fetchall()
        assert assessments[0]["stage"] == "unreviewed"
        assert assessments[0]["reason_description"] is None
        assert assessments[1]["priority"] == "first"
        assert assessments[1]["reason_description"] == "Completed assessment"
        assert not any(name.startswith("screening_") for name in dict(assessments[0]))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrations.migrate(conn) == 0
    finally:
        conn.close()
