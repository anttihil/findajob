"""Expose a Profile through the interface the keyword and gap analysis layer speaks."""

import re
from typing import TYPE_CHECKING, Any

from careerradar.profile.models import LEVEL_CLAIMED, LEVEL_MENTIONED, LEVEL_STRONG, Profile

if TYPE_CHECKING:
    from careerradar.core.database import Database
    from careerradar.taxonomy.skills import Taxonomy


class ProfileAdapter:
    """A Profile wearing the UserProfile query interface."""

    def __init__(
        self,
        profile: Profile,
        version: int | None = 1,
        taxonomy: "Taxonomy | None" = None,
    ) -> None:
        self.profile = profile
        self.version = version
        self.taxonomy = taxonomy

        # Build skills map from categorized skills
        skills_map: dict[str, dict[str, Any]] = {}
        for cat in profile.skills:
            for s_name in cat.skills:
                clean = s_name.strip()
                if not clean:
                    continue
                key = re.sub(r"[^a-z0-9_]+", "_", clean.lower()).strip("_")
                skills_map[key] = {
                    "level": LEVEL_CLAIMED,
                    "label": clean,
                    "evidence": [f"{cat.category}: {clean}"],
                    "recency": None,
                }

        # Check experience bullets for strong evidence (ground truth)
        exp_text = ""
        for role in profile.experience:
            for proj in role.projects:
                proj_heading = proj.heading or ""
                exp_text += f" {proj_heading} " + " ".join(proj.bullets)
        for proj in profile.projects:
            proj_heading = proj.heading or ""
            exp_text += f" {proj_heading} " + " ".join(proj.bullets)
        exp_lower = exp_text.lower()

        for key, rec in skills_map.items():
            if (
                key in exp_lower
                or rec["label"].lower() in exp_lower
                or re.search(rf"\b{re.escape(key)}\b", exp_lower)
            ):
                rec["level"] = LEVEL_STRONG
                rec["evidence"].append(f"Demonstrated in work experience: {rec['label']}")

        self.skills = skills_map
        self.sources = [f"profile v{version}"] if version else ["profile"]

    def has(self, key: str, min_level: int = LEVEL_MENTIONED) -> bool:
        return self.level(key) >= min_level

    def level(self, key: str) -> int:
        record = self.skills.get(key)
        return record["level"] if record else 0

    def keys(self, min_level: int = LEVEL_MENTIONED) -> set[str]:
        return {k for k, v in self.skills.items() if v["level"] >= min_level}

    def evidence(self, key: str) -> list[str]:
        record = self.skills.get(key)
        return record["evidence"] if record else []

    def by_category(self) -> dict[str, list[dict[str, Any]]]:
        """Group skills by taxonomy category for dashboard display."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for key, record in self.skills.items():
            category = "other"
            if self.taxonomy is not None:
                category = self.taxonomy.category(key) or "other"
            grouped.setdefault(category, []).append(
                {"key": key, "label": record["label"], "level": record["level"]}
            )
        for entries in grouped.values():
            entries.sort(key=lambda e: (-e["level"], e["key"]))
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.profile.name,
            "email": self.profile.email,
            "bio": self.profile.summary_guidance,
            "years_experience": self.profile.years_experience,
            "seniority": self.profile.seniority,
            "skills": {
                key: {"level": r["level"], "label": r["label"], "evidence": r["evidence"]}
                for key, r in self.skills.items()
            },
            "eligibility": self.profile.eligibility.model_dump(),
            "targeting": self.profile.targeting.model_dump(),
            "by_category": self.by_category(),
        }

    def __len__(self) -> int:
        return len(self.skills)

    def __contains__(self, key: str) -> bool:
        return key in self.skills


class NoActiveProfile(RuntimeError):
    """Raised when profile is not configured."""


def load_profile(
    db: "Database | None" = None,
    taxonomy: "Taxonomy | None" = None,
    required: bool = True,
) -> ProfileAdapter | None:
    """Load the singleton Profile as a ProfileAdapter."""
    from careerradar.profile.repository import load_profile as repo_load_profile

    profile = repo_load_profile(conn=db.conn if db else None)
    if profile is None and required:
        raise NoActiveProfile("No active profile configured.")
    if profile is None:
        return None
    return ProfileAdapter(profile, version=1, taxonomy=taxonomy)
