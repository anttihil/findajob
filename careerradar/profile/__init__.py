"""Unified candidate profile, scoring adaptation, and tailored resume generation."""

from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.models import (
    MasterEducation,
    MasterProfile,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
    RoleTargeting,
    TailoredResumePayload,
    WorkEligibility,
)
from careerradar.profile.repository import (
    get_latest_tailored_resume,
    get_resume_by_id,
    list_tailored_resumes,
    load_profile,
    save_profile,
    save_tailored_resume,
)

__all__ = [
    "MasterEducation",
    "MasterProfile",
    "MasterProject",
    "MasterRole",
    "MasterSkillCategory",
    "Profile",
    "ProfileAdapter",
    "RoleTargeting",
    "TailoredResumePayload",
    "WorkEligibility",
    "get_latest_tailored_resume",
    "get_resume_by_id",
    "list_tailored_resumes",
    "load_profile",
    "save_profile",
    "save_tailored_resume",
]
