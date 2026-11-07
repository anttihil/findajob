"""Location assignments are the search plan; saved locations are reusable choices."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from findajob.core import migrations
from findajob.core.database import Database
from findajob.search.capacity import capacity_for_targets
from findajob.search.repository import prune_cells, seed_cells
from findajob.search.targets import (
    add_query,
    delete_location,
    get_locations,
    get_queries,
    load_targets,
    save_location,
    toggle_query,
    update_query,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrations.migrate(db)
    save_location(
        db, "remote", "United States (remote)", search_label="United States", is_remote=True
    )
    save_location(db, "la", "Los Angeles")
    yield db
    db.close()


def sync(conn: sqlite3.Connection) -> None:
    specs = load_targets(conn=conn).cell_specs()
    seed_cells(conn, specs)
    prune_cells(conn, specs)


def test_migration_preserves_coverage_without_assigning_paused_locations() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:41]):
        migrations.migrate(db)
    db.executescript("""
        INSERT INTO search_queries (query, enabled) VALUES ('Active', 1), ('Paused', 0);
        INSERT INTO search_query_sources SELECT id, 'indeed' FROM search_queries;
        INSERT INTO search_locations
            (id, label, search_label, country, enabled)
        VALUES ('remote', 'Remote', 'Remote', 'US', 1), ('la', 'LA', 'LA', 'US', 0);
        INSERT INTO scrape_cells
            (source, location_id, query, search_label, country, total_scrapes, created_at)
        VALUES ('indeed', 'remote', 'Active', 'Remote', 'US', 7, '2026-11-01');
    """)
    old_cell_id = db.execute("SELECT id FROM scrape_cells").fetchone()[0]
    migrations.migrate(db)
    assert [q["location_ids"] for q in get_queries(db)] == [["remote"], ["remote"]]
    assert "enabled" not in get_locations(db)[0]
    assert {loc["id"] for loc in get_locations(db)} == {"remote", "la"}
    sync(db)
    row = db.execute("SELECT id, enabled, total_scrapes FROM scrape_cells").fetchone()
    assert tuple(row) == (old_cell_id, 1, 7)
    toggle_query(db, 2, True)
    sync(db)
    assert {s["location_id"] for s in load_targets(conn=db).cell_specs()} == {"remote"}
    db.close()


def test_selection_intersects_sources_and_preserves_cell_history(conn: sqlite3.Connection) -> None:
    qid = add_query(conn, "product engineer", sources=["indeed"], location_ids=["remote"])
    add_query(conn, "local engineer", sources=["linkedin"], location_ids=["la"])
    sync(conn)
    assert {
        (s["query"], s["location_id"], s["source"]) for s in load_targets(conn=conn).cell_specs()
    } == {
        ("product engineer", "remote", "indeed"),
        ("local engineer", "la", "linkedin"),
    }
    cell = conn.execute("SELECT id FROM scrape_cells WHERE query='product engineer'").fetchone()[0]
    conn.execute("UPDATE scrape_cells SET total_scrapes=7 WHERE id=?", (cell,))
    conn.commit()
    update_query(conn, qid, location_ids=["la"])
    sync(conn)
    assert tuple(
        conn.execute(
            "SELECT enabled,total_scrapes FROM scrape_cells WHERE id=?", (cell,)
        ).fetchone()
    ) == (0, 7)
    update_query(conn, qid, location_ids=["remote", "la", "remote"])
    sync(conn)
    assert tuple(
        conn.execute(
            "SELECT enabled,total_scrapes FROM scrape_cells WHERE id=?", (cell,)
        ).fetchone()
    ) == (1, 7)
    assert get_queries(conn)[0]["location_ids"] == ["la", "remote"]
    assert {s["location_id"] for s in load_targets(conn=conn).cell_specs(sources=("indeed",))} == {
        "remote",
        "la",
    }
    update_query(conn, qid, query_term="Product Engineer")
    assert get_queries(conn)[0]["location_ids"] == ["la", "remote"]
    assert (
        conn.execute("SELECT query FROM scrape_cells WHERE id=?", (cell,)).fetchone()[0]
        == "Product Engineer"
    )


def test_failed_location_migration_rolls_back_schema_and_can_be_retried() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:41]):
            migrations.migrate(db)
        db.executescript("""
            INSERT INTO search_queries (query, enabled) VALUES ('Engineer', 1);
            INSERT INTO search_locations (id, label, search_label, country)
            VALUES ('remote', 'Remote', 'United States', 'US');
        """)
        alters = 0

        def deny_second_alter(
            action: int,
            arg1: str | None,
            arg2: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            nonlocal alters
            if action == sqlite3.SQLITE_ALTER_TABLE:
                alters += 1
                if alters == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(deny_second_alter)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            migrations.migrate(db)
        db.set_authorizer(None)
        assert migrations.current_version(db) == 41
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='search_query_locations'"
        ).fetchall()
        columns = {row["name"] for row in db.execute("PRAGMA table_info(search_locations)")}
        assert "enabled" in columns
        assert "last_used_at" not in columns
        assert migrations.migrate(db) == 1
        assert get_queries(db)[0]["location_ids"] == ["remote"]
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_saved_locations_never_expand_coverage_and_do_not_change_plan_hash(
    conn: sqlite3.Connection,
) -> None:
    qid = add_query(conn, "Engineer", location_ids=["remote"])
    before = load_targets(conn=conn).fingerprint
    save_location(conn, "ny", "New York")
    assert load_targets(conn=conn).fingerprint == before
    assert {s["location_id"] for s in load_targets(conn=conn).cell_specs()} == {"remote"}
    update_query(conn, qid, location_ids=["remote"])
    assert load_targets(conn=conn).fingerprint == before  # Recency is not search behavior.
    update_query(conn, qid, location_ids=["la"])
    assert load_targets(conn=conn).fingerprint != before


@pytest.mark.parametrize("location_ids", [[], ["unknown"], ["remote", "unknown"]])
def test_invalid_selections_leave_the_whole_query_unchanged(
    conn: sqlite3.Connection, location_ids: list[str]
) -> None:
    qid = add_query(conn, "Engineer", location_ids=["remote"])
    before = get_queries(conn)
    recent_before = get_locations(conn)
    with pytest.raises(ValueError):
        update_query(conn, qid, query_term="Changed", sources=["google"], location_ids=location_ids)
    assert get_queries(conn) == before
    assert get_locations(conn) == recent_before
    with pytest.raises(ValueError):
        add_query(conn, "New", location_ids=location_ids)
    assert get_queries(conn) == before


def test_capacity_and_empty_plans_follow_assignments(conn: sqlite3.Connection) -> None:
    qid = add_query(conn, "Engineer", sources=["indeed"], location_ids=["remote"])
    add_query(conn, "Other", sources=["linkedin"], location_ids=["remote", "la"])
    cap = capacity_for_targets(load_targets(conn=conn), {})
    assert (cap["locations_in_use"], cap["search_pairs"], cap["total_cells"]) == (2, 3, 3)
    assert cap["sources"]["indeed"]["cells"] == 1
    assert cap["sources"]["linkedin"]["cells"] == 2
    sync(conn)
    delete_location(conn, "remote")
    assert get_queries(conn)[0]["location_ids"] == []
    sync(conn)
    assert {s["location_id"] for s in load_targets(conn=conn).cell_specs()} == {"la"}
    toggle_query(conn, qid + 1, False)
    toggle_query(conn, qid, False)
    sync(conn)
    assert conn.execute("SELECT COUNT(*) FROM scrape_cells WHERE enabled=1").fetchone()[0] == 0
    assert load_targets(conn=conn).validate() == []
    assert capacity_for_targets(load_targets(conn=conn), {})["zone"] == "empty"


def test_recent_choices_update_only_on_successful_location_saves(conn: sqlite3.Connection) -> None:
    assert all(loc["last_used_at"] is None for loc in get_locations(conn))
    qid = add_query(conn, "Engineer", location_ids=["la"])
    assert get_locations(conn)[0]["id"] == "la"
    used_at = get_locations(conn)[0]["last_used_at"]
    update_query(conn, qid, query_term="Renamed", enabled=False, sources=["google"])
    assert get_locations(conn)[0]["last_used_at"] == used_at
    update_query(conn, qid, location_ids=["remote"])
    assert get_locations(conn)[0]["id"] == "remote"
    # An SQL failure must roll back the mapping, recency, and any other changes.
    add_query(conn, "Duplicate", location_ids=["la"])
    before = get_queries(conn)
    locations = get_locations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        update_query(conn, qid, query_term="Duplicate", location_ids=["la"])
    assert get_queries(conn) == before
    assert get_locations(conn) == locations


def test_api_requires_selection_and_location_reuse_does_not_mutate_other_queries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from findajob.web import app as web

    path = str(tmp_path / "locations.db")
    Database(path).close()
    monkeypatch.setattr(web, "get_db", lambda: Database(path))

    def sync_api() -> None:
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            sync(db)

    monkeypatch.setattr(web, "_sync_target_cells", sync_api)
    client = TestClient(web.app)
    assert client.post("/api/targets/queries", json={"query": "Engineer"}).status_code == 422
    assert (
        client.post(
            "/api/targets/queries", json={"query": "Engineer", "location_ids": []}
        ).status_code
        == 400
    )
    location = {"id": "la", "label": "Los Angeles", "distance": 50}
    loc_id = client.post("/api/targets/locations", json=location).json()["id"]
    assert client.post("/api/targets/locations", json=location).json()["id"] == loc_id
    qid = client.post(
        "/api/targets/queries",
        json={"query": "Engineer", "sources": ["indeed"], "location_ids": [loc_id]},
    ).json()["id"]
    changed_id = client.post("/api/targets/locations", json={**location, "distance": 25}).json()[
        "id"
    ]
    assert changed_id != loc_id
    targets = client.get("/api/targets").json()
    assert targets["queries"][0]["location_ids"] == [loc_id]
    assert next(loc for loc in targets["locations"] if loc["id"] == loc_id)["distance"] == 50
    assert (
        next(loc for loc in targets["locations"] if loc["id"] == changed_id)["last_used_at"] is None
    )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT location_id FROM scrape_cells WHERE enabled=1").fetchall() == [
            (loc_id,)
        ]
    assert (
        client.put(f"/api/targets/queries/{qid}", json={"location_ids": ["missing"]}).status_code
        == 400
    )
    assert (
        client.put(f"/api/targets/queries/{qid}", json={"location_ids": [changed_id]}).status_code
        == 200
    )
    assert client.get("/api/targets").json()["queries"][0]["location_ids"] == [changed_id]
    assert (
        client.put(f"/api/targets/queries/{qid}/toggle", json={"enabled": False}).status_code == 200
    )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM scrape_cells WHERE enabled=1").fetchone()[0] == 0


def test_crawler_source_changes_recompile_assignments_and_keep_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from findajob.core import config, database
    from findajob.search import targets
    from findajob.web import app as web

    path = str(tmp_path / "jobs.db")
    defaults = tmp_path / "config.yaml"
    defaults.write_text("{}")
    monkeypatch.setattr(config, "CONFIG_PATH", str(defaults))
    monkeypatch.setattr(config, "CONFIG_LOCAL_PATH", str(tmp_path / "config.local.yaml"))
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(targets, "DB_PATH", path)
    monkeypatch.setattr(web, "get_db", lambda: Database(path))
    db = Database(path)
    try:
        save_location(db.conn, "remote", "Remote")
        add_query(db.conn, "Engineer", sources=["indeed"], location_ids=["remote"])
        sync(db.conn)
        cell_id = db.conn.execute("SELECT id FROM scrape_cells").fetchone()[0]
        db.conn.execute("UPDATE scrape_cells SET total_scrapes=7")
        db.conn.commit()
        client = TestClient(web.app)
        for enabled in (False, True):
            response = client.post(
                "/api/config",
                json={
                    "scraper": {"sources": {"indeed": enabled, "linkedin": False, "google": False}}
                },
            )
            assert response.status_code == 200
            rows = db.conn.execute("SELECT id,enabled,total_scrapes FROM scrape_cells").fetchall()
            assert [tuple(row) for row in rows] == [(cell_id, int(enabled), 7)]
            assert client.get("/api/targets/capacity").json()["total_cells"] == int(enabled)
            assert get_queries(db.conn)[0]["location_ids"] == ["remote"]
    finally:
        db.close()
