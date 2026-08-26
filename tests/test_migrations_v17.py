"""Unit tests for schema migration v17 (Resume Builder)."""

import sqlite3

from careerradar.core.migrations import current_version, migrate


def test_migration_v17():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    assert current_version(conn) == 17

    # Check resume_master_profile table exists and is seeded
    row = conn.execute("SELECT * FROM resume_master_profile").fetchone()
    assert row is not None
    assert row["name"] != ""
    has_ucla = "UCLA" in (row["experience_json"] or "")
    has_brain = "Acme Robotics" in (row["experience_json"] or "")
    assert has_ucla or has_brain

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
