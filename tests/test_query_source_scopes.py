"""Source-scoped scheduled query regression tests."""

import sqlite3

from findajob.core.migrations import migrate
from findajob.search.repository import prune_cells, seed_cells
from findajob.search.targets import add_query, load_targets, save_location, update_query


def _migrated() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _seed(conn: sqlite3.Connection, sources: tuple[str, ...] = ("indeed", "linkedin")) -> None:
    specs = load_targets(conn=conn).cell_specs(sources=sources)
    seed_cells(conn, specs)
    prune_cells(conn, specs)


def test_migration_backfills_existing_queries_to_both_sources() -> None:
    conn = _migrated()
    conn.execute("DROP TABLE search_query_sources")
    conn.execute("INSERT INTO search_queries (query) VALUES ('Platform Engineer')")
    query_id = conn.execute(
        "SELECT id FROM search_queries WHERE query = 'Platform Engineer'"
    ).fetchone()[0]
    conn.execute("PRAGMA user_version = 30")
    conn.commit()
    migrate(conn)
    rows = conn.execute(
        "SELECT source FROM search_query_sources WHERE query_id = ? ORDER BY source", (query_id,)
    ).fetchall()
    assert [row["source"] for row in rows] == ["indeed", "linkedin"]


def test_source_scopes_generate_and_prune_only_their_own_cells() -> None:
    conn = _migrated()
    save_location(conn, loc_id="remote", label="Remote")
    indeed_id = add_query(conn, "Agentic AI Engineer", sources=["indeed"])
    add_query(conn, "Broad Engineer", sources=["linkedin"])
    _seed(conn)

    active = conn.execute(
        "SELECT source, query FROM scrape_cells WHERE enabled = 1 ORDER BY query, source"
    ).fetchall()
    assert [(row["source"], row["query"]) for row in active] == [
        ("indeed", "Agentic AI Engineer"),
        ("linkedin", "Broad Engineer"),
    ]

    indeed_cell = conn.execute(
        "SELECT id FROM scrape_cells WHERE source = 'indeed' AND query = 'Agentic AI Engineer'"
    ).fetchone()["id"]
    conn.execute("UPDATE scrape_cells SET total_scrapes = 4 WHERE id = ?", (indeed_cell,))
    conn.commit()
    update_query(conn, indeed_id, sources=["linkedin"])
    _seed(conn)

    old = conn.execute(
        "SELECT enabled, total_scrapes FROM scrape_cells WHERE id = ?", (indeed_cell,)
    ).fetchone()
    assert (old["enabled"], old["total_scrapes"]) == (0, 4)
    assert (
        conn.execute(
            "SELECT enabled FROM scrape_cells "
            "WHERE source = 'linkedin' AND query = 'Agentic AI Engineer'"
        ).fetchone()["enabled"]
        == 1
    )


def test_global_source_switch_intersects_query_scope() -> None:
    conn = _migrated()
    save_location(conn, loc_id="remote", label="Remote")
    add_query(conn, "Platform Engineer", sources=["indeed", "linkedin"])
    _seed(conn, sources=("indeed",))
    rows = conn.execute("SELECT source FROM scrape_cells WHERE enabled = 1").fetchall()
    assert [row["source"] for row in rows] == ["indeed"]
