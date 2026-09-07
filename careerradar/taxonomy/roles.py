"""Search targets: the queries and locations that define the scrape matrix.

Loaded from the `search_queries` and `search_locations` tables. This module carries no
title classification -- a posting's provenance is the cell that found it, never something
re-derived from the title.
"""

import os
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from careerradar.core.paths import DB_PATH
from careerradar.taxonomy import repository as taxonomy_repo


@dataclass
class Location:
    id: str
    label: str
    search_label: str = ""
    country: str = ""
    indeed_country: str = "usa"
    is_remote: bool = False
    weight: float = 1.0
    distance: int = 50
    enabled: bool = True

    def __post_init__(self) -> None:
        self.search_label = self.search_label or self.label
        self.is_remote = bool(self.is_remote)
        self.enabled = bool(self.enabled)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RoleTaxonomy:
    def __init__(
        self,
        db_path: str | None = None,
        conn: sqlite3.Connection | None = None,
        spec_dict: dict[str, Any] | None = None,
    ) -> None:
        self.queries: list[str] = []
        self.locations: dict[str, Location] = {}
        self.hash = "db_taxonomy"

        if spec_dict is not None:
            self._load_from_dict(spec_dict)
            return

        target_conn = conn
        close_conn = False
        if target_conn is None:
            resolved_db = db_path or DB_PATH
            if os.path.exists(resolved_db):
                try:
                    target_conn = sqlite3.connect(resolved_db)
                    target_conn.row_factory = sqlite3.Row
                    close_conn = True
                except sqlite3.Error:
                    target_conn = None

        if target_conn is None:
            return

        try:
            self.queries = [
                q["query"] for q in taxonomy_repo.get_queries(target_conn, enabled_only=True)
            ]
            for loc in taxonomy_repo.get_locations(target_conn):
                self.locations[loc["id"]] = Location(**loc)
        except sqlite3.Error:
            pass
        finally:
            if close_conn:
                target_conn.close()

    def _load_from_dict(self, data: dict[str, Any]) -> None:
        self.queries = list(data.get("queries") or [])
        for spec in data.get("locations") or []:
            location = Location(**spec)
            self.locations[location.id] = location

    # -- validation --------------------------------------------------------------------
    def validate(self) -> list[str]:
        problems = []
        if not self.queries:
            problems.append("no search queries defined")

        for location in self.locations.values():
            if "(" in location.search_label:
                problems.append(f"location '{location.id}': search_label contains a parenthetical")
        return problems

    # -- scrape planning ---------------------------------------------------------------
    def cell_specs(
        self,
        sources: tuple[str, ...] = ("indeed", "linkedin"),
    ) -> list[dict[str, Any]]:
        """Enumerate active search cells: every enabled query across every enabled location.

        Each spec carries the whole JobSpy call, so `seed_cells` can write a cell that
        needs no join at task-build time.
        """
        specs = []
        for location in self.locations.values():
            if not location.enabled:
                continue
            for source in sources:
                for query in self.queries:
                    specs.append(
                        {
                            "source": source,
                            "location_id": location.id,
                            "query": query,
                            "search_label": location.search_label,
                            "country": location.country,
                            "indeed_country": location.indeed_country,
                            "is_remote": 1 if location.is_remote else 0,
                            "distance": location.distance,
                            "weight": location.weight,
                        }
                    )
        return specs


_CACHE: dict[tuple[str, float], "RoleTaxonomy"] = {}


def load_roles(
    path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> "RoleTaxonomy":
    resolved = path or DB_PATH
    try:
        stamp = os.path.getmtime(resolved)
    except OSError:
        stamp = 0
    cache_key = (resolved, stamp)
    if cache_key not in _CACHE or conn is not None:
        taxonomy = RoleTaxonomy(db_path=resolved, conn=conn)
        if conn is None:
            _CACHE.clear()
            _CACHE[cache_key] = taxonomy
        return taxonomy
    return _CACHE[cache_key]
