"""Role-family and seniority normalization from job titles.

`role_family` is always re-derived from the title, never inherited from the search query
that surfaced the posting. A live "Platform Engineer" query on Indeed returned a
"Maintenance Technician", so the query predicts very little about what came back -- and
counting those returns toward platform-engineer supply would inflate it.

Titles that match no family get `None`. Those postings are retained for audit but excluded
from every statistic; the real corpus already contains "Full-time", "Remote (US, Canada)",
"Toronto, ON", and "YC 19" as titles.
"""

import hashlib
import os
import re

import yaml

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
ROLES_PATH = os.path.join(DATA_DIR, "roles.yaml")

SENIORITY_UNSPECIFIED = "unspecified"


class RoleFamily:
    __slots__ = ("key", "label", "tier", "resume", "query_terms", "order", "_patterns",
                 "_weak_patterns")

    def __init__(self, key, spec, order):
        self.key = key
        self.label = spec.get("label", key.replace("_", " ").title())
        self.tier = spec.get("tier", "breadth")
        self.resume = spec.get("resume")
        self.query_terms = spec.get("query_terms") or [self.label]
        # Declaration order doubles as a specificity ranking, used to break position ties.
        self.order = order
        self._patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])]
        # Weak patterns need corroboration from a technical skill in the posting. Bare
        # "X Engineer" titles are otherwise claimed by the generic family regardless of
        # whether the job has anything to do with software.
        self._weak_patterns = [
            re.compile(p, re.IGNORECASE) for p in spec.get("weak_patterns", [])
        ]

    def find(self, title, allow_weak=True):
        """Earliest match offset in `title`, or None.

        Returns (offset, is_weak). A weak-only match is reported so the caller can require
        technical corroboration before accepting it.
        """
        best = None
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

    def __repr__(self):
        return f"<RoleFamily {self.key}>"


class Location:
    __slots__ = ("id", "label", "country", "is_remote", "weight", "indeed_country",
                 "distance")

    def __init__(self, spec):
        self.id = spec["id"]
        self.label = spec["label"]
        self.country = spec["country"]
        self.is_remote = bool(spec.get("is_remote", False))
        self.weight = float(spec.get("weight", 1.0))
        self.indeed_country = spec.get("indeed_country", "usa")
        self.distance = spec.get("distance", 50)

    def to_dict(self):
        return {
            "id": self.id, "label": self.label, "country": self.country,
            "is_remote": self.is_remote, "weight": self.weight,
            "indeed_country": self.indeed_country, "distance": self.distance,
        }


class RoleTaxonomy:
    def __init__(self, path=None):
        self.path = path or ROLES_PATH
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        data = yaml.safe_load(raw)

        self.version = data.get("version", 0)
        self.families = {}
        for order, (key, spec) in enumerate((data.get("families") or {}).items()):
            self.families[key] = RoleFamily(key, spec or {}, order)

        self._seniority = []
        for level, spec in (data.get("seniority") or {}).items():
            patterns = [re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])]
            self._seniority.append((level, patterns))

        self.locations = {}
        for spec in data.get("locations") or []:
            location = Location(spec)
            self.locations[location.id] = location

        self.tier_locations = data.get("tier_locations") or {}
        self._exclusions = [
            re.compile(p, re.IGNORECASE) for p in (data.get("exclusions") or [])
        ]
        self.hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    # -- lookup ------------------------------------------------------------------------
    def __len__(self):
        return len(self.families)

    def __contains__(self, key):
        return key in self.families

    def get(self, key, default=None):
        return self.families.get(key, default)

    def label(self, key):
        family = self.families.get(key)
        return family.label if family else (key or "Unclassified")

    def tier(self, key):
        family = self.families.get(key)
        return family.tier if family else None

    def resume_for(self, key):
        family = self.families.get(key)
        return family.resume if family else None

    def by_tier(self, tier):
        return [f for f in self.families.values() if f.tier == tier]

    # -- normalization -----------------------------------------------------------------
    def classify(self, title, has_tech_skills=False):
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

        candidates = []
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

    def is_excluded(self, title):
        """Whether a title looks like a non-software engineering or non-technical role.

        Hardware, RF, manufacturing, and facilities postings do mention Python and Linux, so
        the technical-skill gate alone does not exclude them -- and left unchecked they are
        absorbed by the generic family and inflate its supply figure.
        """
        return any(pattern.search(title or "") for pattern in self._exclusions)

    def seniority(self, title):
        if not title:
            return SENIORITY_UNSPECIFIED
        for level, patterns in self._seniority:
            for pattern in patterns:
                if pattern.search(title):
                    return level
        return SENIORITY_UNSPECIFIED

    def classify_all(self, title, has_tech_skills=False):
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
        hits = []
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
            if not family.query_terms:
                problems.append(f"family '{key}': no query_terms")
            if family.tier not in ("core", "adjacent", "breadth"):
                problems.append(f"family '{key}': unknown tier '{family.tier}'")

        for tier, location_ids in self.tier_locations.items():
            if tier not in ("core", "adjacent", "breadth"):
                problems.append(f"tier_locations: unknown tier '{tier}'")
            for location_id in location_ids:
                if location_id not in known_locations:
                    problems.append(
                        f"tier_locations[{tier}]: unknown location '{location_id}'"
                    )

        for tier in ("core", "adjacent", "breadth"):
            if tier not in self.tier_locations:
                problems.append(f"tier_locations: missing tier '{tier}'")

        return problems

    # -- scrape planning ---------------------------------------------------------------
    def cell_specs(self, sources=("indeed", "linkedin"), queries_per_family=1):
        """Enumerate the (source, family, location, query) cells to be scraped.

        Cell count is the binding constraint on the whole analytics design, so it is worth
        being explicit about the arithmetic. The unpruned cross-product is 28 families x 7
        locations x 2 sources x up to 4 query terms -- over 500 cells. At the sustainable
        rate of roughly 52 cells/day (LinkedIn 429s around the 10th page on one IP), that
        is a 10-day full-matrix cycle, and honest supply comparison needs several complete
        cycles inside the analysis window. So two prunings apply:

          tier_locations      breadth families are searched only where volume is highest
          queries_per_family  one search term per family by default

        Together these give ~126 cells per source, a ~5-day cycle, which supports the
        30-day minimum analysis window. Additional query terms are not discarded -- they
        remain on the family for the scheduler to rotate through on later visits.
        """
        specs = []
        for family in self.families.values():
            location_ids = self.tier_locations.get(family.tier, [])
            queries = family.query_terms[:max(1, queries_per_family)]
            for location_id in location_ids:
                location = self.locations.get(location_id)
                if location is None:
                    continue
                for source in sources:
                    for query in queries:
                        specs.append({
                            "source": source,
                            "role_family": family.key,
                            "location_id": location.id,
                            "query": query,
                            "tier": family.tier,
                        })
        return specs

    def alternate_queries(self, family_key):
        """Query terms beyond the primary, for the scheduler to rotate through."""
        family = self.families.get(family_key)
        return list(family.query_terms[1:]) if family else []


_CACHE = {}


def load_roles(path=None):
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
