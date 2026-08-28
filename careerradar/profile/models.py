"""Unified domain models for candidate Profile, scoring, and tailored resume generation."""

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# --- Core Profile Shapes (The Single Source of Truth) -----------------------------------


class MasterEducation(BaseModel):
    institution: str = Field(description="e.g. 'State University'")
    degree: str = Field(description="e.g. 'BS in Computer Science'")
    details: str | None = ""


class MasterSkillCategory(BaseModel):
    category: str = Field(description="e.g. 'Infrastructure', 'AI Systems', 'Frontend'")
    skills: list[str] = Field(default_factory=list, description="List of skills and tools")


class MasterProject(BaseModel):
    heading: str | None = Field(
        default=None,
        description="Optional project/scope heading, e.g. 'Designed replacement for 20 sites:'",
    )
    url: str | None = Field(default="", description="Project repository or live URL")
    bullets: list[str] = Field(
        default_factory=list,
        description="Quantified impact bullet points demonstrating skills",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and "heading" not in data and "name" in data:
            data["heading"] = data.get("name")
        return data

    @property
    def name(self) -> str:
        return self.heading or ""


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

    # 3. Positioning & AI Guidance
    seniority: str | None = "Mid / Senior"
    years_experience: float | None = 4.0

    # Recruiter-facing sales pitch (source template for resume summary)
    executive_summary: str = Field(
        default="",
        description="2-3 sentence elevator pitch / sales summary for the resume header.",
    )

    # Internal AI instructions (used by Scoring Agent and Generator Agent)
    model_guidance: str = Field(
        default="",
        description=(
            "Freeform strategic directives for LLM scoring and tailoring (not printed on resume)."
        ),
    )

    # Hard vetoes for scoring
    dealbreakers: list[str] = Field(
        default_factory=lambda: ["24-hour on-call site reliability rotations"],
        description="Hard disqualifiers that trigger an immediate fit: false verdict.",
    )

    # 4. Experience Pool (Work roles at organizations)
    experience: list[MasterRole] = Field(default_factory=list)

    # 5. Standalone Personal / Open Source Projects
    projects: list[MasterProject] = Field(default_factory=list)

    # 6. Skills & Categorized Tools
    skills: list[MasterSkillCategory] = Field(default_factory=list)

    # 7. Education History
    education: list[MasterEducation] = Field(default_factory=list)

    # Backward compatibility shims for transitions
    summary_guidance: str = ""
    targeting: RoleTargeting = Field(default_factory=RoleTargeting)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Fallback for summary_guidance -> executive_summary
            if not data.get("executive_summary") and data.get("summary_guidance"):
                data["executive_summary"] = data["summary_guidance"]
            # Fallback for targeting.dealbreakers -> dealbreakers
            if "dealbreakers" not in data and "targeting" in data:
                targ = data["targeting"]
                if isinstance(targ, RoleTargeting):
                    data["dealbreakers"] = targ.dealbreakers
                elif isinstance(targ, dict) and "dealbreakers" in targ:
                    data["dealbreakers"] = targ["dealbreakers"]
        return data

    @field_validator("executive_summary", mode="before")
    @classmethod
    def _fallback_exec_summary(cls, v: Any) -> Any:
        return v or ""


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
