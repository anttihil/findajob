"""Shapes for the company dossier.

Every field here is meant to answer a question the candidate would otherwise have to open
ten browser tabs to answer before applying: is this company real, is it growing, what do
they actually build, and who would I be talking to.
"""

from pydantic import BaseModel, Field


class CompanyIntel(BaseModel):
    summary: str = Field(description="Two or three sentences on what the company does.")
    size: str | None = Field(default=None, description="Headcount or a range.")
    stage: str | None = Field(default=None, description="Funding stage or public/private status.")
    funding: str | None = Field(default=None, description="Most recent round and date.")
    tech_stack: list[str] = Field(
        default_factory=list, description="Technologies they are known to use."
    )
    recent_news: list[str] = Field(
        default_factory=list, description="Notable events in the last year or so."
    )
    engineering_culture: str | None = Field(
        default=None, description="What is publicly known about how they build."
    )
    reputation: str | None = Field(
        default=None, description="Employee sentiment, if there is a credible signal."
    )
    concerns: list[str] = Field(
        default_factory=list,
        description="Layoffs, funding trouble, poor reviews, anything a candidate should weigh.",
    )


class Contact(BaseModel):
    """A publicly listed person, with a public route to reach them.

    Strictly what the company or the person has already published: team pages, engineering
    blog bylines, conference talks, public profile URLs. No inferred email addresses, no
    enrichment lookups, no gated data -- the point is to tell the candidate who to address
    a message to, not to assemble a contact database.
    """

    name: str
    role: str | None = None
    relevance: str = Field(description="Why this person is worth contacting for this role.")
    public_url: str | None = Field(
        default=None, description="Where this person was found. Must be a public page."
    )


class NearbyJob(BaseModel):
    title: str
    company: str
    location: str | None = None
    url: str | None = None
    source: str = Field(description="'same-company', 'nearby-company', or 'careers-page'")
    why: str | None = Field(default=None, description="Why it is worth a look.")


class Dossier(BaseModel):
    company: str
    intel: CompanyIntel
    contacts: list[Contact] = Field(default_factory=list)
    nearby_jobs: list[NearbyJob] = Field(default_factory=list)
    application_angle: str | None = Field(
        default=None,
        description=(
            "Given this candidate's profile and what was learned, the single most useful "
            "thing to lead with in an application."
        ),
    )
    sources: list[str] = Field(default_factory=list, description="URLs backing the above.")
