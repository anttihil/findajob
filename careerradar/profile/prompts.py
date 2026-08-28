"""Prompts and formatting helpers for the Resume Builder and ATS Screener."""

import json
from typing import Any

from careerradar.profile.models import Profile, TailoredResumePayload

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
     - For major roles, provide 1-2 distinct project subheadings with 2-3 high-impact bullets each.
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


def render_master_profile_context(profile: Profile) -> str:
    """Render the master profile into prompt context."""
    parts: list[str] = [
        f"CANDIDATE NAME: {profile.name}",
        f"LOCATION: {profile.location}",
        f"EMAIL: {profile.email} | PHONE: {profile.phone}",
        f"GITHUB: {profile.github} | LINKEDIN: {profile.linkedin}",
    ]
    if profile.website:
        parts.append(f"WEBSITE: {profile.website}")

    exec_summary = profile.executive_summary or profile.summary_guidance
    if exec_summary:
        parts.append(f"\nBASE EXECUTIVE SUMMARY (SALES PITCH TEMPLATE):\n{exec_summary}")

    if profile.model_guidance:
        parts.append(f"\nAI GUIDANCE / TAILORING DIRECTIVES:\n{profile.model_guidance}")

    parts.append("\n--- MASTER EXPERIENCE (ROLES & EMPLOYMENT) ---")
    for role in profile.experience:
        loc_str = f" ({role.location})" if role.location else ""
        parts.append(f"\nROLE: {role.title} at {role.company}{loc_str} [{role.dates}]")
        for proj in role.projects:
            proj_title = f"Project: {proj.name}" if proj.name else "Project"
            if proj.heading:
                proj_title += f" - {proj.heading}"
            parts.append(f"  {proj_title}")
            for b in proj.bullets:
                parts.append(f"    * {b}")

    if profile.projects:
        parts.append("\n--- STANDALONE / PERSONAL / OPEN SOURCE PROJECTS ---")
        for proj in profile.projects:
            url_str = f" ({proj.url})" if proj.url else ""
            proj_title = f"PROJECT: {proj.name}{url_str}"
            if proj.heading:
                proj_title += f" - {proj.heading}"
            parts.append(f"\n{proj_title}")
            for b in proj.bullets:
                parts.append(f"  * {b}")

    parts.append("\n--- MASTER SKILLS ---")
    for cat in profile.skills:
        skills_str = ", ".join(cat.skills)
        parts.append(f"{cat.category}: {skills_str}")

    parts.append("\n--- MASTER EDUCATION ---")
    for edu in profile.education:
        detail_str = f" ({edu.details})" if edu.details else ""
        parts.append(f"{edu.institution}, {edu.degree}{detail_str}")

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
        f"KEYWORD / SKILL SIGNALS: {', '.join(matched_skills)}",
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
