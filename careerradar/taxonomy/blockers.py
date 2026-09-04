"""Decoupled blocker and dealbreaker pattern detection.

Migrated from hardcoded static skills.yaml into a standalone component that can
be loaded from configuration, candidate dealbreakers, or default industry patterns.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_BLOCKERS: dict[str, dict[str, Any]] = {
    "eu_work_authorization": {
        "label": "EU/EEA work authorization required",
        "patterns": [
            r"\b(?:eu|eea)\s+work\s+(?:permit|authori[sz]ation|authorisation)\b",
            r"\bright to work in (?:the )?(?:eu|eea|sweden|norway|denmark|finland)\b",
            r"\barbetstillst[åa]nd\b",
            r"\boppholdstillatelse\b",
        ],
    },
    "us_work_authorization": {
        "label": "US work authorization required",
        "patterns": [
            r"\bus (?:work )?(?:authorization|authorisation)\b",
            r"\bmust be (?:legally )?authorized to work in the (?:us|united states)\b",
            r"\bno (?:visa )?sponsorship\b",
            r"\b(?:unable|not able|cannot|can not|do not|does not|won't)\s+(?:\w+\s+){0,3}?sponsor",
            r"\bsponsorship (?:is )?not (?:available|offered|provided)\b",
            r"\bwithout (?:the need for )?sponsorship\b",
            r"\bcitizens? only\b",
        ],
    },
    "security_clearance": {
        "label": "Security clearance required",
        "patterns": [
            r"\bsecurity clearance\b",
            r"\b(?:ts/sci|top secret)\b",
            r"\bpublic trust\b",
        ],
    },
    "local_language": {
        "label": "Local language required",
        "patterns": [
            r"\b(?:fluent|proficient|native)\s+(?:in\s+)?(?:swedish|norwegian|danish|finnish|german)\b",
            (
                r"\b(?:swedish|norwegian|danish|finnish)\s+"
                r"(?:language\s+)?(?:required|is required|skills required)\b"
            ),
            r"\bflytande svenska\b",
        ],
    },
}


class Blocker:
    __slots__ = ("_regex", "key", "label", "patterns")

    def __init__(self, key: str, spec: dict[str, Any]) -> None:
        self.key = key
        self.label = spec.get("label", key)
        self.patterns = spec.get("patterns", [])
        if not self.patterns:
            raise ValueError(f"blocker '{key}': needs at least one pattern")
        self._regex = re.compile("|".join(f"(?:{p})" for p in self.patterns), re.IGNORECASE)

    def search(self, text: str) -> bool:
        return bool(self._regex.search(text))

    def __repr__(self) -> str:
        return f"<Blocker {self.key}: {self.label}>"


class BlockerExtractor:
    """Extracts blockers from text using configured or default regex patterns."""

    def __init__(
        self,
        blockers: dict[str, dict[str, Any]] | None = None,
        dealbreakers: list[str] | None = None,
    ) -> None:
        raw_specs = dict(DEFAULT_BLOCKERS)
        if blockers:
            raw_specs.update(blockers)
        self.blockers: dict[str, Blocker] = {k: Blocker(k, spec) for k, spec in raw_specs.items()}

        # Optional candidate-specific dealbreakers compiled into blocker patterns
        if dealbreakers:
            for i, db_text in enumerate(dealbreakers):
                cleaned = db_text.strip()
                if not cleaned:
                    continue
                key = f"dealbreaker_{i}"
                escaped = re.escape(cleaned)
                self.blockers[key] = Blocker(
                    key,
                    {"label": cleaned, "patterns": [rf"\b{escaped}\b"]},
                )

    def extract(self, text: str | None) -> list[str]:
        if not text:
            return []
        return [key for key, blocker in self.blockers.items() if blocker.search(text)]

    def __len__(self) -> int:
        return len(self.blockers)

    def __contains__(self, key: str) -> bool:
        return key in self.blockers

    def get(self, key: str) -> Blocker | None:
        return self.blockers.get(key)


def load_blocker_extractor(
    config: dict[str, Any] | None = None,
    profile: Any = None,
) -> BlockerExtractor:
    """Construct a blocker extractor from configuration and profile dealbreakers."""
    config = config or {}
    blocker_specs = config.get("blockers") or config.get("scoring", {}).get("blockers")
    dealbreakers: list[str] | None = None
    if profile is not None:
        if hasattr(profile, "dealbreakers") and profile.dealbreakers:
            dealbreakers = profile.dealbreakers
        elif hasattr(profile, "profile") and getattr(profile.profile, "dealbreakers", None):
            dealbreakers = profile.profile.dealbreakers
    return BlockerExtractor(blockers=blocker_specs, dealbreakers=dealbreakers)
