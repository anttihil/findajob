"""Open-vocabulary skill metadata container."""

import hashlib
from typing import Any


class Skill:
    __slots__ = ("category", "effort", "key", "label")

    def __init__(self, key: str, spec: dict[str, Any] | None = None) -> None:
        spec = spec or {}
        self.key = key
        self.label = spec.get("label") or key.replace("_", " ").title()
        self.category = spec.get("category", "other")
        self.effort = spec.get("effort", "medium")

    def __repr__(self) -> str:
        return f"<Skill {self.key}: {self.label}>"


class Taxonomy:
    """Open-vocabulary skill container populated dynamically from Candidate Profile."""

    def __init__(
        self,
        skills: dict[str, Skill] | None = None,
        data: dict[str, Any] | None = None,
        path: str | None = None,  # noqa: ARG002
    ) -> None:
        self.skills: dict[str, Skill] = {}
        if skills:
            self.skills.update(skills)
        if data and "skills" in data:
            for k, spec in data["skills"].items():
                self.skills[k] = Skill(k, spec)
        self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        keys = sorted(self.skills.keys())
        if not keys:
            return "open_vocabulary"
        return hashlib.sha256(",".join(keys).encode("utf-8")).hexdigest()[:16]

    @property
    def categories(self) -> list[str]:
        cats = {s.category for s in self.skills.values()}
        return sorted(cats)

    def __len__(self) -> int:
        return len(self.skills)

    def __contains__(self, key: str) -> bool:
        return key in self.skills

    def __getitem__(self, key: str) -> Skill:
        return self.skills[key]

    def __iter__(self):
        return iter(self.skills)

    def keys(self):
        return self.skills.keys()

    def values(self):
        return self.skills.values()

    def items(self):
        return self.skills.items()

    def get(self, key: str) -> Skill | None:
        return self.skills.get(key)

    def label(self, key: str) -> str:
        skill = self.skills.get(key)
        return skill.label if skill else key.replace("_", " ").title()

    def category(self, key: str) -> str:
        skill = self.skills.get(key)
        return skill.category if skill else "other"

    def effort(self, key: str) -> str:
        skill = self.skills.get(key)
        return skill.effort if skill else "medium"

    def validate(self) -> list[str]:
        return []

    def clone(self) -> "Taxonomy":
        cloned_skills = {
            k: Skill(k, {"label": s.label, "category": s.category, "effort": s.effort})
            for k, s in self.skills.items()
        }
        return Taxonomy(cloned_skills)

    def enrich_from_profile(self, profile: Any) -> None:
        if profile is None:
            return
        cats = getattr(profile, "skills", None)
        if isinstance(cats, list):
            for cat in cats:
                cat_name = getattr(cat, "category", "other")
                for s_name in getattr(cat, "skills", []):
                    clean = str(s_name).strip()
                    if not clean:
                        continue
                    key = clean.lower().replace(" ", "_").replace("-", "_")
                    if key not in self.skills:
                        self.skills[key] = Skill(key, {"label": clean, "category": cat_name})
        elif isinstance(cats, dict):
            for key, rec in cats.items():
                if key not in self.skills:
                    self.skills[key] = Skill(
                        key,
                        {"label": rec.get("label", key), "category": rec.get("category", "other")},
                    )
        self.hash = self._compute_hash()

    @classmethod
    def from_profile(cls, profile: Any, domain_skills: list[str] | None = None) -> "Taxonomy":
        tax = cls()
        tax.enrich_from_profile(profile)
        if domain_skills:
            for s in domain_skills:
                clean = s.strip()
                if clean:
                    key = clean.lower().replace(" ", "_").replace("-", "_")
                    if key not in tax.skills:
                        tax.skills[key] = Skill(key, {"label": clean, "category": "domain"})
            tax.hash = tax._compute_hash()
        return tax


_CACHE: dict[str, Taxonomy] = {}


def load_taxonomy(path: str | None = None, profile: Any = None) -> Taxonomy:  # noqa: ARG001
    global _CACHE
    if "default" not in _CACHE:
        _CACHE["default"] = Taxonomy()
    tax = _CACHE["default"]
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
