"""A search-target edit must reach the compiled scrape matrix.

`scrape_cells` is a materialized copy of the queries x locations matrix, so every edit to
the plan needs a recompile before the scraper sees it. These tests cover the two places
that used to get it wrong: a rename that stranded a cell's history, and the CLI, which
wrote the plan and stopped there.
"""

import pathlib
import sqlite3

import pytest

from careerradar.core.migrations import migrate
from careerradar.search.repository import prune_cells, seed_cells
from careerradar.taxonomy.repository import add_query, save_location, update_query
from careerradar.taxonomy.roles import RoleTaxonomy


def _migrated() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    specs = RoleTaxonomy(conn=conn).cell_specs(sources=("indeed",))
    seed_cells(conn, specs)
    prune_cells(conn, specs)


def test_query_rename_keeps_the_cell_and_its_history() -> None:
    conn = _migrated()
    save_location(conn, loc_id="testville", label="Testville", country="US")
    query_id = add_query(conn, query_term="Backend Enginer")
    _seed(conn)

    conn.execute("UPDATE scrape_cells SET total_scrapes = 7 WHERE query = 'Backend Enginer'")
    conn.commit()
    cell_id = conn.execute(
        "SELECT id FROM scrape_cells WHERE query = 'Backend Enginer'"
    ).fetchone()["id"]

    update_query(conn, query_id=query_id, query_term="Backend Engineer")
    _seed(conn)

    rows = conn.execute(
        "SELECT id, enabled, total_scrapes FROM scrape_cells WHERE query = 'Backend Engineer'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == cell_id, "the rename started a new cell instead of moving the old one"
    assert rows[0]["enabled"] == 1
    assert rows[0]["total_scrapes"] == 7, "scrape history did not survive the rename"

    assert not conn.execute(
        "SELECT 1 FROM scrape_cells WHERE query = 'Backend Enginer'"
    ).fetchall(), "the pre-rename cell was left stranded"


def test_query_rename_keeps_the_market_join_intact() -> None:
    conn = _migrated()
    save_location(conn, loc_id="testville", label="Testville", country="US")
    query_id = add_query(conn, query_term="Backend Enginer")
    _seed(conn)

    update_query(conn, query_id=query_id, query_term="Backend Engineer")

    # market/repository.py recovers the query row by joining on the text. Both sides move
    # together, so the join still resolves after a rename.
    row = conn.execute(
        """
        SELECT sq.id FROM scrape_cells sc
          LEFT JOIN search_queries sq ON sq.query = sc.query
         WHERE sc.query = 'Backend Engineer'
        """
    ).fetchone()
    assert row["id"] == query_id


def test_cli_target_edits_recompile_the_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import careerradar.cli as cli

    db_path = str(tmp_path / "jobs.db")
    # DB_PATH is bound at import in each module that needs it, so every binding moves.
    for module in (
        "careerradar.core.paths",
        "careerradar.core.database",
        "careerradar.taxonomy.roles",
    ):
        monkeypatch.setattr(f"{module}.DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    save_location(conn, loc_id="testville", label="Testville", country="US")
    conn.close()

    assert cli.main(["target", "add", "Backend Engineer"]) == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cells = conn.execute("SELECT source, query FROM scrape_cells WHERE enabled = 1").fetchall()
    conn.close()
    assert [r["query"] for r in cells] == ["Backend Engineer"] * len(cells)
    assert cells, "the CLI wrote the query but never recompiled scrape_cells"
