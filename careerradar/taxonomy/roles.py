"""Role-family and seniority normalization from job titles.

`role_family` is always re-derived from the title, never inherited from the search query
that surfaced the posting. A live "Platform Engineer" query on Indeed returned a
"Maintenance Technician", so the query predicts very little about what came back -- and
counting those returns toward platform-engineer supply would inflate it.

Titles that match no family get `None`. Those postings are stored and scored like any
other -- `title_family_fit` scores 0.0 for `role_family=None` (keyword_score.py), so they
are demoted, not dropped -- but excluded from every role-family statistic; the real corpus
already contains "Full-time", "Remote (US, Canada)", "Toronto, ON", and "YC 19" as titles.
"""

import hashlib
import os
import re
from typing import Any

import yaml

from careerradar.core.paths import DATA_DIR

ROLES_PATH = os.path.join(DATA_DIR, "roles.yaml")

SENIORITY_UNSPECIFIED = "unspecified"


class RoleFamily:
    __slots__ = (
        "_patterns",
        "_weak_patterns",
        "key",
        "label",
        "order",
        "query_terms",
        "resume",
        "tier",
    )

    def __init__(self, key: str, spec: dict[str, Any], order: int) -> None:
        self.key = key
        self.label = spec.get("label", key.replace("_", " ").title())
        self.tier: str = spec.get("tier", "breadth")
        self.resume: str | None = spec.get("resume")
        # An explicit empty list means "classify titles into this family, but do not search
        # for it". role_family is derived from the title, not from the query that returned
        # the posting, so a family with no queries still accounts for supply that other
        # families' queries drag in. Omitting the key entirely still defaults to the label.
        raw_queries = spec.get("query_terms")
        self.query_terms: list[str] = [self.label] if raw_queries is None else list(raw_queries)
        # Declaration order doubles as a specificity ranking, used to break position ties.
        self.order = order
        self._patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])]
        # Weak patterns need corroboration from a technical skill in the posting. Bare
        # "X Engineer" titles are otherwise claimed by the generic family regardless of
        # whether the job has anything to do with software.
        self._weak_patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("weak_patterns", [])]

    def find(self, title: str, allow_weak: bool = True) -> tuple[int | None, bool]:
        """Earliest match offset in `title`, or None.

        Returns (offset, is_weak). A weak-only match is reported so the caller can require
        technical corroboration before accepting it.
        """
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


# How reachable a role is without moving house. This is the distinction that actually
# governs whether a posting is worth reading.
ACCESS_COMMUTABLE = "commutable"
ACCESS_REMOTE = "remote"
ACCESS_RELOCATION = "relocation"
ACCESS_LEVELS = (ACCESS_COMMUTABLE, ACCESS_REMOTE, ACCESS_RELOCATION)


class Location:
    __slots__ = (
        "access",
        "country",
        "distance",
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
        # What the board is actually asked for. `label` is a display string and the two are
        # NOT interchangeable: Indeed returns 0 rows for "United States (onsite,
        # nationwide)" and 10 for "United States". A label used as a query fails silently --
        # the cell records status='empty', which analytics read as a genuine "none
        # observed" rather than as a broken search.
        self.search_label: str = spec.get("search_label", spec["label"])
        self.country: str = spec["country"]
        self.is_remote = bool(spec.get("is_remote", False))
        self.weight = float(spec.get("weight", 1.0))
        self.indeed_country: str = spec.get("indeed_country", "usa")
        self.distance: int = spec.get("distance", 50)
        self.access: str = spec.get(
            "access", ACCESS_REMOTE if self.is_remote else ACCESS_RELOCATION
        )

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
        }


class RoleTaxonomy:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or ROLES_PATH
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        data: dict[str, Any] = yaml.safe_load(raw)

        self.version = data.get("version", 0)
        self.families: dict[str, RoleFamily] = {}
        for order, (key, spec) in enumerate((data.get("families") or {}).items()):
            self.families[key] = RoleFamily(key, spec or {}, order)

        self._seniority: list[tuple[str, list[re.Pattern[str]]]] = []
        for level, spec in (data.get("seniority") or {}).items():
            patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])]
            self._seniority.append((level, patterns))
        self._seniority_by_level = dict(self._seniority)

        self.locations: dict[str, Location] = {}
        for spec in data.get("locations") or []:
            location = Location(spec)
            self.locations[location.id] = location

        self.tier_locations: dict[str, list[str]] = data.get("tier_locations") or {}
        self._exclusions = [re.compile(p, re.IGNORECASE) for p in (data.get("exclusions") or [])]

        area = data.get("commutable_area") or {}
        self.commutable_region = (area.get("region") or "").strip().upper()
        self._commutable_cities = {c.strip().lower() for c in (area.get("cities") or []) if c}
        self._commutable_patterns = [
            re.compile(p, re.IGNORECASE) for p in (area.get("patterns") or [])
        ]
        self.hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

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

    def tier(self, key: str | None) -> str | None:
        family = self.families.get(key) if key else None
        return family.tier if family else None

    def resume_for(self, key: str | None) -> str | None:
        family = self.families.get(key) if key else None
        return family.resume if family else None

    def by_tier(self, tier: str) -> list[RoleFamily]:
        return [f for f in self.families.values() if f.tier == tier]

    # -- normalization -----------------------------------------------------------------
    def classify(self, title: str | None, has_tech_skills: bool = False) -> tuple[str | None, str]:
        """Map a raw job title onto (role_family, seniority).

        Earliest match position wins, since titles lead with the primary role:
        "Senior Backend Developer / DevOps Engineer" is a backend role that also does
        devops, not the other way round. Declaration order breaks positional ties, so
        specific families beat the generic software_engineer catch-all at the same offset.

        `has_tech_skills` gates weak patterns. Bare "X Engineer" is only a software role if
        the posting actually mentions a technology -- otherwise a "Stationary Engineer" or
        "R&D Engineer, Materials" is counted as software supply and handed a perfect
        title-family score.
        """
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
            # Sort key: confident matches before weak ones, THEN by position. A weak generic
            # match must never outrank a confident specific one just for appearing earlier
            # in the string -- "Observability Engineer / Site Reliability Engineer" was
            # being classified as generic software because the bare "Engineer" in
            # "Observability Engineer" sits at a lower offset than "site reliability".
            candidates.append((1 if is_weak else 0, position, family.order, family.key))

        if not candidates:
            return None, self.seniority(normalized)

        # An exclusion only vetoes a WEAK match. Applying it unconditionally threw away real
        # postings: "Sr. Java Backend / Sr. React Frontend / Sr. Network Engineer / Sr. Cloud
        # Infrastructure Engineer" is a software job that happens to mention a network role,
        # and "Principal Software Engineer, Senior Java Engineer - Cloud, Account Executive"
        # was vetoed by the account-executive pattern. A confident software pattern is
        # stronger evidence than the presence of a non-software phrase.
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
        """Whether a posting sits within commuting distance of home.

        Judged from the posting's own city, not from which search surfaced it: a nationwide
        or remote-flagged search routinely returns roles that happen to sit in the LA basin,
        and those are the most actionable results in the whole corpus.
        """
        city_key = (city or "").strip().lower()
        if city_key and city_key in self._commutable_cities:
            # Guard against same-named cities in other states (Glendale AZ, Pasadena TX).
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
        """Posting-level access: commutable beats remote beats relocation.

        Commutable is checked first on purpose -- a remote-friendly role down the road is
        strictly better than one that is merely remote.
        """
        if self.is_commutable(city, region, location_text):
            return ACCESS_COMMUTABLE
        if is_remote:
            return ACCESS_REMOTE
        return ACCESS_RELOCATION

    def is_excluded(self, title: str | None) -> bool:
        """Whether a title looks like a non-software engineering or non-technical role.

        Hardware, RF, manufacturing, and facilities postings do mention Python and Linux, so
        the technical-skill gate alone does not exclude them -- and left unchecked they are
        absorbed by the generic family and inflate its supply figure.
        """
        return any(pattern.search(title or "") for pattern in self._exclusions)

    def seniority(self, title: str | None) -> str:
        if not title:
            return SENIORITY_UNSPECIFIED
        # "Member of Technical Staff" is an INDIVIDUAL-CONTRIBUTOR title at AI labs, not a
        # staff-level one, but `\bstaff\b` claims it. Measured on prod_jobs.db 2026-08-16 it
        # is 26 scored / 3 hits / avg fit 29.6, against 928 scored / 1 hit / avg 11.1 for
        # genuine staff titles -- so mislabelling it costs real hits once scoring skips
        # staff (config.yaml scoring.skip_seniority). Checked before the pattern loop
        # because `staff` would otherwise win on position.
        if re.search(r"member\s+(?:of\s+)?(?:the\s+)?technical\s+staff", title, re.IGNORECASE):
            return SENIORITY_UNSPECIFIED
        # senior is checked ahead of junior below (roles.yaml:46-58) so "Senior Associate"
        # resolves senior, not junior via the `associate` pattern. That reorder would also
        # flip a genuine open range like "Junior to Senior Engineer" to senior, so guard it
        # here first: if junior's earliest match precedes senior's, the title reads as a
        # range and the honest reading of an open range is still junior.
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
        """Every family a title touches, ordered by match position.

        Multi-role titles are common ("Sr. Java Backend / Sr. React Frontend / Sr. Network
        Engineer"). `classify` returns only the primary; this exposes the rest for display
        without letting one posting count toward several families' supply.
        """
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
    def validate(self):
        problems = []
        known_locations = set(self.locations)

        if not self.families:
            problems.append("no families defined")

        keys = list(self.families)
        if keys and keys[-1] != "software_engineer":
            problems.append(
                "software_engineer must be declared last so specific families win "
                f"positional ties (currently last: '{keys[-1]}')"
            )

        for key, family in self.families.items():
            if not family._patterns:
                problems.append(f"family '{key}': no patterns")
            if family.tier not in ("core", "adjacent", "breadth"):
                problems.append(f"family '{key}': unknown tier '{family.tier}'")

        for location in self.locations.values():
            if location.access not in ACCESS_LEVELS:
                problems.append(
                    f"location '{location.id}': unknown access '{location.access}' "
                    f"(expected one of {ACCESS_LEVELS})"
                )
            if len(location.id) <= 2 and location.id not in ("no",):
                problems.append(
                    f"location id '{location.id}' is ambiguously short — "
                    f"two-letter ids read as US state codes"
                )
            # A qualifier in the string sent to the board matches nothing and the cell
            # records status='empty', which analytics read as real "none observed" demand.
            # Cheap to assert, invisible otherwise.
            if "(" in location.search_label:
                problems.append(
                    f"location '{location.id}': search_label "
                    f"{location.search_label!r} contains a parenthetical — boards match it "
                    f"literally and return nothing. Set search_label to the bare place name."
                )

        for tier, location_ids in self.tier_locations.items():
            if tier not in ("core", "adjacent", "breadth"):
                problems.append(f"tier_locations: unknown tier '{tier}'")
            for location_id in location_ids:
                if location_id not in known_locations:
                    problems.append(f"tier_locations[{tier}]: unknown location '{location_id}'")

        for tier in ("core", "adjacent", "breadth"):
            if tier not in self.tier_locations:
                problems.append(f"tier_locations: missing tier '{tier}'")

        return problems

    # -- scrape planning ---------------------------------------------------------------
    def cell_specs(self, sources: tuple[str, ...] = ("indeed", "linkedin")) -> list[dict[str, str]]:
        """Enumerate the (source, family, location, query) cells to be scraped.

        Cell count was once treated as the binding constraint on the whole analytics
        design, pruned against an assumed sustainable rate of ~52 cells/day (LinkedIn
        429ing around the 10th page on one IP). That figure was never real: a direct probe
        found no 429s through page 99 (scripts/probe_linkedin_page_wall.py), and a live
        single-IP run separately sustained ~1000 posts with no block. What actually limits
        the matrix is how often runs happen, not how many cells they hold -- a cell costs
        23-32 s of wall clock, measured over sync_runs.

        The budget is therefore spent on query diversity. A family with one seeded phrasing
        ("AI Engineer") never surfaces a posting phrased "AI Engineering Senior Associate",
        however much depth that one query is allowed, because matching is delegated
        entirely to the board's own relevance ranking.

        EVERY declared query_term is seeded. `tier_locations` is now the only pruning:
        breadth families are searched only where volume is highest. A per-tier cap on
        query terms used to sit here as well, which meant roles.yaml could declare a term
        that was silently never sent to a board -- the audit found four in that state,
        including the one term academic_technology had that was predicted to work. Two
        places to edit for one decision, and the file did not show which terms were live.
        Dropping the cap makes roles.yaml's query_terms mean exactly what it reads as.

        Cell count is now bounded by what roles.yaml declares, so a term costs
        len(tier_locations[tier]) x len(sources) cells -- 14 for core, 8 for adjacent,
        4 for breadth. That is the number to weigh before adding one.
        """
        specs = []
        for family in self.families.values():
            location_ids = self.tier_locations.get(family.tier, [])
            queries = family.query_terms
            for location_id in location_ids:
                location = self.locations.get(location_id)
                if location is None:
                    continue
                for source in sources:
                    for query in queries:
                        specs.append(
                            {
                                "source": source,
                                "role_family": family.key,
                                "location_id": location.id,
                                "query": query,
                                "tier": family.tier,
                            }
                        )
        return specs


_CACHE: dict[tuple[str, float], "RoleTaxonomy"] = {}


def load_roles(path: str | None = None) -> "RoleTaxonomy":
    resolved = path or ROLES_PATH
    try:
        stamp = os.path.getmtime(resolved)
    except OSError:
        stamp = 0
    cache_key = (resolved, stamp)
    if cache_key not in _CACHE:
        _CACHE.clear()
        _CACHE[cache_key] = RoleTaxonomy(resolved)
    return _CACHE[cache_key]


if __name__ == "__main__":
    roles = load_roles()
    print(f"{len(roles)} families, {len(roles.locations)} locations")
    print(f"roles_hash: {roles.hash}")
    issues = roles.validate()
    print("validate(): " + ("OK" if not issues else f"{len(issues)} problem(s)"))
    for issue in issues:
        print(f"  - {issue}")

    for tier in ("core", "adjacent", "breadth"):
        families = roles.by_tier(tier)
        locations = roles.tier_locations.get(tier, [])
        print(f"\n{tier}: {len(families)} families x {len(locations)} locations")
        print(f"  {', '.join(f.key for f in families)}")

    specs = roles.cell_specs()
    print(f"\ntotal scrape cells: {len(specs)}")
    by_source = {}
    for spec in specs:
        by_source[spec["source"]] = by_source.get(spec["source"], 0) + 1
    print(f"  by source: {by_source}")
