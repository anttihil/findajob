"""SQLite repository for search queries and search locations."""

import sqlite3
from typing import Any


# ---------------------------------------------------------------------------------------
# Search queries
# ---------------------------------------------------------------------------------------
def get_queries(
    conn: sqlite3.Connection,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch the search queries sent to job boards."""
    query = "SELECT id, query, enabled FROM search_queries"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY id ASC"

    return [dict(row) for row in conn.execute(query).fetchall()]


def add_query(
    conn: sqlite3.Connection,
    query_term: str,
    enabled: bool = True,
) -> int:
    """Add a search query, or re-enable one that already exists."""
    row = conn.execute("SELECT id FROM search_queries WHERE query = ?", (query_term,)).fetchone()
    if row:
        conn.execute(
            "UPDATE search_queries SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, row[0]),
        )
        conn.commit()
        return row[0]

    cursor = conn.execute(
        "INSERT INTO search_queries (query, enabled) VALUES (?, ?)",
        (query_term, 1 if enabled else 0),
    )
    conn.commit()
    return cursor.lastrowid or 0


def delete_query(conn: sqlite3.Connection, query_id: int) -> None:
    conn.execute("DELETE FROM search_queries WHERE id = ?", (query_id,))
    conn.commit()


def toggle_query(conn: sqlite3.Connection, query_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE search_queries SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, query_id),
    )
    conn.commit()


def update_query(
    conn: sqlite3.Connection,
    query_id: int,
    query_term: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Update the text and/or the enabled state of a search query."""
    updates: list[str] = []
    params: list[Any] = []
    old_term: str | None = None
    if query_term is not None:
        row = conn.execute("SELECT query FROM search_queries WHERE id = ?", (query_id,)).fetchone()
        old_term = row[0] if row else None
        updates.append("query = ?")
        params.append(query_term)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not updates:
        return
    params.append(query_id)
    conn.execute(f"UPDATE search_queries SET {', '.join(updates)} WHERE id = ?", params)
    # The query text is part of the scrape cell's identity, so a rename that touched only
    # this table would leave seed_cells to build a second set of cells and disable the
    # first -- silently stranding that query's whole scrape history, EWMA and quality
    # samples. Carry the cells across in the same transaction instead.
    if old_term is not None and old_term != query_term:
        conn.execute("UPDATE scrape_cells SET query = ? WHERE query = ?", (query_term, old_term))
    conn.commit()


# ---------------------------------------------------------------------------------------
# Search locations
# ---------------------------------------------------------------------------------------
def get_locations(
    conn: sqlite3.Connection,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch the configured search locations."""
    query = """
        SELECT id, label, search_label, country, indeed_country,
               is_remote, weight, distance, enabled
          FROM search_locations
    """
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY id ASC"

    return [dict(row) for row in conn.execute(query).fetchall()]


def save_location(
    conn: sqlite3.Connection,
    loc_id: str,
    label: str,
    search_label: str | None = None,
    country: str = "US",
    indeed_country: str = "usa",
    is_remote: bool = False,
    weight: float = 1.0,
    distance: int = 50,
    enabled: bool = True,
) -> None:
    """Insert or update a search location."""
    conn.execute(
        """
        INSERT INTO search_locations (
            id, label, search_label, country, indeed_country,
            is_remote, weight, distance, enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label,
            search_label = excluded.search_label,
            country = excluded.country,
            indeed_country = excluded.indeed_country,
            is_remote = excluded.is_remote,
            weight = excluded.weight,
            distance = excluded.distance,
            enabled = excluded.enabled
        """,
        (
            loc_id,
            label,
            search_label or label,
            country,
            indeed_country,
            1 if is_remote else 0,
            weight,
            distance,
            1 if enabled else 0,
        ),
    )
    conn.commit()


def toggle_location(conn: sqlite3.Connection, loc_id: str, enabled: bool) -> None:
    conn.execute(
        "UPDATE search_locations SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, loc_id),
    )
    conn.commit()


def delete_location(conn: sqlite3.Connection, loc_id: str) -> None:
    conn.execute("DELETE FROM search_locations WHERE id = ?", (loc_id,))
    conn.commit()
