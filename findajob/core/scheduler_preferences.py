"""Database-backed user preferences for automatic pipeline runs.

Scheduling limits and cadence remain deployment configuration. This module stores only
the user decision to permit automatic runs, so a dashboard toggle never rewrites YAML.
"""

import sqlite3
from typing import Any


def get_scheduler_preferences(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT enabled, updated_at FROM scheduler_preferences WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"enabled": False, "updated_at": None}
    return {"enabled": bool(row[0]), "updated_at": row[1]}


def update_scheduler_preferences(conn: sqlite3.Connection, *, enabled: bool) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO scheduler_preferences (id, enabled, updated_at)
        VALUES (1, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled, updated_at = CURRENT_TIMESTAMP
        """,
        (enabled,),
    )
    conn.commit()
    return get_scheduler_preferences(conn)
