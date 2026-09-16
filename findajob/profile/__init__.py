"""Unified candidate profile, scoring adaptation, and tailored resume generation."""

from findajob.profile.adapter import ProfileAdapter
from findajob.profile.models import (
    DEFAULT_PROFILE_VERSION,
    MASTER_PROFILE_ID,
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
from findajob.profile.repository import (
    get_latest_tailored_resume,
    get_resume_by_id,
    list_tailored_resumes,
    load_profile,
    save_profile,
    save_tailored_resume,
)

__all__ = [
    "DEFAULT_PROFILE_VERSION",
    "MASTER_PROFILE_ID",
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
