"""Unit tests for schema migration v20 and target repository."""

import sqlite3

from careerradar.core.migrations import current_version, migrate
from careerradar.taxonomy.repository import (
    add_target_query,
    delete_target_location,
    delete_target_query,
    delete_target_role,
    get_target_locations,
    get_target_queries,
    get_target_roles,
    save_target_location,
    save_target_role,
    toggle_target_location,
    toggle_target_query,
    toggle_target_role,
)


def test_migration_v20_creates_tables():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    assert current_version(conn) >= 20

    # Verify target_roles table
    roles_cols = {row["name"] for row in conn.execute("PRAGMA table_info(target_roles)")}
    for col in ("id", "key", "label", "resume", "aliases_json", "enabled", "created_at"):
        assert col in roles_cols, f"Missing {col} in target_roles"

    # Verify target_queries table
    queries_cols = {row["name"] for row in conn.execute("PRAGMA table_info(target_queries)")}
    for col in ("id", "role_key", "query", "enabled"):
        assert col in queries_cols, f"Missing {col} in target_queries"

    # Verify target_locations table
    locs_cols = {row["name"] for row in conn.execute("PRAGMA table_info(target_locations)")}
    expected_loc_cols = (
        "id",
        "label",
        "search_label",
        "country",
        "indeed_country",
        "is_remote",
        "access",
        "weight",
        "distance",
        "enabled",
    )
    for col in expected_loc_cols:
        assert col in locs_cols, f"Missing {col} in target_locations"


def test_target_roles_crud():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    save_target_role(
        conn,
        key="ai_engineer",
        label="AI Engineer",
        aliases=["ai engineer", "genai engineer"],
        resume="fullstack_ai.md",
        enabled=True,
    )

    roles = get_target_roles(conn)
    ai_role = next((r for r in roles if r["key"] == "ai_engineer"), None)
    assert ai_role is not None
    assert ai_role["label"] == "AI Engineer"
    assert ai_role["aliases"] == ["ai engineer", "genai engineer"]
    assert ai_role["enabled"] == 1

    toggle_target_role(conn, "ai_engineer", enabled=False)
    enabled_roles = get_target_roles(conn, enabled_only=True)
    assert not any(r["key"] == "ai_engineer" for r in enabled_roles)

    delete_target_role(conn, "ai_engineer")
    assert not any(r["key"] == "ai_engineer" for r in get_target_roles(conn))


def test_target_queries_crud():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    save_target_role(conn, key="backend_engineer", label="Backend Engineer")
    qid = add_target_query(conn, role_key="backend_engineer", query_term="Backend Engineer")
    assert qid > 0

    queries = get_target_queries(conn, role_key="backend_engineer")
    assert len(queries) >= 1
    assert any(q["query"] == "Backend Engineer" for q in queries)

    toggle_target_query(conn, qid, enabled=False)
    assert not any(q["id"] == qid for q in get_target_queries(conn, enabled_only=True))

    delete_target_query(conn, qid)
    assert not any(q["id"] == qid for q in get_target_queries(conn))


def test_target_locations_crud():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    save_target_location(
        conn,
        loc_id="helsinki",
        label="Helsinki, Finland",
        search_label="Helsinki",
        country="FI",
        indeed_country="finland",
        is_remote=False,
        access="relocation",
        weight=0.8,
        distance=50,
        enabled=True,
    )

    locs = get_target_locations(conn)
    hel = next((loc for loc in locs if loc["id"] == "helsinki"), None)
    assert hel is not None
    assert hel["country"] == "FI"
    assert hel["access"] == "relocation"

    toggle_target_location(conn, "helsinki", enabled=False)
    assert not any(loc["id"] == "helsinki" for loc in get_target_locations(conn, enabled_only=True))

    delete_target_location(conn, "helsinki")
    assert not any(loc["id"] == "helsinki" for loc in get_target_locations(conn))
