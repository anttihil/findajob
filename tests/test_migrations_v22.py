"""Unit tests for schema migration v22 (Drop Legacy Tables and Columns)."""

import sqlite3

from careerradar.core.migrations import MIGRATIONS, SCHEMA_VERSION, current_version, migrate


def test_migration_v22_drops_legacy_tables_and_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Apply up to v21
    cursor = conn.cursor()
    for target, _desc, fn in MIGRATIONS:
        if target > 21:
            break
        fn(cursor)
        cursor.execute(f"PRAGMA user_version = {int(target)}")
    conn.commit()

    assert current_version(conn) == 21

    # Verify legacy tables exist before v22
    tables_before = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for expected_legacy in [
        "profiles",
        "profile_documents",
        "interview_turns",
        "resume_master_profile",
        "role_market_stats",
        "skill_market_stats",
        "skill_candidates",
        "job_blockers",
    ]:
        assert expected_legacy in tables_before

    # Insert a sample generated resume before migration
    conn.execute(
        """
        INSERT INTO jobs (id, job_key, title, url, description, sync_run_id)
        VALUES (1, 'k1', 'Staff Engineer', 'https://example.com/1', 'desc', 1)
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

    # Now apply migration v22
    applied = migrate(conn)
    assert applied >= 1
    assert current_version(conn) == SCHEMA_VERSION
    assert current_version(conn) >= 22

    # Verify legacy tables are gone
    tables_after = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for dropped_table in [
        "profiles",
        "profile_documents",
        "interview_turns",
        "resume_master_profile",
        "role_market_stats",
        "skill_market_stats",
        "skill_candidates",
        "job_blockers",
    ]:
        assert dropped_table not in tables_after

    # Verify ewma_yield_per_day is gone from scrape_cells
    cells_cols = {r[1] for r in conn.execute("PRAGMA table_info(scrape_cells)").fetchall()}
    assert "ewma_yield_per_day" not in cells_cols

    # Verify profile_version is gone from generated_resumes
    gr_cols = {r[1] for r in conn.execute("PRAGMA table_info(generated_resumes)").fetchall()}
    assert "profile_version" not in gr_cols
    assert "docx_path" in gr_cols
    assert "job_id" in gr_cols

    # Verify generated_resumes records survived
    res_row = conn.execute("SELECT * FROM generated_resumes WHERE job_id = 1").fetchone()
    assert res_row is not None
    assert res_row["docx_path"] == "/tmp/res.docx"

    # Verify foreign key check passes
    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert len(fk_violations) == 0

    conn.close()
