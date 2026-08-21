"""The shapes the profile agent reads and writes.

`Profile` is the artifact everything downstream depends on: the scoring agent renders it
into a cached prompt prefix, the research agent uses its constraints to judge relevance,
and the dashboard shows it back to the user. It is built once, reviewed by a human, and
versioned -- not recomputed per request like the regex profile it replaces.
"""

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Kept identical to the levels the old regex profile used, because `keyword_score.py` and
# `gap_analysis.py` still index into LEVEL_CREDIT with these numbers. What changes is how a
# level is *decided*: previously "appeared in a bolded phrase" vs "appeared in a bulleted
# list", now a judgement about demonstrated capability with the evidence recorded next to it.
LEVEL_STRONG = 3
LEVEL_CLAIMED = 2
LEVEL_MENTIONED = 1
LEVEL_ABSENT = 0


class Skill(BaseModel):
    key: str = Field(description="snake_case canonical key, e.g. 'kubernetes'")
    label: str = Field(description="Human-readable name, e.g. 'Kubernetes'")
    level: int = Field(
        ge=0,
        le=3,
        description=(
            "3 = built and shipped substantial work with it; "
            "2 = used it in real work; "
            "1 = touched it or studied it; "
            "0 = does not have it"
        ),
    )
    evidence: str = Field(
        description="What in the documents or interview justifies this level. Be concrete."
    )
    recency: str | None = Field(default=None, description="When it was last used, if determinable.")


class Constraints(BaseModel):
    """Facts that make a posting a non-starter regardless of skill fit."""

    work_authorization: list[str] = Field(
        default_factory=list,
        description="Where the candidate can already work without sponsorship.",
    )
    locations: list[str] = Field(
        default_factory=list, description="Where they can work: cities, 'remote', regions."
    )
    willing_to_relocate: bool | None = None
    comp_floor_usd: int | None = Field(
        default=None, description="Annual base below which they would decline."
    )
    languages: list[str] = Field(default_factory=list)
    notes: str | None = None


class Preferences(BaseModel):
    """Soft signals. These shade a score; they do not veto."""

    role_families: list[str] = Field(default_factory=list)
    company_sizes: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    work_mode: str | None = Field(default=None, description="remote / hybrid / onsite")
    notes: str | None = None


class Profile(BaseModel):
    """The complete candidate picture."""

    bio: str = Field(
        description=(
            "Two or three sentences: who this person is professionally, and their trajectory."
        )
    )
    years_experience: float | None = None
    seniority: str | None = Field(default=None, description="junior / mid / senior / staff / lead")
    skills: list[Skill] = Field(default_factory=list)
    projects: list[str] = Field(
        default_factory=list,
        description="Short descriptions of key projects, systems built, or major achievements.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="What they are genuinely good at, beyond a skill list. Differentiators.",
    )
    weaknesses: list[str] = Field(
        default_factory=list,
        description="Honest gaps. Used to flag stretch roles, not to disqualify.",
    )
    constraints: Constraints = Field(default_factory=Constraints)
    preferences: Preferences = Field(default_factory=Preferences)
    non_negotiables: list[str] = Field(
        default_factory=list, description="Things that make a role an automatic no."
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Patterns in a posting this candidate has learned to avoid.",
    )


class ExtractedClaims(BaseModel):
    """First pass: what the documents alone support, before any interview."""

    bio: str
    years_experience: float | None = None
    seniority: str | None = None
    skills: list[Skill] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    apparent_strengths: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(
        default_factory=list,
        description="Places the documents disagree with each other, or oversell.",
    )


class GapQuestion(BaseModel):
    topic: str = Field(description="Short slug: 'seniority', 'comp', 'weaknesses', ...")
    question: str = Field(description="The question to ask, in second person.")
    why: str = Field(description="What about the documents makes this worth asking.")


class GapQuestions(BaseModel):
    """What the documents cannot answer.

    Asked for explicitly rather than inferred, because the failure mode of a
    document-only profile is confident silence: it does not know what it is missing, so
    it fills the gap with a plausible default and every downstream score inherits it.
    """

    questions: list[GapQuestion] = Field(default_factory=list)


# --- simplified scoring verdict ---------------------------------------------------------
#
# Bumped when the schema below changes shape. `scoring/worker.py` selects on it, so raising
# it re-drains the backlog through the normal resumable path instead of needing a
# `--rescore-all` that starts over on every interruption.
VERDICT_SCHEMA_VERSION = 3


class JobFitVerdict(BaseModel):
    """The scoring agent's simplified output for one posting.

    Evaluates whether the candidate meets the core requirements for the posting
    at >= target threshold.
    """

    fit: bool = Field(
        description=(
            "True if the candidate fits this job at >= target match threshold "
            "(e.g. 90%), otherwise False."
        )
    )
    reason_type: str = Field(
        description=(
            "A single lowercase word classifying the primary verdict reason. "
            "Examples: 'match', 'skills', 'experience', 'seniority', 'domain', "
            "'clearance', 'location', 'tech_stack', 'overqualified'."
        )
    )
    reason_description: str = Field(
        description="A concise 1-2 sentence explanation of why the candidate fits or does not fit."
    )

    @field_validator("reason_type", mode="before")
    @classmethod
    def _clean_reason_type(cls, value: Any) -> str:
        if not isinstance(value, str):
            return "unknown"
        cleaned = re.sub(r"[^a-z0-9_-]", "", value.strip().lower())
        return cleaned or "unknown"


# Backward compatibility alias
FitAssessment = JobFitVerdict


_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTE_MARKS = dict.fromkeys(map(ord, "‘’‚‛′"), "'")
_QUOTE_MARKS.update(dict.fromkeys(map(ord, "“”„‟″"), '"'))


def normalize_requirement(text: str) -> str:
    """Normalize requirement text for display/matching."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DASHES).translate(_QUOTE_MARKS)
    return re.sub(r"\s+", " ", text).strip().casefold()
