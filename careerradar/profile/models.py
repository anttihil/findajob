"""Unified domain models for candidate Profile, scoring, and tailored resume generation."""

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# --- Core Profile Shapes (The Single Source of Truth) -----------------------------------


class MasterEducation(BaseModel):
    institution: str = Field(description="e.g. 'State University'")
    degree: str = Field(description="e.g. 'BS in Computer Science'")
    details: str | None = ""


class MasterSkillCategory(BaseModel):
    category: str = Field(description="e.g. 'Infrastructure', 'AI Systems', 'Frontend'")
    skills: list[str] = Field(default_factory=list, description="List of skills and tools")


class MasterProject(BaseModel):
    name: str = Field(description="Project or system name")
    heading: str = Field(default="", description="Scope description or role summary")
    bullets: list[str] = Field(
        default_factory=list,
        description="Quantified impact bullet points demonstrating skills",
    )


class MasterRole(BaseModel):
    title: str = Field(description="Job title, e.g. 'Software Engineer'")
    company: str = Field(description="Organization or company name")
    dates: str = Field(description="Date range, e.g. 'Jan 2023 - Present'")
    location: str | None = ""
    projects: list[MasterProject] = Field(default_factory=list)


class WorkEligibility(BaseModel):
    """Work authorization and mobility attributes that open doors."""

    citizenship: list[str] = Field(
        default_factory=lambda: ["Authorized to work in US"],
        description="Work authorization / citizenship status",
    )
    locations: list[str] = Field(
        default_factory=lambda: ["Remote"],
        description="Locations where candidate is available to work",
    )
    willing_to_relocate: bool = Field(
        default=False, description="Whether candidate is open to relocation"
    )
    comp_floor_usd: int | None = Field(
        default=90000, description="Annual base compensation floor in USD"
    )


class RoleTargeting(BaseModel):
    """Desired roles and explicit disqualifiers."""

    target_roles: list[str] = Field(
        default_factory=lambda: ["Software Engineer", "Platform Engineer"],
        description="Desired role titles / families",
    )
    work_modes: list[str] = Field(
        default_factory=lambda: ["remote", "hybrid", "onsite"],
        description="Acceptable work modes (remote / hybrid / onsite)",
    )
    target_industries: list[str] = Field(
        default_factory=lambda: ["Cloud Infrastructure", "Developer Tools"],
        description="Preferred industry domains",
    )
    dealbreakers: list[str] = Field(
        default_factory=lambda: ["24-hour on-call site reliability rotations"],
        description="Hard dealbreakers that make a posting an automatic no",
    )


class Profile(BaseModel):
    """The single canonical candidate profile powering scoring and resumes."""

    # 1. Personal & Contact Info
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    github: str = ""
    linkedin: str = ""
    website: str = ""

    # 2. Work Eligibility & Availability
    eligibility: WorkEligibility = Field(default_factory=WorkEligibility)

    # 3. Positioning & Level
    summary_guidance: str = ""
    seniority: str | None = "Mid / Senior"
    years_experience: float | None = 4.0

    # 4. Experience Pool & Projects (Ground-Truth Evidence)
    experience: list[MasterRole] = Field(default_factory=list)

    # 5. Skills & Categorized Tools
    skills: list[MasterSkillCategory] = Field(default_factory=list)

    # 6. Role Targeting & Boundaries
    targeting: RoleTargeting = Field(default_factory=RoleTargeting)

    # Education History
    education: list[MasterEducation] = Field(default_factory=list)


# Aliases for backward compatibility during transition
MasterProfile = Profile
ResumeMasterProfile = Profile


# --- Profile Adapter Helpers (Keyword Scoring & Gap Analysis) ---------------------------

LEVEL_STRONG = 3
LEVEL_CLAIMED = 2
LEVEL_MENTIONED = 1


class Skill(BaseModel):
    key: str = Field(description="snake_case canonical key, e.g. 'kubernetes'")
    label: str = Field(description="Human-readable name, e.g. 'Kubernetes'")
    level: int = Field(ge=0, le=3, description="0 to 3 proficiency rating")
    evidence: str = Field(default="", description="Bullet point evidence")
    recency: str | None = None


# --- Tailored 1-Page Resume Shapes ------------------------------------------------------


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
    institution: str = Field(description="e.g. 'State University'")
    degree: str = Field(description="e.g. 'BS in Computer Science'")


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
        default="",
        description="Guidance for generator on how to frame/prioritize achievements.",
    )


class LayoutValidationResult(BaseModel):
    is_valid: bool
    estimated_points: float
    max_points: float = 695.0
    violations: list[str] = Field(default_factory=list)


# --- Simplified Scoring Verdict ---------------------------------------------------------

VERDICT_SCHEMA_VERSION = 3


class JobFitVerdict(BaseModel):
    """The scoring agent's simplified output for one posting."""

    fit: bool = Field(
        description="True if the candidate fits this job at >= target match threshold, else False."
    )
    reason_type: str = Field(
        description="Single lowercase word: 'match', 'skills', 'experience', 'seniority', etc."
    )
    reason_description: str = Field(
        description="Concise 1-2 sentence explanation of why the candidate fits or does not fit."
    )

    @field_validator("reason_type", mode="before")
    @classmethod
    def _clean_reason_type(cls, value: Any) -> str:
        if not isinstance(value, str):
            return "unknown"
        cleaned = re.sub(r"[^a-z0-9_-]", "", value.strip().lower())
        return cleaned or "unknown"
