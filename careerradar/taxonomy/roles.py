"""Search targets: the roles and locations that define the scrape matrix.

Loaded from the `target_roles`, `target_queries`, and `target_locations` tables. This
module carries no title classification -- a posting's provenance is the cell that found
it, never something re-derived from the title.
"""

import os
import sqlite3
from typing import Any

from careerradar.core.paths import DB_PATH
from careerradar.taxonomy import repository as taxonomy_repo


class RoleFamily:
    __slots__ = ("active", "enabled", "key", "label", "order", "query_terms", "resume")

    def __init__(
        self,
        key: str,
        spec: dict[str, Any],
        order: int,
    ) -> None:
        self.key = key
        self.label = spec.get("label", key.replace("_", " ").title())
        self.resume: str | None = spec.get("resume")
        self.enabled: bool = bool(spec.get("enabled", spec.get("active", True)))
        self.active: bool = self.enabled
        self.order = order

        raw_queries = spec.get("query_terms")
        self.query_terms: list[str] = [self.label] if raw_queries is None else list(raw_queries)

    def __repr__(self) -> str:
        return f"<RoleFamily {self.key}>"


class Location:
    __slots__ = (
        "country",
        "distance",
        "enabled",
        "id",
        "indeed_country",
        "is_remote",
        "label",
        "search_label",
        "weight",
    )

    def __init__(self, spec: dict[str, Any]) -> None:
        self.id: str = spec["id"]
        self.label: str = spec["label"]
        self.search_label: str = spec.get("search_label", spec["label"])
        self.country: str = spec.get("country", "")
        self.is_remote = bool(spec.get("is_remote", False))
        self.weight = float(spec.get("weight", 1.0))
        self.indeed_country: str = spec.get("indeed_country", "usa")
        self.distance: int = spec.get("distance", 50)
        self.enabled: bool = bool(spec.get("enabled", True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "search_label": self.search_label,
            "country": self.country,
            "is_remote": self.is_remote,
            "weight": self.weight,
            "indeed_country": self.indeed_country,
            "distance": self.distance,
            "enabled": self.enabled,
        }


class RoleTaxonomy:
    def __init__(
        self,
        db_path: str | None = None,
        conn: sqlite3.Connection | None = None,
        spec_dict: dict[str, Any] | None = None,
    ) -> None:
        self.families: dict[str, RoleFamily] = {}
        self.locations: dict[str, Location] = {}
        self.version = 1
        self.hash = "db_taxonomy"

        if spec_dict is not None:
            self._load_from_dict(spec_dict)
            return

        # Attempt to load from SQLite target tables
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
            db_roles = taxonomy_repo.get_target_roles(target_conn)
            for order, r in enumerate(db_roles):
                key = r["key"]
                queries = [
                    q["query"]
                    for q in taxonomy_repo.get_target_queries(
                        target_conn, role_key=key, enabled_only=True
                    )
                ]
                spec = {
                    "label": r["label"],
                    "resume": r.get("resume"),
                    "query_terms": queries,
                    "enabled": bool(r.get("enabled", 1)),
                }
                self.families[key] = RoleFamily(key, spec, order)

            db_locs = taxonomy_repo.get_target_locations(target_conn)
            for loc in db_locs:
                self.locations[loc["id"]] = Location(loc)
        except sqlite3.Error:
            pass
        finally:
            if close_conn:
                target_conn.close()

    def _load_from_dict(self, data: dict[str, Any]) -> None:
        self.version = data.get("version", 1)
        for order, (key, spec) in enumerate((data.get("families") or {}).items()):
            self.families[key] = RoleFamily(key, spec or {}, order)
        for spec in data.get("locations") or []:
            location = Location(spec)
            self.locations[location.id] = location

    # -- lookup ------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.families)

    def __contains__(self, key: str) -> bool:
        return key in self.families

    def get(self, key: str, default: RoleFamily | None = None) -> RoleFamily | None:
        return self.families.get(key, default)

    def is_active(self, key: str | None) -> bool:
        family = self.families.get(key) if key else None
        return bool(family and family.active)

    def active_families(self) -> list[RoleFamily]:
        return [f for f in self.families.values() if f.active]

    def resume_for(self, key: str | None) -> str | None:
        family = self.families.get(key) if key else None
        return family.resume if family else None

    # -- validation --------------------------------------------------------------------
    def validate(self) -> list[str]:
        problems = []
        if not self.families:
            problems.append("no families defined")

        for location in self.locations.values():
            if "(" in location.search_label:
                problems.append(f"location '{location.id}': search_label contains a parenthetical")
        return problems

    # -- scrape planning ---------------------------------------------------------------
    def cell_specs(
        self,
        sources: tuple[str, ...] = ("indeed", "linkedin"),
    ) -> list[dict[str, Any]]:
        """Enumerate active search cells: all enabled queries across all enabled locations."""
        specs = []
        enabled_locs = [loc for loc in self.locations.values() if loc.enabled]
        for family in self.families.values():
            if not family.enabled:
                continue
            for location in enabled_locs:
                for source in sources:
                    for query in family.query_terms:
                        specs.append(
                            {
                                "source": source,
                                "role_family": family.key,
                                "location_id": location.id,
                                "query": query,
                                "active": True,
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
