"""Unit tests for schema migration v24 and the search-target repository."""

import sqlite3

from careerradar.core.migrations import current_version, migrate
from careerradar.search.targets import (
    add_query,
    delete_location,
    delete_query,
    get_locations,
    get_queries,
    save_location,
    toggle_location,
    toggle_query,
)


def _migrated() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_migration_v24_target_schema():
    conn = _migrated()
    assert current_version(conn) >= 24

    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
    for gone in (
        "target_roles",
        "target_queries",
        "target_locations",
        "company_dossiers",
        "research_runs",
        "idx_jobs_access",
        "idx_jobs_family_found",
    ):
        assert gone not in tables, f"{gone} survived v24"

    queries_cols = {row["name"] for row in conn.execute("PRAGMA table_info(search_queries)")}
    assert queries_cols == {"id", "query", "enabled"}

    locs_cols = {row["name"] for row in conn.execute("PRAGMA table_info(search_locations)")}
    for gone in ("access", "weight"):
        assert gone not in locs_cols, f"search_locations.{gone} survived"
    for col in (
        "id",
        "label",
        "search_label",
        "country",
        "indeed_country",
        "is_remote",
        "distance",
        "enabled",
    ):
        assert col in locs_cols, f"Missing {col} in search_locations"

    jobs_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for gone in ("role_family", "role_family_hint", "access", "dossier_id"):
        assert gone not in jobs_cols, f"jobs.{gone} survived v24"


def test_migration_v24_embeds_the_search_parameters_on_the_cell():
    conn = _migrated()
    cell_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scrape_cells)")}
    for gone in ("role_family", "tier", "weight"):
        assert gone not in cell_cols
    for col in ("search_label", "country", "indeed_country", "is_remote", "distance"):
        assert col in cell_cols, f"Missing {col} in scrape_cells"

    obs_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cell_observations)")}
    for gone in ("role_family", "returned_on_topic"):
        assert gone not in obs_cols


def test_migration_v24_unique_key_is_source_location_query():
    conn = _migrated()
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'scrape_cells'"
    ).fetchone()
    assert "UNIQUE (source, location_id, query)" in row["sql"]


def test_search_queries_crud():
    conn = _migrated()

    qid = add_query(conn, query_term="Backend Engineer")
    assert qid > 0
    # The query text is the key: adding it twice re-uses the row.
    assert add_query(conn, query_term="Backend Engineer") == qid
    assert any(q["query"] == "Backend Engineer" for q in get_queries(conn))

    toggle_query(conn, qid, enabled=False)
    assert not any(q["id"] == qid for q in get_queries(conn, enabled_only=True))

    delete_query(conn, qid)
    assert not any(q["id"] == qid for q in get_queries(conn))


def test_search_locations_crud():
    conn = _migrated()

    save_location(
        conn,
        loc_id="helsinki",
        label="Helsinki, Finland",
        search_label="Helsinki",
        country="FI",
        indeed_country="finland",
        is_remote=False,
        distance=50,
        enabled=True,
    )

    hel = next((loc for loc in get_locations(conn) if loc["id"] == "helsinki"), None)
    assert hel is not None
    assert hel["country"] == "FI"

    toggle_location(conn, "helsinki", enabled=False)
    assert not any(loc["id"] == "helsinki" for loc in get_locations(conn, enabled_only=True))

    delete_location(conn, "helsinki")
    assert not any(loc["id"] == "helsinki" for loc in get_locations(conn))
