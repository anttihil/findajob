"""The shapes the profile agent reads and writes.

`Profile` is the artifact everything downstream depends on: the scoring agent renders it
into a cached prompt prefix, the research agent uses its constraints to judge relevance,
and the dashboard shows it back to the user. It is built once, reviewed by a human, and
versioned -- not recomputed per request like the regex profile it replaces.
"""

import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from careerradar.scoring import rubric

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
    research_worthy: bool = Field(description="Whether the company is worth a deep-research pass.")

    @field_validator("hard_blockers", "key_gaps", "strengths", mode="before")
    @classmethod
    def _accept_a_json_encoded_list(cls, value: Any) -> Any:
        """Take a list field the model filled with the *text* of a list.

        V4 does this intermittently -- `'["a", "b"]'` as a string where a list was
        required -- and strict function calling does not prevent it. The scoring graph
        retries, so it was invisible except as cost: every occurrence buys the same
        posting a second full call, and three in a row lose the verdict entirely.
        Decoding what the model plainly meant is cheaper than another round trip.
        """
        if not isinstance(value, str):
            return value
        try:
            decoded = json.loads(value)
        except ValueError:
            # Not JSON at all: one blocker written as a bare sentence. Keeping it beats
            # discarding a verdict over punctuation.
            return [value] if value.strip() else []
        if isinstance(decoded, list):
            return decoded
        return [str(decoded)] if decoded not in (None, "") else []


# --- the ordinal assessment -------------------------------------------------------------
#
# Bumped when the schema below changes shape. `scoring/worker.py` selects on it, so raising
# it re-drains the backlog through the normal resumable path instead of needing a
# `--rescore-all` that starts over on every interruption.
VERDICT_SCHEMA_VERSION = 2

_ANCHORED = "  ".join(f"{value}: {text}" for value, text in rubric.IMPORTANCE_ANCHORS.items())

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTE_MARKS = dict.fromkeys(map(ord, "‘’‚‛′"), "'")
_QUOTE_MARKS.update(dict.fromkeys(map(ord, "“”„‟″"), '"'))


def normalize_requirement(text: str) -> str:
    """The join key between `core_requirements` and `requirement_assessments`.

    The two lists are one table -- `core_requirements` carries the importance and
    `requirement_assessments` carries the status -- joined on the requirement text the
    model was told to repeat word for word. Four call sites need that join and they must
    not disagree about which requirement is which: the validator below, the
    `assessment_incomplete` flag in `scoring/audit.py`, the dashboard counts in
    `core/database._requirement_summary`, and the per-row display join in
    `web/rendering.py`. They had drifted -- the last used `.lower()` where the others used
    `.casefold()`.

    `strip().casefold()` alone was too literal. Over one backlog run it rejected 86
    verdicts on first attempt, 94% of which the model then fixed by re-typing the same
    string correctly, so the pipeline paid a whole extra call for punctuation. This folds
    the differences that are not a difference of meaning, the same set
    `scoring/audit.normalize` folds for quotes.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DASHES).translate(_QUOTE_MARKS)
    return re.sub(r"\s+", " ", text).strip().casefold()


class CoreRequirement(BaseModel):
    """One thing the posting actually asks for, with the words that ask for it.

    The quote is what makes the extraction checkable. `scoring/audit.py` verifies it is
    really in the posting, so a requirement the model invented is detectable rather than
    something the reader has to take on trust.
    """

    requirement: str = Field(description="The requirement, in your own words, one line.")
    quote: str = Field(description="The phrase from the posting that states it, copied verbatim.")
    importance: Literal["must_have", "important", "nice_to_have"] = Field(description=_ANCHORED)

    @model_validator(mode="before")
    @classmethod
    def _accept_a_requirement_written_as_a_question(cls, value: Any) -> Any:
        """Take `question` where `requirement` belongs.

        The rules tell the model it "answers five questions on named scales", and it
        carries the word down into the extraction step: 46 rejections in one run were a
        `core_requirements` entry keyed `question` with everything else correct. A
        `validation_alias` would fix it too, but it renames the property in the generated
        tool schema, and the schema is the one place this field's name is stated.
        """
        if isinstance(value, dict) and "requirement" not in value and "question" in value:
            return {**value, "requirement": value["question"]}
        return value


class RequirementAssessment(BaseModel):
    requirement: str = Field(
        description="Must repeat one of the requirements you listed above, word for word."
    )
    status: Literal["met", "partial", "unmet"] = Field(
        description=(
            "met: the profile shows this. partial: it shows some of it. unmet: it does "
            "not. A requirement the candidate is disqualified on is `unmet` -- say that "
            "it disqualifies in `hard_blockers`, not here."
        )
    )
    candidate_evidence: str | None = Field(
        default=None,
        description="What in the profile shows this, or null if nothing does.",
    )

    @field_validator("status", mode="before")
    @classmethod
    def _a_blocking_requirement_is_unmet(cls, value: Any) -> Any:
        """Take `blocked`, which is `eligibility`'s vocabulary leaking one level down.

        This was the single most expensive rejection in the backlog run: 126 calls and 26
        of the 38 postings that ended with no verdict at all. The field had no
        `description` and the rules never name its three values, while `blocked` appears
        throughout them for `eligibility` -- so when a must_have was also the hard blocker
        the model reached for the word it had been given.

        Retrying could not clear it. Scoring runs at `temperature=0` with thinking off, so
        the second and third attempts are the same draw at the same conditions: 41% of
        these cleared on retry and then 30% of the remainder, which is noise rather than
        correction.

        `unmet` loses nothing. A must_have the candidate is disqualified on is unmet by
        definition, and `hard_blockers` already carries the fact that it disqualifies.
        Nothing downstream reads this field for the number -- `scoring/scale.py` projects
        the score from the five ordinals -- so no stored score can move.
        """
        if isinstance(value, str) and value.strip().casefold() == "blocked":
            return "unmet"
        return value


# Why HardBlocker has two fields. This is a comment rather than a docstring because a
# model docstring is sent to the model as the schema description on every call, and none of
# the following is any use to it.
#
# `hard_blockers` was a list of strings, each of which had to be the evidence AND the
# explanation at once. The model resolved that conflict by writing the explanation, and the
# auditor -- which can only check the evidence -- discarded the verdict. Of the 33 distinct
# blockers that lost a verdict in one run, 19 were prose citing the candidate's *own profile
# constraint*: "the candidate's NON-NEGOTIABLE constraint is 'Military technology / defense
# contractors'", which is not in the posting and can never be located there. Another five
# were role-category judgements ("this is a building maintenance role, not a software
# role") with the same shape. All were correct, thrown away, and re-scored from scratch on
# the next run at full price.
#
# Splitting the field fixes it structurally rather than by asking the model more nicely:
# `quote` is the half that is checkable and `why` is somewhere for the reasoning to live
# that is not the evidence. `CoreRequirement` already worked this way; this is the same
# treatment applied where it was missing.
class HardBlocker(BaseModel):
    """One disqualifying requirement: the words from the posting, and why they disqualify.

    `scoring/audit.py` checks `quote` against the posting and ignores `why`, so a blocker
    the model invented is detectable while its reasoning stays free to talk about the
    candidate.
    """

    quote: str = Field(
        description=(
            "Words copied from the posting: the requirement, the company name or the job "
            "title, whichever one carries the disqualification. Copy them exactly -- do "
            "not summarise, translate, or explain in this field. If nothing in the "
            "posting can be copied here, it is not a hard blocker."
        )
    )
    why: str = Field(
        description=(
            "One sentence on why that phrase disqualifies THIS candidate: the constraint "
            "or gap it collides with. All of the reasoning goes here, none of it in "
            "`quote`."
        )
    )


class FitAssessment(BaseModel):
    """What the scoring agent returns. It answers questions; it does not produce a score.

    FIELD ORDER IS THE REASONING ORDER. Function calling fills fields in declaration
    order, so the model reads the posting, names what it is, extracts the requirements and
    checks them off *before* it is asked for a judgement. Reordering these silently
    changes what each answer is conditioned on, which is why `test_scoring_module.py`
    pins the sequence.

    The number lives in `scoring/scale.py` and is computed from the five ordinals. The
    previous schema asked the model for `fit_score: int` directly and got 53 distinct
    values across 5,511 verdicts, 99.84% of which simply re-encoded the band label.
    """

    role_summary: str = Field(
        description=(
            "One line: what this job actually is. Name the product domain, not just the "
            "job title -- 'QA for warehouse robotics', not 'QA engineer'."
        )
    )
    core_requirements: list[CoreRequirement] = Field(
        description=(
            "What the posting genuinely requires, quoted. Job ads pad their requirements "
            "sections; a skill named once in a wish-list is not the job. Five to eight is "
            "usual."
        )
    )
    requirement_assessments: list[RequirementAssessment] = Field(
        description="One entry per requirement above. Every must_have needs one."
    )

    eligibility: Literal["eligible", "conditional", "blocked"] = Field(
        description=rubric.render_scale("eligibility")
    )
    role_match: Literal["same_role", "adjacent", "different_domain", "different_field"] = Field(
        description=rubric.render_scale("role_match")
    )
    capability_match: Literal["exceeds", "meets", "most_with_gaps", "major_gaps", "not_close"] = (
        Field(description=rubric.render_scale("capability_match"))
    )
    seniority_gap: Literal["matched", "candidate_above", "candidate_below"] = Field(
        description=rubric.render_scale("seniority_gap")
    )
    evidence_quality: Literal["strong", "adequate", "thin"] = Field(
        description=rubric.render_scale("evidence_quality")
    )

    hard_blockers: list[HardBlocker] = Field(
        default_factory=list,
        description=(
            "Why eligibility is not `eligible`, one entry per disqualifying requirement. "
            "Empty when eligible."
        ),
    )
    key_gaps: list[str] = Field(
        default_factory=list, description="Missing but learnable or negotiable."
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="Where this candidate is a strong answer to the posting.",
    )
    reasoning: str = Field(description="Two or three sentences. Blunt and specific.")
    research_worthy: bool = Field(description="Whether the company is worth a deep-research pass.")

    _accept_a_json_encoded_list = field_validator("key_gaps", "strengths", mode="before")(
        FitVerdict._accept_a_json_encoded_list.__func__
    )

    @field_validator("hard_blockers", mode="before")
    @classmethod
    def _accept_a_blocker_written_as_a_string(cls, value: Any) -> Any:
        """Take the old flat shape and the JSON-encoded-list shape both.

        Two callers need this. The model still occasionally emits a bare string where the
        object belongs, and refusing costs a whole retry for something with an obvious
        reading -- the same argument `_accept_a_json_encoded_list` makes. And verdicts
        written before this field had a shape are stored flat; migration v7 converts them,
        but nothing guarantees a database has been through it before a row is parsed.

        A string becomes the quote with an empty `why`, which is exactly what it meant:
        the old field's whole contract was "quote the phrase from the posting".
        """
        value = FitVerdict._accept_a_json_encoded_list.__func__(cls, value)
        if not isinstance(value, list):
            return value
        return [{"quote": item, "why": ""} if isinstance(item, str) else item for item in value]

    @model_validator(mode="after")
    def _repair_and_check(self):
        """Repair what is mechanically recoverable; raise only where judgement is missing.

        A raise here surfaces as `parsing_error` through `with_structured_output(...,
        include_raw=True)`, which routes to the scoring graph's existing retry edge. That
        costs a whole extra call, so it is reserved for cases where there is nothing to
        repair from -- following the precedent set by `_accept_a_json_encoded_list`, which
        decodes what the model plainly meant rather than buying another round trip.

        The verbatim-quote check is NOT here. It needs the posting text, and LangChain
        builds the parser itself and calls `model_validate` with no context to pass it
        through. It lives in `scoring/audit.py`, which runs post-parse in `node_score`.
        """
        if not self.core_requirements:
            raise ValueError(
                "core_requirements is empty: the extraction step was skipped, so the "
                "ordinals were guessed rather than derived."
            )

        if self.eligibility != "eligible" and not self.hard_blockers:
            raise ValueError(
                f"eligibility is '{self.eligibility}' but hard_blockers is empty. A "
                "verdict that disqualifies without naming what disqualified is not "
                "actionable."
            )

        # An unassessed must_have used to raise here. It no longer does. The rule was
        # right and the enforcement was in the wrong place: 89 of 95 rejections named ONE
        # requirement out of the five to eight extracted, and 94% cleared on the first
        # retry -- the model had assessed it and lost a word re-typing an 89-character
        # string. That is a full extra call to buy back punctuation.
        #
        # The gap is now recorded instead of refused. `scoring/audit.py` already flags
        # every unassessed requirement as `assessment_incomplete`; the flag never fired
        # for a must_have only because this raise ran first. Everything downstream was
        # already built for the state: `core/database._requirement_summary` buckets it to
        # `unassessed` and `macros/badges.html` renders the count on the card. So the
        # reader still sees the gap, on a verdict that survived.
        if self.capability_match == "exceeds":
            unmet = {
                normalize_requirement(a.requirement)
                for a in self.requirement_assessments
                if a.status == "unmet"
            }
            must = {
                normalize_requirement(r.requirement)
                for r in self.core_requirements
                if r.importance == "must_have"
            }
            if unmet & must:
                raise ValueError(
                    "capability_match is 'exceeds' while a must_have is unmet -- these "
                    "cannot both be true."
                )

        # Repairs. One-bit fixes that a retry would only buy back at the price of a call.
        if self.eligibility == "blocked" and self.research_worthy:
            self.research_worthy = False

        return self

    def ordinals(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in rubric.DIMENSIONS}
