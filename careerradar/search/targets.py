"""Configured search queries and locations, and the scrape cells they define."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from careerradar.core.paths import DB_PATH


@dataclass
class Location:
    id: str
    label: str
    search_label: str = ""
    country: str = ""
    indeed_country: str = "usa"
    is_remote: bool = False
    distance: int = 50
    enabled: bool = True

    def __post_init__(self) -> None:
        self.search_label = self.search_label or self.label
        self.is_remote = bool(self.is_remote)
        self.enabled = bool(self.enabled)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchTargets:
    """The editable search plan stored in ``search_queries`` and ``search_locations``."""

    queries: list[str]
    locations: dict[str, Location]

    @classmethod
    def from_spec(cls, data: dict[str, Any]) -> "SearchTargets":
        locations = {
            location.id: location
            for location in (Location(**spec) for spec in data.get("locations") or [])
        }
        return cls(queries=list(data.get("queries") or []), locations=locations)

    @property
    def fingerprint(self) -> str:
        """Stable identity of the active target matrix, before sources are added."""
        payload = {
            "queries": self.queries,
            "locations": [
                location.to_dict()
                for location in sorted(self.locations.values(), key=lambda location: location.id)
                if location.enabled
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def validate(self) -> list[str]:
        problems = []
        if not self.queries:
            problems.append("no search queries defined")
        for location in self.locations.values():
            if "(" in location.search_label:
                problems.append(f"location '{location.id}': search_label contains a parenthetical")
        return problems

    def cell_specs(self, sources: tuple[str, ...] = ("indeed", "linkedin")) -> list[dict[str, Any]]:
        """Build the active query × location × source scrape matrix."""
        return [
            {
                "source": source,
                "location_id": location.id,
                "query": query,
                "search_label": location.search_label,
                "country": location.country,
                "indeed_country": location.indeed_country,
                "is_remote": int(location.is_remote),
                "distance": location.distance,
            }
            for location in self.locations.values()
            if location.enabled
            for source in sources
            for query in self.queries
        ]


def get_queries(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT id, query, enabled FROM search_queries"
    if enabled_only:
        query += " WHERE enabled = 1"
    return [dict(row) for row in conn.execute(query + " ORDER BY id ASC").fetchall()]


def get_locations(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = """
        SELECT id, label, search_label, country, indeed_country, is_remote, distance, enabled
          FROM search_locations
    """
    if enabled_only:
        query += " WHERE enabled = 1"
    return [dict(row) for row in conn.execute(query + " ORDER BY id ASC").fetchall()]


def load_targets(path: str | None = None, conn: sqlite3.Connection | None = None) -> SearchTargets:
    """Load targets fresh; edits must be visible immediately to the next scrape plan."""
    if conn is not None:
        return SearchTargets(
            queries=[row["query"] for row in get_queries(conn, enabled_only=True)],
            locations={row["id"]: Location(**row) for row in get_locations(conn)},
        )

    db_path = path or DB_PATH
    if not Path(db_path).exists():
        return SearchTargets([], {})
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return load_targets(conn=db)


def add_query(conn: sqlite3.Connection, query_term: str, enabled: bool = True) -> int:
    row = conn.execute("SELECT id FROM search_queries WHERE query = ?", (query_term,)).fetchone()
    if row:
        conn.execute("UPDATE search_queries SET enabled = ? WHERE id = ?", (int(enabled), row[0]))
        conn.commit()
        return row[0]
    cursor = conn.execute(
        "INSERT INTO search_queries (query, enabled) VALUES (?, ?)", (query_term, int(enabled))
    )
    conn.commit()
    return cursor.lastrowid or 0


def update_query(
    conn: sqlite3.Connection,
    query_id: int,
    query_term: str | None = None,
    enabled: bool | None = None,
) -> None:
    updates: list[str] = []
    params: list[Any] = []
    old_term = None
    if query_term is not None:
        row = conn.execute("SELECT query FROM search_queries WHERE id = ?", (query_id,)).fetchone()
        old_term = row[0] if row else None
        updates.append("query = ?")
        params.append(query_term)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(int(enabled))
    if not updates:
        return
    conn.execute(
        f"UPDATE search_queries SET {', '.join(updates)} WHERE id = ?",
        [*params, query_id],
    )
    if old_term is not None and old_term != query_term:
        conn.execute("UPDATE scrape_cells SET query = ? WHERE query = ?", (query_term, old_term))
    conn.commit()


def toggle_query(conn: sqlite3.Connection, query_id: int, enabled: bool) -> None:
    update_query(conn, query_id, enabled=enabled)


def delete_query(conn: sqlite3.Connection, query_id: int) -> None:
    conn.execute("DELETE FROM search_queries WHERE id = ?", (query_id,))
    conn.commit()


def save_location(
    conn: sqlite3.Connection,
    loc_id: str,
    label: str,
    search_label: str | None = None,
    country: str = "US",
    indeed_country: str = "usa",
    is_remote: bool = False,
    distance: int = 50,
    enabled: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO search_locations
            (id, label, search_label, country, indeed_country, is_remote, distance, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label, search_label = excluded.search_label,
            country = excluded.country, indeed_country = excluded.indeed_country,
            is_remote = excluded.is_remote, distance = excluded.distance, enabled = excluded.enabled
        """,
        (
            loc_id,
            label,
            search_label or label,
            country,
            indeed_country,
            int(is_remote),
            distance,
            int(enabled),
        ),
    )
    conn.commit()


def toggle_location(conn: sqlite3.Connection, loc_id: str, enabled: bool) -> None:
    conn.execute("UPDATE search_locations SET enabled = ? WHERE id = ?", (int(enabled), loc_id))
    conn.commit()


def delete_location(conn: sqlite3.Connection, loc_id: str) -> None:
    conn.execute("DELETE FROM search_locations WHERE id = ?", (loc_id,))
    conn.commit()
