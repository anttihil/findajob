"""Canonical skill taxonomy: load, validate, canonicalize, and match.

This module owns the shared vocabulary. Resume candidate surfaces and job-description text
both pass through it, so a match means the same thing on both sides.

Three matching modes exist because naive `\\b` alternation is where this kind of matcher
usually breaks:

  word    (default) case-insensitive, `\\b`-anchored alternation over aliases.
  literal an explicit regex, for tokens `\\b` cannot bound. `\\bc\\+\\+\\b` never matches,
          because there is no word character after the plus signs for `\\b` to anchor to.
  strict  short or ambiguous surfaces ("go", "r", "ray", "spark") that are only counted
          when they appear with original-case capitalization AND near a context word.
          Without this, every "go to market" in every job ad counts as the Go language.
"""

import hashlib
import os
import re
from typing import Any

import yaml

from careerradar.core.paths import DATA_DIR

SKILLS_PATH = os.path.join(DATA_DIR, "skills.yaml")

# How far from a strict alias a context word must appear to license the match.
STRICT_CONTEXT_WINDOW = 120


class Skill:
    __slots__ = (
        "_context_regex",
        "_regex",
        "_strict_regex",
        "aliases",
        "category",
        "context",
        "effort",
        "implies",
        "key",
        "label",
        "strict_aliases",
        "user_level",
    )

    def __init__(self, key: str, spec: dict[str, Any]) -> None:
        self.key = key
        self.label = spec.get("label") or key.replace("_", " ").title()
        self.category = spec.get("category", "other")
        self.aliases = [a for a in spec.get("aliases", []) if a]
        self.strict_aliases = [a for a in spec.get("strict_aliases", []) if a]
        self.context = [c for c in spec.get("context", []) if c]
        self.effort = spec.get("effort", "medium")
        # Subsumption only: "X is a kind of Y", never "X is related to Y". Used on the
        # PROFILE side of a coverage comparison so evidence of `claude_api` counts, at a
        # discount, toward a posting asking for `llm_apps`. Never used in extract().
        self.implies = [i for i in spec.get("implies", []) if i]
        # None means "derive from the resume corpus" -- the default, so the profile stays in
        # sync when the resumes change. An explicit value is for skills no resume mentions.
        self.user_level = spec.get("user_level")

        match_mode = spec.get("match", "word")
        if not self.aliases and not self.strict_aliases and match_mode != "literal":
            aliases = [self.label]
            if "_" in self.key:
                aliases.append(self.key.replace("_", " "))
            self.aliases = aliases

        if match_mode == "literal":
            pattern = spec.get("pattern")
            if not pattern:
                raise ValueError(f"skill '{key}': match: literal requires a pattern")
            self._regex = re.compile(pattern, re.IGNORECASE)
        elif self.aliases:
            # Build boundary-safe alternation: (?<!\w) and (?!\w) ensure tokens like
            # C++, C#, .NET match without requiring manual regex pattern authoring.
            patterns = []
            for a in sorted(self.aliases, key=len, reverse=True):
                clean_a = a.strip()
                if not clean_a:
                    continue
                patterns.append(rf"(?<!\w){re.escape(clean_a)}(?!\w)")
            self._regex = re.compile("|".join(patterns), re.IGNORECASE) if patterns else None
        else:
            self._regex = None

        if self.strict_aliases:
            # Case-insensitive: capitalization is too weak a signal on its own, since job
            # ads shout in headings ("GO TO MARKET") and start sentences with the verb.
            # The context list below is what actually licenses the match.
            self._strict_regex = re.compile(
                r"\b(?:{})\b".format("|".join(re.escape(a.strip()) for a in self.strict_aliases)),
                re.IGNORECASE,
            )
        else:
            self._strict_regex = None

        if self.context:
            self._context_regex = re.compile(
                r"(?:{})".format("|".join(re.escape(c.strip()) for c in self.context)),
                re.IGNORECASE,
            )
        else:
            self._context_regex = None

    def search(self, text: str, lowered: str | None = None) -> bool:  # noqa: ARG002 - accepted so batch callers can pass a prelowered copy
        """Return True if this skill is present in `text`.

        `lowered` is accepted so callers scanning many skills over one document do not
        re-lowercase it per skill.
        """
        if self._regex is not None and self._regex.search(text):
            return True
        return self._search_strict(text)

    def _search_strict(self, text: str) -> bool:
        if self._strict_regex is None:
            return False
        for match in self._strict_regex.finditer(text):
            if self._context_regex is None:
                # A strict alias with no context list would match on capitalization alone,
                # which is too weak. Require context explicitly.
                continue
            start = max(0, match.start() - STRICT_CONTEXT_WINDOW)
            end = min(len(text), match.end() + STRICT_CONTEXT_WINDOW)
            if self._context_regex.search(text[start:end]):
                return True
        return False

    def matches_surface(self, surface: str) -> bool:
        """Whether a single candidate surface string denotes this skill.

        Used to canonicalize resume candidates, where the surface is the whole string
        rather than a span inside prose -- so a strict alias is accepted on an exact
        case-sensitive match without needing surrounding context.
        """
        surface = surface.strip()
        if not surface:
            return False
        if self._regex is not None and self._regex.fullmatch(surface):
            return True
        if self._strict_regex is not None and self._strict_regex.fullmatch(surface):
            return True
        # Fall back to containment for multiword aliases ("local LLM hosting" -> llm_apps).
        return bool(self._regex is not None and self._regex.search(surface))

    def __repr__(self) -> str:
        return f"<Skill {self.key}>"


from careerradar.taxonomy.blockers import DEFAULT_BLOCKERS, Blocker  # noqa: E402


class Taxonomy:
    def __init__(
        self,
        path: str | None = None,
        data: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> None:
        self.path = path
        raw = ""
        loaded_data: dict[str, Any] = {}
        if data is not None:
            loaded_data = data
            raw = yaml.dump(data) if yaml else str(sorted(data.items()))
        elif self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as handle:
                raw = handle.read()
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict):
                loaded_data = parsed
        else:
            loaded_data = {}

        self.version = loaded_data.get("version", 0)
        self.categories = list(loaded_data.get("categories", []))
        self.skills: dict[str, Skill] = {}
        for key, spec in (loaded_data.get("skills") or {}).items():
            self.skills[key] = Skill(key, spec or {})

        self.blockers: dict[str, Blocker] = {}
        raw_blockers = loaded_data.get("blockers")
        if raw_blockers:
            for key, spec in raw_blockers.items():
                self.blockers[key] = Blocker(key, spec or {})
        else:
            for key, spec in DEFAULT_BLOCKERS.items():
                self.blockers[key] = Blocker(key, spec or {})

        self.hash = ""
        self._implied_by: dict[str, set[str]] = {}
        self._closure_cache: dict[Any, frozenset[str]] = {}
        self._recompute_structures(raw)

        if profile is not None:
            self.enrich_from_profile(profile)

    def _recompute_structures(self, raw: str = "") -> None:
        if raw:
            self.hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        else:
            content = f"v{self.version}:" + ",".join(sorted(self.skills.keys()))
            self.hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

        self._implied_by = {}
        for key, skill in self.skills.items():
            for parent in skill.implies:
                self._implied_by.setdefault(parent, set()).add(key)
        self._closure_cache = {}

    def clone(self) -> "Taxonomy":
        """Return an independent copy that can be enriched without mutating the cached base."""
        tax = Taxonomy(path=None, data={})
        tax.version = self.version
        tax.categories = list(self.categories)
        tax.skills = dict(self.skills)
        tax.blockers = dict(self.blockers)
        tax.hash = self.hash
        tax._implied_by = {k: set(v) for k, v in self._implied_by.items()}
        tax._closure_cache = dict(self._closure_cache)
        return tax

    def enrich_from_profile(self, profile: Any) -> None:
        """Add open-vocabulary candidate profile skills to this taxonomy."""
        skills_to_add: dict[str, dict[str, Any]] = {}
        if hasattr(profile, "skills"):
            if isinstance(profile.skills, dict):
                for k, rec in profile.skills.items():
                    label = rec.get("label", k)
                    if k in self.skills or self.canonicalize([label]):
                        continue
                    skills_to_add[k] = {
                        "label": label,
                        "category": rec.get("category", "other"),
                        "aliases": [label, k.replace("_", " ")],
                    }
            elif isinstance(profile.skills, list):
                for cat in profile.skills:
                    c_name = getattr(cat, "category", "other")
                    c_skills = getattr(cat, "skills", [])
                    for s in c_skills:
                        clean = s.strip()
                        if not clean:
                            continue
                        if self.canonicalize([clean]):
                            continue
                        k = re.sub(r"[^a-z0-9_]+", "_", clean.lower()).strip("_")
                        if k in self.skills:
                            continue
                        skills_to_add[k] = {
                            "label": clean,
                            "category": c_name,
                            "aliases": [clean, k.replace("_", " ")],
                        }

        changed = False
        for k, spec in skills_to_add.items():
            if k not in self.skills:
                self.skills[k] = Skill(k, spec)
                cat = spec.get("category", "other")
                if cat not in self.categories:
                    self.categories.append(cat)
                changed = True

        if changed:
            self._recompute_structures()

    @classmethod
    def from_profile(
        cls,
        profile: Any,
        domain_skills: dict[str, Any] | None = None,
        base_taxonomy: "Taxonomy | None" = None,
    ) -> "Taxonomy":
        """Build target-driven taxonomy directly from candidate profile and domain skills."""
        tax = cls(path=None, data={})
        if base_taxonomy is not None:
            tax.version = base_taxonomy.version
            tax.categories = list(base_taxonomy.categories)
            tax.skills = dict(base_taxonomy.skills)
            tax.blockers = dict(base_taxonomy.blockers)
        if domain_skills:
            for k, spec in domain_skills.items():
                tax.skills[k] = Skill(k, spec or {})
                cat = (spec or {}).get("category", "other")
                if cat not in tax.categories:
                    tax.categories.append(cat)
        tax.enrich_from_profile(profile)
        return tax

    # -- lookup ------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.skills)

    def __contains__(self, key: str) -> bool:
        return key in self.skills

    def __getitem__(self, key: str) -> Skill:
        return self.skills[key]

    def get(self, key: str, default: Skill | None = None) -> Skill | None:
        return self.skills.get(key, default)

    def label(self, key: str) -> str:
        skill = self.skills.get(key)
        return skill.label if skill else key

    def category(self, key: str) -> str:
        skill = self.skills.get(key)
        return skill.category if skill else "other"

    def by_category(self, category: str) -> list[Skill]:
        return [s for s in self.skills.values() if s.category == category]

    # -- subsumption -------------------------------------------------------------------
    MAX_IMPLIES_DEPTH = 4

    def closure(self, key: str) -> frozenset[str]:
        """Everything `key` implies, transitively. Excludes `key` itself."""
        cached = self._closure_cache.get(key)
        if cached is not None:
            return cached
        seen, frontier, depth = set(), [key], 0
        while frontier and depth < self.MAX_IMPLIES_DEPTH:
            nxt = []
            for current in frontier:
                skill = self.skills.get(current)
                for parent in skill.implies if skill else ():
                    if parent not in seen and parent != key:
                        seen.add(parent)
                        nxt.append(parent)
            frontier, depth = nxt, depth + 1
        result = frozenset(seen)
        self._closure_cache[key] = result
        return result

    def implied_by(self, parent: str) -> frozenset[str]:
        """Every skill whose presence evidences `parent`, transitively."""
        cached = self._closure_cache.get(("<-", parent))
        if cached is not None:
            return cached
        seen, frontier, depth = set(), [parent], 0
        while frontier and depth < self.MAX_IMPLIES_DEPTH:
            nxt = []
            for current in frontier:
                for child in self._implied_by.get(current, ()):
                    if child not in seen and child != parent:
                        seen.add(child)
                        nxt.append(child)
            frontier, depth = nxt, depth + 1
        result = frozenset(seen)
        self._closure_cache[("<-", parent)] = result
        return result

    # -- validation --------------------------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of problems. Empty means the taxonomy is well-formed."""
        problems = []
        known_categories = set(self.categories)
        seen_aliases: dict[str, str] = {}

        for key, skill in self.skills.items():
            for parent in skill.implies:
                if parent not in self.skills:
                    problems.append(f"skill '{key}': implies unknown skill '{parent}'")
                elif parent == key:
                    problems.append(f"skill '{key}': implies itself")
            # A cycle would make coverage credit circular and, without the depth cap,
            # would not terminate.
            if key in self.closure(key):
                problems.append(f"skill '{key}': implies itself through a cycle")

            if skill.category not in known_categories:
                problems.append(f"skill '{key}': unknown category '{skill.category}'")
            if not skill.aliases and not skill.strict_aliases and skill._regex is None:
                problems.append(f"skill '{key}': no aliases and no pattern")
            if skill.strict_aliases and not skill.context:
                problems.append(
                    f"skill '{key}': strict_aliases require a context list, "
                    "otherwise they match on capitalization alone"
                )
            if skill.user_level is not None and not 0 <= skill.user_level <= 3:
                problems.append(f"skill '{key}': user_level must be 0-3")
            for alias in skill.aliases:
                normalized = alias.strip().lower()
                other = seen_aliases.get(normalized)
                # A shared alias across categories is usually correct and intentional:
                # "GKE" genuinely implies both Google Cloud and Kubernetes, so a posting
                # mentioning it should count toward each. A shared alias *within* one
                # category is almost always a copy-paste mistake.
                if other and other != key and self.skills[other].category == skill.category:
                    problems.append(
                        f"alias '{alias}' claimed by both '{other}' and '{key}', "
                        f"both in category '{skill.category}'"
                    )
                seen_aliases.setdefault(normalized, key)

        return problems

    # -- canonicalization --------------------------------------------------------------
    def canonicalize(self, surfaces: list[str]) -> list[str]:
        """Map candidate surface strings onto canonical keys, dropping the unrecognized.

        Resume parsing deliberately over-produces surfaces; this is where the noise
        ("academic stakeholders", "timeline management") falls away.
        """
        keys = []
        for surface in surfaces:
            for key, skill in self.skills.items():
                if key in keys:
                    continue
                if skill.matches_surface(surface):
                    keys.append(key)
        return keys

    def canonicalize_verbose(self, surfaces: list[str]) -> tuple[dict[str, list[str]], list[str]]:
        """Like canonicalize, but also reports what did not map -- for taxonomy growth."""
        mapped: dict[str, list[str]] = {}
        unmapped = []
        for surface in surfaces:
            hits = [k for k, s in self.skills.items() if s.matches_surface(surface)]
            if hits:
                for key in hits:
                    mapped.setdefault(key, []).append(surface)
            else:
                unmapped.append(surface)
        return mapped, unmapped

    # -- extraction --------------------------------------------------------------------
    def extract(self, text: str | None, title: str = "") -> dict[str, dict[str, bool]]:
        """Find every skill present in a document.

        Returns {skill_key: {"in_title": bool}}. Presence is the signal; within-document
        frequency is noise, so nothing is counted twice.
        """
        if not text:
            text = ""
        found = {}
        for key, skill in self.skills.items():
            if skill.search(text):
                found[key] = {"in_title": bool(title) and skill.search(title)}
        return found

    def extract_blockers(self, text: str) -> list[str]:
        if not text:
            return []
        return [key for key, blocker in self.blockers.items() if blocker.search(text)]


_CACHE: dict[tuple[str, float], "Taxonomy"] = {}


def load_taxonomy(path: str | None = None, profile: Any = None) -> "Taxonomy":
    """Load and cache a taxonomy.

    If path is provided and exists, loads the YAML file.
    Otherwise returns an open-vocabulary taxonomy (optionally enriched from profile).
    """
    resolved = path or SKILLS_PATH
    stamp = 0
    if os.path.exists(resolved):
        try:
            stamp = os.path.getmtime(resolved)
        except OSError:
            stamp = 0
    cache_key = (resolved, stamp)
    if cache_key not in _CACHE:
        _CACHE.clear()
        _CACHE[cache_key] = Taxonomy(path=resolved if os.path.exists(resolved) else None)
    tax = _CACHE[cache_key]
    if profile is not None:
        tax = tax.clone()
        tax.enrich_from_profile(profile)
    elif not tax.skills:
        try:
            from careerradar.profile.repository import load_profile as repo_load_profile

            active_p = repo_load_profile()
            if active_p and active_p.skills:
                tax = tax.clone()
                tax.enrich_from_profile(active_p)
        except Exception:  # noqa: BLE001
            pass
    return tax


if __name__ == "__main__":
    taxonomy = load_taxonomy()
    print(f"{len(taxonomy)} skills, {len(taxonomy.blockers)} blockers")
    print(f"taxonomy_hash: {taxonomy.hash}")
    issues = taxonomy.validate()
    if issues:
        print(f"\n{len(issues)} problem(s):")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("validate(): OK")

    counts = {}
    for skill in taxonomy.skills.values():
        counts[skill.category] = counts.get(skill.category, 0) + 1
    print("\nby category:")
    for category in taxonomy.categories:
        print(f"  {category:16} {counts.get(category, 0)}")
