"""The shapes the profile agent reads and writes.

`Profile` is the artifact everything downstream depends on: the scoring agent renders it
into a cached prompt prefix, the research agent uses its constraints to judge relevance,
and the dashboard shows it back to the user. It is built once, reviewed by a human, and
versioned -- not recomputed per request like the regex profile it replaces.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

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
        ge=0, le=3,
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
    recency: Optional[str] = Field(
        default=None, description="When it was last used, if determinable."
    )


class Constraints(BaseModel):
    """Facts that make a posting a non-starter regardless of skill fit."""

    work_authorization: list[str] = Field(
        default_factory=list, description="Where the candidate can already work without sponsorship."
    )
    locations: list[str] = Field(
        default_factory=list, description="Where they can work: cities, 'remote', regions."
    )
    willing_to_relocate: Optional[bool] = None
    comp_floor_usd: Optional[int] = Field(
        default=None, description="Annual base below which they would decline."
    )
    languages: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class Preferences(BaseModel):
    """Soft signals. These shade a score; they do not veto."""

    role_families: list[str] = Field(default_factory=list)
    company_sizes: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    work_mode: Optional[str] = Field(default=None, description="remote / hybrid / onsite")
    notes: Optional[str] = None


class Profile(BaseModel):
    """The complete candidate picture."""

    bio: str = Field(
        description="Two or three sentences: who this person is professionally, and their trajectory."
    )
    years_experience: Optional[float] = None
    seniority: Optional[str] = Field(
        default=None, description="junior / mid / senior / staff / lead"
    )
    skills: list[Skill] = Field(default_factory=list)
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
    years_experience: Optional[float] = None
    seniority: Optional[str] = None
    skills: list[Skill] = Field(default_factory=list)
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


class FitVerdict(BaseModel):
    """The scoring agent's output for one posting.

    Deliberately close to what the retired Claude reranker returned -- that schema was
    well-chosen. `hard_blockers` quoting the posting is the load-bearing part: it makes a
    verdict checkable against its evidence instead of something you either trust or don't.
    """

    fit_score: int = Field(ge=0, le=100)
    verdict: Literal["strong", "worth_applying", "stretch", "poor_fit", "mismatch"]
    seniority_fit: Literal["below", "matched", "above"]
    hard_blockers: list[str] = Field(
        default_factory=list,
        description="Requirements that disqualify. Quote the phrase from the posting.",
    )
    key_gaps: list[str] = Field(
        default_factory=list, description="Missing but learnable or negotiable."
    )
    strengths: list[str] = Field(
        default_factory=list, description="Where this candidate is a strong answer to the posting."
    )
    reasoning: str = Field(description="Two or three sentences. Blunt and specific.")
    research_worthy: bool = Field(
        description="Whether the company is worth a deep-research pass."
    )
