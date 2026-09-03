"""Prompts and formatting helpers for the Resume Builder and ATS Screener."""

import json
from typing import Any

from careerradar.profile.models import Profile, TailoredResumePayload
from careerradar.profile.render import render_profile_for_resume

GENERATOR_SYSTEM_PROMPT = """You are an expert technical resume strategist.
Generate a concise, tailored 1-page resume for the target job posting based strictly on the
candidate's provided master profile.

CRITICAL RULES:
1. STRICT TRUTHFULNESS & ZERO HALLUCINATION:
   - Select and frame achievements ONLY from the candidate's master profile.
   - NEVER fabricate employers, dates, metrics, degrees, or technologies not in the source data.
   - Rephrase and emphasize real achievements to highlight alignment with the target role and
     required tech stack.

2. 1-PAGE LAYOUT CONSTRAINTS:
   - Summary: Exactly 2 to 3 sentences (200-320 characters) focusing on key differentiators.
   - Experience:
     - Every role MUST include accomplishment bullet points in `subsections`.
     - For major roles with multiple distinct projects, you may provide 1-2 project
       subheadings with 2-3 high-impact bullets each.
     - For roles without distinct projects, provide a single subsection with heading=null and
       2-3 bullets.
     - Each bullet must be 100 to 160 characters (action verb + what was built/solved +
       quantified impact/technologies).
     - Total bullets across the entire resume must be between 10 and 13 bullets.
   - Skills: Group into 3 to 4 logical categories prioritizing matched technologies from the job.
"""


SCREENER_SYSTEM_PROMPT = """You are an automated Applicant Tracking System (ATS) screener.
Evaluate whether the candidate's 1-page resume demonstrates a strong match for the job
description's requirements, seniority, and tech stack.

Be rigorous:
- If critical required skills or core domains in the JD are absent or weakly mentioned in the
  resume, flag them.
- Score the candidate from 1 to 10 based on qualification match.
- Set passed = True if score >= 9, otherwise passed = False.
- Provide concise, actionable feedback on what signals are missing or under-emphasized.
"""


# Backward compatibility alias
render_master_profile_context = render_profile_for_resume


def build_generator_system(profile: Profile) -> str:
    """The cached half for resume generation. Byte-identical across all target jobs."""
    candidate_ctx = render_profile_for_resume(profile)
    return (
        f"{GENERATOR_SYSTEM_PROMPT}\n\n"
        f"=== CANDIDATE MASTER PROFILE (GROUND TRUTH) ===\n"
        f"{candidate_ctx}"
    )


def render_generator_user(job: dict[str, Any], feedback: str | None = None) -> str:
    """The volatile half for resume generation. One target job + optional retry critique."""
    job_ctx = render_job_context(job)
    parts = [
        "=== TARGET JOB DETAILS ===",
        job_ctx,
        "\nGenerate the tailored 1-page resume for this target job.",
    ]
    if feedback:
        parts.append(f"\n=== FEEDBACK FROM PREVIOUS ATTEMPT (PLEASE RESOLVE) ===\n{feedback}")
    return "\n".join(parts)


def render_job_context(job: dict[str, Any]) -> str:
    """Render the target job posting into prompt context."""
    title = job.get("title") or "Software Engineer"
    company = job.get("company") or "Unknown Company"
    location = job.get("location") or job.get("country") or ""
    role_family = job.get("role_family") or ""
    seniority = job.get("seniority") or ""
    matched_skills = job.get("matched_skills") or []
    if isinstance(matched_skills, str):
        try:
            matched_skills = json.loads(matched_skills)
        except (json.JSONDecodeError, TypeError):
            matched_skills = [matched_skills]
    desc = job.get("description") or "No description provided."

    parts = [
        f"TARGET JOB: {title}",
        f"COMPANY: {company}",
        f"LOCATION: {location}",
        f"ROLE FAMILY: {role_family} | SENIORITY: {seniority}",
        f"MATCHED SKILLS: {', '.join(matched_skills)}",
        "\n--- JOB DESCRIPTION ---",
        desc,
    ]
    return "\n".join(parts)


def render_resume_plaintext(payload: TailoredResumePayload) -> str:
    """Render the tailored resume as clean plaintext for the ATS Blind Screener."""
    lines: list[str] = [
        payload.name.upper(),
        payload.contact_line_1,
        payload.contact_line_2,
        "",
        "SUMMARY",
        payload.summary,
        "",
        "EXPERIENCE",
    ]

    for role in payload.experience:
        lines.append(f"{role.title}, {role.company} \t {role.dates}")
        for sub in role.subsections:
            if sub.heading:
                lines.append(f"  {sub.heading}")
            for bullet in sub.bullets:
                lines.append(f"  * {bullet}")
        lines.append("")

    lines.append("SKILLS")
    for cat in payload.skills:
        lines.append(f"{cat.category}: {cat.skills}")
    lines.append("")

    lines.append("EDUCATION")
    for edu in payload.education:
        lines.append(f"{edu.institution}, {edu.degree}")

    return "\n".join(lines)
