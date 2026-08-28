from typing import Any

from pydantic import BaseModel, Field


class MasterEducation(BaseModel):
    institution: str
    degree: str
    details: str | None = ""


class MasterSkillCategory(BaseModel):
    category: str
    skills: list[str] = Field(default_factory=list)


class MasterProject(BaseModel):
    name: str
    heading: str = ""
    bullets: list[str] = Field(default_factory=list)


class MasterRole(BaseModel):
    title: str
    company: str
    dates: str
    location: str | None = ""
    projects: list[MasterProject] = Field(default_factory=list)


class ResumeMasterProfile(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    github: str = ""
    linkedin: str = ""
    website: str = ""
    summary_guidance: str = ""
    seniority: str | None = "Mid / Senior"
    years_experience: float | None = 4.0
    citizenship: list[str] = Field(default_factory=lambda: ["Authorized to work in US"])
    locations: list[str] = Field(default_factory=lambda: ["Remote"])
    willing_to_relocate: bool = False
    comp_floor_usd: int | None = 90000
    target_roles: list[str] = Field(
        default_factory=lambda: [
            "Software Engineer",
            "Platform Engineer",
            "Full-Stack Engineer",
        ]
    )
    work_modes: list[str] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    target_industries: list[str] = Field(
        default_factory=lambda: [
            "Cloud Infrastructure",
            "Developer Tools",
            "AI / ML Applications",
        ]
    )
    dealbreakers: list[str] = Field(
        default_factory=lambda: [
            "24-hour on-call site reliability rotations",
            "No remote flexibility",
        ]
    )
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    education: list[MasterEducation] = Field(default_factory=list)
    skills: list[MasterSkillCategory] = Field(default_factory=list)
    skill_ratings: list[dict[str, Any]] = Field(default_factory=list)
    experience: list[MasterRole] = Field(default_factory=list)
    raw_achievements_md: str | None = None


class ResumeSubsection(BaseModel):
    heading: str | None = Field(
        default=None,
        description="Italic project/scope heading, e.g. 'Designed replacement for 20 sites:'",
    )
    bullets: list[str] = Field(
        min_length=1,
        max_length=5,
        description="Action-oriented impact bullet points, each max 160 characters.",
    )


class ResumeRole(BaseModel):
    title: str = Field(description="Job title, e.g. 'Software Engineer'")
    company: str = Field(description="Organization or company name")
    dates: str = Field(description="Date range, e.g. 'Jan 2025 - present'")
    subsections: list[ResumeSubsection] = Field(default_factory=list)


class ResumeSkillCategory(BaseModel):
    category: str = Field(description="Category name, e.g. 'Infrastructure'")
    skills: str = Field(description="Comma-separated skills string")


class ResumeEducation(BaseModel):
    institution: str = Field(description="e.g. 'UCLA'")
    degree: str = Field(description="e.g. 'PhD in Philosophy (2019); MA in Philosophy'")


class TailoredResumePayload(BaseModel):
    name: str
    contact_line_1: str = Field(description="Contact line 1, e.g. Location | Email | Phone")
    contact_line_2: str = Field(description="Contact line 2, e.g. GitHub | LinkedIn | Website")
    summary: str = Field(description="2-3 sentence tailored executive summary (200-320 characters)")
    experience: list[ResumeRole] = Field(default_factory=list)
    skills: list[ResumeSkillCategory] = Field(default_factory=list)
    education: list[ResumeEducation] = Field(default_factory=list)


class ATSScreeningVerdict(BaseModel):
    passed: bool = Field(
        description="True if candidate meets core bar to pass ATS screen for this role."
    )
    score: int = Field(ge=1, le=10, description="ATS Match score from 1 to 10.")
    strengths: list[str] = Field(
        default_factory=list, description="Matching qualifications and strong signals."
    )
    missing_signals: list[str] = Field(
        default_factory=list, description="Important requirements that are weak or missing."
    )
    actionable_feedback: str = Field(
        default="", description="Guidance for generator on how to frame/prioritize achievements."
    )


class LayoutValidationResult(BaseModel):
    is_valid: bool
    estimated_points: float
    max_points: float = 695.0
    violations: list[str] = Field(default_factory=list)
