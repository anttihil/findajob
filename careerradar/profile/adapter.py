"""Expose a Profile through the dictionary interface needed for UI components."""

import re
from typing import TYPE_CHECKING, Any

from careerradar.profile.models import Profile

if TYPE_CHECKING:
    from careerradar.core.database import Database


class ProfileAdapter:
    """A Profile wearing the query interface for dashboard and API representation."""

    def __init__(
        self,
        profile: Profile,
        version: int | None = 1,
    ) -> None:
        self.profile = profile
        self.version = version

        # Build skills map from categorized skills (binary: has skill)
        skills_map: dict[str, dict[str, Any]] = {}
        for cat in profile.skills:
            for s_name in cat.skills:
                clean = s_name.strip()
                if not clean:
                    continue
                key = re.sub(r"[^a-z0-9_]+", "_", clean.lower()).strip("_")
                skills_map[key] = {
                    "level": 1,
                    "label": clean,
                    "evidence": [f"{cat.category}: {clean}"],
                    "recency": None,
                    "category": cat.category,
                }

        self.skills = skills_map
        self.sources = [f"profile v{version}"] if version else ["profile"]

    def has(self, key: str, min_level: int = 1) -> bool:  # noqa: ARG002 - compatibility
        return key in self.skills

    def level(self, key: str) -> int:
        return 1 if key in self.skills else 0

    def keys(self, min_level: int = 1) -> set[str]:  # noqa: ARG002 - compatibility
        return set(self.skills.keys())

    def evidence(self, key: str) -> list[str]:
        record = self.skills.get(key)
        return record["evidence"] if record else []

    def by_category(self) -> dict[str, list[dict[str, Any]]]:
        """Group skills by category for dashboard display."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for key, record in self.skills.items():
            category = record.get("category") or "other"
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


def load_profile_adapter(
    db: "Database | None" = None,
    required: bool = True,
) -> ProfileAdapter | None:
    """Load the singleton Profile as a ProfileAdapter."""
    from careerradar.profile.repository import load_profile as repo_load_profile

    profile = repo_load_profile(conn=db.conn if db else None)
    if profile is None and required:
        raise NoActiveProfile("No active profile configured.")
    if profile is None:
        return None
    return ProfileAdapter(profile, version=1)


# Backward compatibility alias
load_profile = load_profile_adapter
