"""Role-family and seniority normalization from job titles.

`role_family` is re-derived from the title, never inherited from the search query
that surfaced the posting. Titles that match no family get `None`.
"""

import os
import re
import sqlite3
from typing import Any

from careerradar.core.paths import DB_PATH
from careerradar.taxonomy import repository as taxonomy_repo

SENIORITY_UNSPECIFIED = "unspecified"

DEFAULT_SENIORITY_PATTERNS = [
    ("intern", [r"\bintern\b", r"\binternship\b", r"\bco-?op\b", r"\btrainee\b"]),
    (
        "staff",
        [
            r"\bstaff\b",
            r"\bprincipal\b",
            r"\bdistinguished\b",
            r"\bfellow\b",
            r"\barchitect\b",
        ],
    ),
    (
        "lead",
        [
            r"\blead\b",
            r"\bleading\b",
            r"\btech lead\b",
            r"\bteam lead\b",
            r"\bhead of\b",
            r"\bdirector\b",
            r"\bmanager\b",
        ],
    ),
    (
        "senior",
        [
            r"\bsenior\b",
            r"\bsr\.?\b",
            r"\bsenior[- ]level\b",
            r"\bIII\b",
            r"\bfounding\b",
        ],
    ),
    (
        "junior",
        [
            r"\bjunior\b",
            r"\bjr\.?\b",
            r"\bentry[- ]level\b",
            r"\bgraduate\b",
            r"\bassociate\b",
        ],
    ),
    (
        "mid",
        [r"\bmid[- ]level\b", r"\bmid[- ]senior\b", r"\bII\b", r"\bintermediate\b"],
    ),
]

DEFAULT_EXCLUSIONS = [
    r"sales representative|account executive|recruiter|nurse|driver|warehouse|custodian|cashier",
]

DEFAULT_WEAK_PATTERNS = [
    re.compile(r"\bengineers?\b", re.IGNORECASE),
    re.compile(r"\bdevelopers?\b", re.IGNORECASE),
    re.compile(r"\bprogrammers?\b", re.IGNORECASE),
    re.compile(r"\bcoders?\b", re.IGNORECASE),
]

META_REGEX_TOKENS = (r"\b", "(", ")", "?", "*", "+", "|", "[", "]", "^", "$", "\\", "{", "}")


def compile_aliases(aliases: list[str]) -> list[re.Pattern[str]]:
    """Compile plain string aliases into case-insensitive, word-boundary regex patterns."""
    patterns = []
    for alias in aliases:
        if not alias or not alias.strip():
            continue
        raw = alias.strip()
        # If it's already a full regex pattern with meta-tokens, compile as-is
        if any(tok in raw for tok in META_REGEX_TOKENS):
            try:
                patterns.append(re.compile(raw, re.IGNORECASE))
                continue
            except re.error:
                pass
        # Plain string: escape, allow space/hyphen, optional trailing 's', word boundaries
        escaped = re.escape(raw).replace(r"\ ", r"[- ]")
        patterns.append(re.compile(rf"\b{escaped}s?\b", re.IGNORECASE))
    return patterns


class RoleFamily:
    __slots__ = (
        "_patterns",
        "_weak_patterns",
        "active",
        "aliases",
        "enabled",
        "key",
        "label",
        "order",
        "query_terms",
        "resume",
    )

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

        raw_aliases = spec.get("aliases") or spec.get("patterns") or []
        self.aliases: list[str] = list(raw_aliases)
        self._patterns = compile_aliases(self.aliases)
        if not self._patterns and self.label:
            self._patterns = compile_aliases([self.label])

        self._weak_patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("weak_patterns", [])]
        if not self._weak_patterns and key == "software_engineer":
            self._weak_patterns = list(DEFAULT_WEAK_PATTERNS)

    def find(self, title: str, allow_weak: bool = True) -> tuple[int | None, bool]:
        """Earliest match offset in `title`, or None."""
        best: int | None = None
        for pattern in self._patterns:
            match = pattern.search(title)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            return best, False

        if not allow_weak:
            return None, False

        for pattern in self._weak_patterns:
            match = pattern.search(title)
            if match and (best is None or match.start() < best):
                best = match.start()
        return (best, True) if best is not None else (None, False)

    def __repr__(self) -> str:
        return f"<RoleFamily {self.key}>"


ACCESS_COMMUTABLE = "commutable"
ACCESS_REMOTE = "remote"
ACCESS_RELOCATION = "relocation"
ACCESS_LEVELS = (ACCESS_COMMUTABLE, ACCESS_REMOTE, ACCESS_RELOCATION)


class Location:
    __slots__ = (
        "access",
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
        self.country: str = spec["country"]
        self.is_remote = bool(spec.get("is_remote", False))
        self.weight = float(spec.get("weight", 1.0))
        self.indeed_country: str = spec.get("indeed_country", "usa")
        self.distance: int = spec.get("distance", 50)
        self.access: str = spec.get(
            "access", ACCESS_REMOTE if self.is_remote else ACCESS_RELOCATION
        )
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
            "access": self.access,
            "enabled": self.enabled,
        }


class RoleTaxonomy:
    def __init__(
        self,
        db_path: str | None = None,
        conn: sqlite3.Connection | None = None,
        spec_dict: dict[str, Any] | None = None,
        commutable_area: dict[str, Any] | None = None,
        exclusions: list[str] | None = None,
    ) -> None:
        self.families: dict[str, RoleFamily] = {}
        self.locations: dict[str, Location] = {}
        self._seniority: list[tuple[str, list[re.Pattern[str]]]] = []
        self._seniority_by_level: dict[str, list[re.Pattern[str]]] = {}
        self.commutable_region: str | None = None
        self._commutable_cities: set[str] = set()
        self._commutable_patterns: list[re.Pattern[str]] = []
        self.version = 1
        self.hash = "db_taxonomy"

        for level, patterns in DEFAULT_SENIORITY_PATTERNS:
            compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
            self._seniority.append((level, compiled))
            self._seniority_by_level[level] = compiled

        raw_exclusions = (
            exclusions
            if exclusions is not None
            else (spec_dict.get("exclusions") if spec_dict else DEFAULT_EXCLUSIONS)
        )
        self._exclusions: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in (raw_exclusions or DEFAULT_EXCLUSIONS)
        ]

        if spec_dict is not None:
            self._load_from_dict(spec_dict)
            self._init_commutable_area(commutable_area or spec_dict.get("commutable_area"))
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

        if target_conn is not None:
            try:
                db_roles = taxonomy_repo.get_target_roles(target_conn)
                if db_roles:
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
                            "aliases": r.get("aliases") or [],
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
                if close_conn and target_conn is not None:
                    target_conn.close()

        self._init_commutable_area(commutable_area)

    def _init_commutable_area(self, commutable_area_spec: dict[str, Any] | None = None) -> None:
        """Derive commutable area matching from configured commutable locations or explicit spec."""
        if commutable_area_spec:
            self.commutable_region = (
                commutable_area_spec.get("region") or ""
            ).strip().upper() or None
            self._commutable_cities = {
                c.strip().lower() for c in (commutable_area_spec.get("cities") or []) if c
            }
            self._commutable_patterns = [
                re.compile(p, re.IGNORECASE) for p in (commutable_area_spec.get("patterns") or [])
            ]
            return

        self._commutable_cities = set()
        self._commutable_patterns = []
        for loc in self.locations.values():
            if loc.access == ACCESS_COMMUTABLE:
                label_parts = [p.strip() for p in loc.label.split(",") if p.strip()]
                if label_parts:
                    city_name = label_parts[0].lower()
                    self._commutable_cities.add(city_name)
                    self._commutable_patterns.append(
                        re.compile(rf"\b{re.escape(city_name)}\b", re.IGNORECASE)
                    )
                    has_state = len(label_parts) > 1 and len(label_parts[1]) == 2
                    if has_state and not self.commutable_region:
                        self.commutable_region = label_parts[1].upper()

                search_city = loc.search_label.split(",")[0].strip().lower()
                if search_city:
                    self._commutable_cities.add(search_city)
                    self._commutable_patterns.append(
                        re.compile(rf"\b{re.escape(search_city)}\b", re.IGNORECASE)
                    )

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

    def label(self, key: str | None) -> str:
        family = self.families.get(key) if key else None
        return family.label if family else (key or "Unclassified")

    def is_active(self, key: str | None) -> bool:
        family = self.families.get(key) if key else None
        return bool(family and family.active)

    def active_families(self) -> list[RoleFamily]:
        return [f for f in self.families.values() if f.active]

    def resume_for(self, key: str | None) -> str | None:
        family = self.families.get(key) if key else None
        return family.resume if family else None

    # -- normalization -----------------------------------------------------------------
    def classify(
        self,
        title: str | None,
        has_tech_skills: bool = False,
    ) -> tuple[str | None, str]:
        """Map a raw job title onto (role_family, seniority)."""
        if not title:
            return None, SENIORITY_UNSPECIFIED

        normalized = re.sub(r"\s+", " ", title).strip()

        candidates: list[tuple[int, int, int, str]] = []
        for family in self.families.values():
            position, is_weak = family.find(normalized)
            if position is None:
                continue
            if is_weak and not has_tech_skills:
                continue
            candidates.append((1 if is_weak else 0, position, family.order, family.key))

        if not candidates:
            return None, self.seniority(normalized)

        best = min(candidates)
        if best[0] == 1 and self.is_excluded(normalized):
            return None, self.seniority(normalized)

        return best[3], self.seniority(normalized)

    def is_commutable(
        self,
        city: str | None = None,
        region: str | None = None,
        location_text: str | None = None,
    ) -> bool:
        city_key = (city or "").strip().lower()
        if city_key and city_key in self._commutable_cities:
            region_key = (region or "").strip().upper()
            if not region_key or not self.commutable_region:
                return True
            return region_key == self.commutable_region

        haystack = " ".join(filter(None, [city, region, location_text]))
        return any(p.search(haystack) for p in self._commutable_patterns)

    def classify_access(
        self,
        city: str | None = None,
        region: str | None = None,
        location_text: str | None = None,
        is_remote: bool | None = None,
    ) -> str:
        if self.is_commutable(city, region, location_text):
            return ACCESS_COMMUTABLE
        if is_remote:
            return ACCESS_REMOTE
        return ACCESS_RELOCATION

    def is_excluded(self, title: str | None) -> bool:
        return any(pattern.search(title or "") for pattern in self._exclusions)

    def seniority(self, title: str | None) -> str:
        if not title:
            return SENIORITY_UNSPECIFIED
        if re.search(r"member\s+(?:of\s+)?(?:the\s+)?technical\s+staff", title, re.IGNORECASE):
            return SENIORITY_UNSPECIFIED
        junior_pos = self._earliest_match(self._seniority_by_level.get("junior", []), title)
        senior_pos = self._earliest_match(self._seniority_by_level.get("senior", []), title)
        if junior_pos is not None and senior_pos is not None and junior_pos < senior_pos:
            return "junior"
        for level, patterns in self._seniority:
            for pattern in patterns:
                if pattern.search(title):
                    return level
        return SENIORITY_UNSPECIFIED

    @staticmethod
    def _earliest_match(patterns: list[re.Pattern[str]], title: str) -> int | None:
        best: int | None = None
        for pattern in patterns:
            match = pattern.search(title)
            if match and (best is None or match.start() < best):
                best = match.start()
        return best

    def classify_all(self, title: str | None, has_tech_skills: bool = False) -> list[str]:
        if not title:
            return []
        normalized = re.sub(r"\s+", " ", title).strip()
        if self.is_excluded(normalized):
            return []
        hits: list[tuple[int, int, int, str]] = []
        for family in self.families.values():
            position, is_weak = family.find(normalized)
            if position is None or (is_weak and not has_tech_skills):
                continue
            hits.append((1 if is_weak else 0, position, family.order, family.key))
        return [key for _, _, _, key in sorted(hits)]

    # -- validation --------------------------------------------------------------------
    def validate(self) -> list[str]:
        problems = []
        if not self.families:
            problems.append("no families defined")

        for key, family in self.families.items():
            if not family._patterns:
                problems.append(f"family '{key}': no patterns or aliases")

        for location in self.locations.values():
            if location.access not in ACCESS_LEVELS:
                problems.append(
                    f"location '{location.id}': unknown access '{location.access}' "
                    f"(expected one of {ACCESS_LEVELS})"
                )
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
            queries = family.query_terms
            if not queries:
                continue
            for location in enabled_locs:
                for source in sources:
                    for query in queries:
                        specs.append(
                            {
                                "source": source,
                                "role_family": family.key,
                                "location_id": location.id,
                                "query": query,
                                "tier": 1,
                                "active": True,
                            }
                        )
        return specs


_CACHE: dict[tuple[str, float], "RoleTaxonomy"] = {}


def load_roles(
    path: str | None = None,
    conn: sqlite3.Connection | None = None,
    commutable_area: dict[str, Any] | None = None,
    exclusions: list[str] | None = None,
) -> "RoleTaxonomy":
    resolved = path or DB_PATH
    try:
        stamp = os.path.getmtime(resolved)
    except OSError:
        stamp = 0
    cache_key = (resolved, stamp)
    if (
        cache_key not in _CACHE
        or conn is not None
        or commutable_area is not None
        or exclusions is not None
    ):
        taxonomy = RoleTaxonomy(
            db_path=resolved,
            conn=conn,
            commutable_area=commutable_area,
            exclusions=exclusions,
        )
        if conn is None and commutable_area is None and exclusions is None:
            _CACHE.clear()
            _CACHE[cache_key] = taxonomy
        return taxonomy
    return _CACHE[cache_key]
