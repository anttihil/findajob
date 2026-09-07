"""Schema migration v27: the taxonomy_hash columns are gone."""

import sqlite3

from careerradar.core.migrations import current_version, migrate


def _migrated() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_migration_v27_drops_taxonomy_hash():
    conn = _migrated()
    assert current_version(conn) >= 27

    for table in ("jobs", "sync_runs"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "taxonomy_hash" not in cols, f"taxonomy_hash survived v27 on {table}"


def test_migration_v27_is_idempotent_on_a_database_that_never_had_the_column():
    conn = _migrated()
    from careerradar.core.migrations import _v27_drop_taxonomy_hash

    _v27_drop_taxonomy_hash(conn.cursor())
