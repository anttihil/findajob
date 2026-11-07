"""Configured search queries and locations, and the scrape cells they define."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from findajob.core.paths import DB_PATH
from findajob.core.sources import DEFAULT_QUERY_SOURCES, SCHEDULED_SOURCES


@dataclass
class Location:
    id: str
    label: str
    search_label: str = ""
    country: str = ""
    indeed_country: str = "usa"
    is_remote: bool = False
    distance: int = 50
    last_used_at: str | None = None

    def __post_init__(self) -> None:
        self.search_label = self.search_label or self.label
        self.is_remote = bool(self.is_remote)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchTargets:
    """The editable search plan stored in ``search_queries`` and ``search_locations``."""

    queries: list[str]
    locations: dict[str, Location]
    query_sources: dict[str, tuple[str, ...]] | None = None
    query_locations: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def from_spec(cls, data: dict[str, Any]) -> "SearchTargets":
        locations = {
            location.id: location
            for location in (Location(**spec) for spec in data.get("locations") or [])
        }
        queries = list(data.get("queries") or [])
        return cls(
            queries=queries,
            locations=locations,
            query_sources=data.get("query_sources")
            or dict.fromkeys(queries, DEFAULT_QUERY_SOURCES),
            query_locations=data.get("query_locations", dict.fromkeys(queries, tuple(locations))),
        )

    @property
    def fingerprint(self) -> str:
        """Stable identity of assigned search parameters, excluding picker recency."""
        used_ids = {loc_id for query in self.queries for loc_id in self.locations_for(query)}
        payload = {
            "queries": [
                {
                    "query": query,
                    "sources": sorted(self.sources_for(query)),
                    "location_ids": sorted(self.locations_for(query)),
                }
                for query in self.queries
            ],
            "locations": [
                {key: value for key, value in location.to_dict().items() if key != "last_used_at"}
                for location in sorted(self.locations.values(), key=lambda location: location.id)
                if location.id in used_ids
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def validate(self) -> list[str]:
        problems = []
        used_ids = {loc_id for query in self.queries for loc_id in self.locations_for(query)}
        for location in self.locations.values():
            if location.id in used_ids and "(" in location.search_label:
                problems.append(f"location '{location.id}': search_label contains a parenthetical")
        return problems

    def sources_for(self, query: str) -> tuple[str, ...]:
        """Return the board scope; legacy plans default to Indeed and LinkedIn."""
        return (self.query_sources or {}).get(query, DEFAULT_QUERY_SOURCES)

    def locations_for(self, query: str) -> tuple[str, ...]:
        """Only assigned locations are scheduled; saved choices alone create no cells."""
        return (self.query_locations or {}).get(query, ())

    @property
    def search_pairs(self) -> set[tuple[str, str]]:
        return {
            (query, loc_id)
            for query in self.queries
            for loc_id in self.locations_for(query)
            if loc_id in self.locations
        }

    def cell_specs(self, sources: tuple[str, ...] = SCHEDULED_SOURCES) -> list[dict[str, Any]]:
        """Build cells for assigned query/location pairs on enabled, mapped sources."""
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
            for query in self.queries
            if location.id in self.locations_for(query)
            for source in sources
            if source in self.sources_for(query)
        ]


def get_queries(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = """
        SELECT sq.id, sq.query, sq.enabled,
               (SELECT json_group_array(source) FROM search_query_sources
                 WHERE query_id = sq.id) AS sources_json,
               (SELECT json_group_array(location_id) FROM search_query_locations
                 WHERE query_id = sq.id) AS locations_json
          FROM search_queries sq
    """
    if enabled_only:
        query += " WHERE sq.enabled = 1"
    records = []
    for row in conn.execute(query + " ORDER BY sq.id ASC").fetchall():
        record = dict(row)
        record["sources"] = sorted(json.loads(record.pop("sources_json")))
        record["location_ids"] = sorted(json.loads(record.pop("locations_json")))
        records.append(record)
    return records


def get_locations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute("""
        SELECT id, label, search_label, country, indeed_country, is_remote, distance, last_used_at
          FROM search_locations ORDER BY last_used_at DESC, label COLLATE NOCASE, id
    """).fetchall()
    ]


def load_targets(path: str | None = None, conn: sqlite3.Connection | None = None) -> SearchTargets:
    """Load targets fresh; edits must be visible immediately to the next scrape plan."""
    if conn is not None:
        queries = get_queries(conn, enabled_only=True)
        return SearchTargets(
            queries=[row["query"] for row in queries],
            locations={row["id"]: Location(**row) for row in get_locations(conn)},
            query_sources={row["query"]: tuple(row["sources"]) for row in queries},
            query_locations={row["query"]: tuple(row["location_ids"]) for row in queries},
        )

    db_path = path or DB_PATH
    if not Path(db_path).exists():
        return SearchTargets([], {})
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return load_targets(conn=db)


def _validate_sources(sources: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    requested = DEFAULT_QUERY_SOURCES if sources is None else tuple(dict.fromkeys(sources))
    invalid = set(requested) - set(SCHEDULED_SOURCES)
    if invalid:
        raise ValueError(f"Unsupported scheduled source(s): {', '.join(sorted(invalid))}")
    if not requested:
        raise ValueError("A scheduled query must target at least one source")
    return requested


def _save_query_sources(conn: sqlite3.Connection, query_id: int, sources: tuple[str, ...]) -> None:
    conn.execute("DELETE FROM search_query_sources WHERE query_id = ?", (query_id,))
    conn.executemany(
        "INSERT INTO search_query_sources (query_id, source) VALUES (?, ?)",
        [(query_id, source) for source in sources],
    )


def _validate_locations(
    conn: sqlite3.Connection,
    location_ids: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(location_ids or ()))
    if not requested:
        raise ValueError("Choose at least one location for the query")
    known = {row["id"] for row in get_locations(conn)}
    invalid = set(requested) - known
    if invalid:
        raise ValueError(f"Unknown location(s): {', '.join(sorted(invalid))}")
    return requested


def _save_query_locations(
    conn: sqlite3.Connection,
    query_id: int,
    location_ids: tuple[str, ...],
) -> None:
    from datetime import datetime, timezone

    conn.execute("DELETE FROM search_query_locations WHERE query_id = ?", (query_id,))
    conn.executemany(
        "INSERT INTO search_query_locations (query_id, location_id) VALUES (?, ?)",
        [(query_id, loc_id) for loc_id in location_ids],
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE search_locations SET last_used_at = ? WHERE id = ?",
        [(now, loc_id) for loc_id in location_ids],
    )


def add_query(
    conn: sqlite3.Connection,
    query_term: str,
    enabled: bool = True,
    sources: list[str] | tuple[str, ...] | None = None,
    location_ids: list[str] | tuple[str, ...] | None = None,
) -> int:
    sources = _validate_sources(sources)
    locations = _validate_locations(conn, location_ids)
    with conn:
        row = conn.execute(
            "SELECT id FROM search_queries WHERE query = ?", (query_term,)
        ).fetchone()
        if row:
            query_id = row[0]
            conn.execute(
                "UPDATE search_queries SET enabled = ? WHERE id = ?", (int(enabled), query_id)
            )
        else:
            cursor = conn.execute(
                "INSERT INTO search_queries (query, enabled) VALUES (?, ?)",
                (query_term, int(enabled)),
            )
            query_id = cursor.lastrowid or 0
        _save_query_sources(conn, query_id, sources)
        _save_query_locations(conn, query_id, locations)
    return query_id


def update_query(
    conn: sqlite3.Connection,
    query_id: int,
    query_term: str | None = None,
    enabled: bool | None = None,
    sources: list[str] | tuple[str, ...] | None = None,
    location_ids: list[str] | tuple[str, ...] | None = None,
) -> None:
    validated_sources = _validate_sources(sources) if sources is not None else None
    locations = _validate_locations(conn, location_ids) if location_ids is not None else None
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
    if not updates and sources is None and locations is None:
        return
    with conn:
        if updates:
            conn.execute(
                f"UPDATE search_queries SET {', '.join(updates)} WHERE id = ?",
                [*params, query_id],
            )
        if old_term is not None and old_term != query_term:
            conn.execute(
                "UPDATE scrape_cells SET query = ? WHERE query = ?", (query_term, old_term)
            )
        if validated_sources is not None:
            _save_query_sources(conn, query_id, validated_sources)
        if locations is not None:
            _save_query_locations(conn, query_id, locations)


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
) -> None:
    conn.execute(
        """
        INSERT INTO search_locations
            (id, label, search_label, country, indeed_country, is_remote, distance)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label, search_label = excluded.search_label,
            country = excluded.country, indeed_country = excluded.indeed_country,
            is_remote = excluded.is_remote, distance = excluded.distance
        """,
        (
            loc_id,
            label,
            search_label or label,
            country,
            indeed_country,
            int(is_remote),
            distance,
        ),
    )
    conn.commit()


def delete_location(conn: sqlite3.Connection, loc_id: str) -> None:
    conn.execute("DELETE FROM search_locations WHERE id = ?", (loc_id,))
    conn.commit()
