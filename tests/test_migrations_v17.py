"""Unit tests for schema migration v17 (Resume Builder)."""

import sqlite3

from careerradar.core.migrations import MIGRATIONS, apply_pragmas, current_version


def test_migration_v17():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    apply_pragmas(conn)
    cursor = conn.cursor()
    for target, _desc, fn in MIGRATIONS:
        if target > 17:
            break
        fn(cursor)
        cursor.execute(f"PRAGMA user_version = {int(target)}")
    conn.commit()

    assert current_version(conn) == 17

    # Check resume_master_profile table exists
    row = conn.execute("SELECT * FROM resume_master_profile").fetchone()
    assert row is not None

    # Check generated_resumes table exists and respects foreign key to jobs
    conn.execute(
        """
        INSERT INTO jobs (id, job_key, title, url, description, sync_run_id)
        VALUES (1, 'k1', 'Software Engineer', 'https://example.com/1', 'desc', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO generated_resumes
            (job_id, model, created_at, docx_path, resume_json, summary, status)
        VALUES (
            1, 'deepseek-chat', '2026-08-25T00:00:00Z', '/tmp/res.docx', '{}',
            'test summary', 'generated'
        )
        """
    )
    conn.commit()

    r = conn.execute("SELECT * FROM generated_resumes WHERE job_id = 1").fetchone()
    assert r is not None
    assert r["docx_path"] == "/tmp/res.docx"
    conn.close()
