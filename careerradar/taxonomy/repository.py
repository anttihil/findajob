"""SQLite repository for target roles, target queries, and target locations."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------
# Target Roles
# ---------------------------------------------------------------------------------------
def get_target_roles(
    conn: sqlite3.Connection,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch all configured target roles."""
    query = """
        SELECT id, key, label, resume, aliases_json, enabled, created_at
          FROM target_roles
    """
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY id ASC"

    cursor = conn.execute(query)
    roles = []
    for row in cursor.fetchall():
        r_dict = dict(row)
        try:
            aliases = json.loads(r_dict.get("aliases_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            aliases = []
        r_dict["aliases"] = aliases
        roles.append(r_dict)
    return roles


def save_target_role(
    conn: sqlite3.Connection,
    key: str,
    label: str,
    aliases: list[str] | None = None,
    resume: str | None = None,
    enabled: bool = True,
) -> None:
    """Insert or update a target role."""
    aliases_json = json.dumps(aliases or [])
    conn.execute(
        """
        INSERT INTO target_roles (key, label, resume, aliases_json, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label,
            resume = excluded.resume,
            aliases_json = excluded.aliases_json,
            enabled = excluded.enabled
        """,
        (key, label, resume, aliases_json, 1 if enabled else 0, _now()),
    )
    conn.commit()


def toggle_target_role(conn: sqlite3.Connection, key: str, enabled: bool) -> None:
    """Enable or disable a target role."""
    conn.execute(
        "UPDATE target_roles SET enabled = ? WHERE key = ?",
        (1 if enabled else 0, key),
    )
    conn.commit()


def delete_target_role(conn: sqlite3.Connection, key: str) -> None:
    """Delete a target role and its associated queries."""
    conn.execute("DELETE FROM target_roles WHERE key = ?", (key,))
    conn.commit()


# ---------------------------------------------------------------------------------------
# Target Queries
# ---------------------------------------------------------------------------------------
def get_target_queries(
    conn: sqlite3.Connection,
    role_key: str | None = None,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch search queries sent to job boards."""
    query = """
        SELECT id, role_key, query, enabled
          FROM target_queries
    """
    conditions = []
    params: list[Any] = []
    if role_key:
        conditions.append("role_key = ?")
        params.append(role_key)
    if enabled_only:
        conditions.append("enabled = 1")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id ASC"

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def add_target_query(
    conn: sqlite3.Connection,
    role_key: str,
    query_term: str,
    enabled: bool = True,
) -> int:
    """Add a search query term for a target role."""
    cursor = conn.execute(
        """
        INSERT INTO target_queries (role_key, query, enabled)
        VALUES (?, ?, ?)
        """,
        (role_key, query_term, 1 if enabled else 0),
    )
    conn.commit()
    return cursor.lastrowid or 0


def delete_target_query(conn: sqlite3.Connection, query_id: int) -> None:
    """Delete a specific search query."""
    conn.execute("DELETE FROM target_queries WHERE id = ?", (query_id,))
    conn.commit()


def toggle_target_query(conn: sqlite3.Connection, query_id: int, enabled: bool) -> None:
    """Enable or disable a specific search query."""
    conn.execute(
        "UPDATE target_queries SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, query_id),
    )
    conn.commit()


def update_target_query(
    conn: sqlite3.Connection,
    query_id: int,
    query_term: str | None = None,
    role_key: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Update query text, role family, and/or enabled state of a search query."""
    updates: list[str] = []
    params: list[Any] = []
    if query_term is not None:
        updates.append("query = ?")
        params.append(query_term)
    if role_key is not None:
        updates.append("role_key = ?")
        params.append(role_key)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not updates:
        return
    params.append(query_id)
    conn.execute(f"UPDATE target_queries SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


# ---------------------------------------------------------------------------------------
# Target Locations
# ---------------------------------------------------------------------------------------
def get_target_locations(
    conn: sqlite3.Connection,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch configured target search locations."""
    query = """
        SELECT id, label, search_label, country, indeed_country,
               is_remote, access, weight, distance, enabled
          FROM target_locations
    """
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY id ASC"

    cursor = conn.execute(query)
    return [dict(row) for row in cursor.fetchall()]


def save_target_location(
    conn: sqlite3.Connection,
    loc_id: str,
    label: str,
    search_label: str | None = None,
    country: str = "US",
    indeed_country: str = "usa",
    is_remote: bool = False,
    access: str = "relocation",
    weight: float = 1.0,
    distance: int = 50,
    enabled: bool = True,
) -> None:
    """Insert or update a target location."""
    conn.execute(
        """
        INSERT INTO target_locations (
            id, label, search_label, country, indeed_country,
            is_remote, access, weight, distance, enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label,
            search_label = excluded.search_label,
            country = excluded.country,
            indeed_country = excluded.indeed_country,
            is_remote = excluded.is_remote,
            access = excluded.access,
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
            access,
            weight,
            distance,
            1 if enabled else 0,
        ),
    )
    conn.commit()


def toggle_target_location(conn: sqlite3.Connection, loc_id: str, enabled: bool) -> None:
    """Enable or disable a target location."""
    conn.execute(
        "UPDATE target_locations SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, loc_id),
    )
    conn.commit()


def delete_target_location(conn: sqlite3.Connection, loc_id: str) -> None:
    """Delete a target location."""
    conn.execute("DELETE FROM target_locations WHERE id = ?", (loc_id,))
    conn.commit()
