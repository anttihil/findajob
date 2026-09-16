"""Unified profile rendering subsystem for scoring prompt caching and resume generation.

Provides deterministic, byte-stable text representations of a candidate Profile.
- `render_profile_for_scoring`: Prefix-cached system prompt context for scoring.
- `render_profile_for_resume`: Prefix-cached candidate context for resume generation.
"""

from findajob.profile.models import Profile


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  - {item}" for item in items)


def _render_skills_block(profile: Profile) -> list[str]:
    """Render categorized skills sorted alphabetically for byte-stability."""
    if not profile.skills:
        return []
    lines = ["SKILLS:"]
    for cat in sorted(profile.skills, key=lambda c: c.category.lower()):
        clean_skills = [s.strip() for s in cat.skills if s.strip()]
        if clean_skills:
            lines.append(f"  - {cat.category}: {', '.join(clean_skills)}")
    return lines


def _render_experience_and_projects_block(profile: Profile) -> list[str]:
    """Render work experience and standalone projects ground truth."""
    project_lines: list[str] = []
    for role in profile.experience:
        if not role.projects:
            loc_str = f" ({role.location})" if role.location else ""
            dates_str = f" [{role.dates}]" if role.dates else ""
            project_lines.append(f"  - {role.title} at {role.company}{loc_str}{dates_str}")
        for proj in role.projects:
            if proj.heading and proj.heading.strip():
                desc = f"{role.title} at {role.company}: {proj.heading.strip()}"
            else:
                desc = f"{role.title} at {role.company}"
            project_lines.append(f"  - {desc}")
            for bullet in proj.bullets:
                clean_b = bullet.strip()
                if clean_b:
                    project_lines.append(f"    * {clean_b}")

    for proj in profile.projects:
        desc = (
            f"Project: {proj.heading.strip()}"
            if (proj.heading and proj.heading.strip())
            else "Project"
        )
        if proj.url:
            desc += f" ({proj.url})"
        project_lines.append(f"  - {desc}")
        for bullet in proj.bullets:
            clean_b = bullet.strip()
            if clean_b:
                project_lines.append(f"    * {clean_b}")

    if not project_lines:
        return []
    return ["EXPERIENCE:", *project_lines]


def _render_education_block(profile: Profile) -> list[str]:
    """Render education history."""
    if not profile.education:
        return []
    edu_lines = [
        f"{e.degree} - {e.institution}" + (f" ({e.details})" if e.details else "")
        for e in profile.education
    ]
    return ["EDUCATION:", _bullets(edu_lines)]


def render_profile_for_scoring(profile: Profile) -> str:
    """Produce the deterministic stable prompt prefix for candidate scoring against jobs.

    Computed once at profile-save time and stored in `profile.summary_text`.
    DeepSeek caches this byte-identical prefix automatically.
    """
    bio_text = (
        profile.executive_summary.strip()
        or profile.summary_guidance.strip()
        or "Software engineer and platform builder."
    )
    lines = ["CANDIDATE PROFILE", "", bio_text, ""]

    facts: list[str] = []
    if profile.years_experience is not None:
        facts.append(f"Experience: {profile.years_experience:g} years")
    if profile.seniority:
        facts.append(f"Seniority: {profile.seniority}")
    if facts:
        lines.extend([*facts, ""])

    skills_lines = _render_skills_block(profile)
    if skills_lines:
        lines.extend([*skills_lines, ""])

    exp_lines = _render_experience_and_projects_block(profile)
    if exp_lines:
        lines.extend([*exp_lines, ""])

    edu_lines = _render_education_block(profile)
    if edu_lines:
        lines.extend([*edu_lines, ""])

    # Work Eligibility & Constraints
    eligibility = profile.eligibility
    eligibility_lines: list[str] = []
    if eligibility.citizenship:
        eligibility_lines.append(
            f"Authorized to work in: {', '.join(sorted(eligibility.citizenship))}"
        )
    if eligibility.locations:
        eligibility_lines.append(f"Will work from: {', '.join(sorted(eligibility.locations))}")
    if eligibility.willing_to_relocate is not None:
        eligibility_lines.append(
            f"Willing to relocate: {'yes' if eligibility.willing_to_relocate else 'no'}"
        )
    if eligibility.comp_floor_usd:
        eligibility_lines.append(f"Comp floor: ${eligibility.comp_floor_usd:,} base")
    if eligibility_lines:
        lines.extend(["ELIGIBILITY:", _bullets(eligibility_lines), ""])

    # Positioning & AI Strategic Guidance
    if profile.model_guidance and profile.model_guidance.strip():
        lines.extend(
            [
                "POSITIONING & STRATEGIC DIRECTIVES:",
                f"  {profile.model_guidance.strip()}",
                "",
            ]
        )

    # Non-negotiable Dealbreakers
    dealbreakers = profile.dealbreakers or profile.targeting.dealbreakers
    if dealbreakers:
        lines.extend(
            [
                "NON-NEGOTIABLE DEALBREAKERS:",
                _bullets(dealbreakers),
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def render_profile_for_resume(profile: Profile) -> str:
    """Render the candidate master profile into clean ground-truth context for resume tailoring.

    Contains full candidate background, project pool, skills, and model guidance.
    Excludes private constraints (dealbreakers, salary floor) which are not resume content.
    """
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
        parts.append(f"\nEXECUTIVE SUMMARY:\n{exec_summary}")

    if profile.model_guidance:
        parts.append(f"\nMODEL GUIDANCE:\n{profile.model_guidance}")

    parts.append("\n--- EXPERIENCE ---")
    for role in profile.experience:
        loc_str = f" ({role.location})" if role.location else ""
        parts.append(f"\nROLE: {role.title} at {role.company}{loc_str} [{role.dates}]")
        for proj in role.projects:
            if proj.heading and proj.heading.strip():
                parts.append(f"  Scope / Project: {proj.heading.strip()}")
            for b in proj.bullets:
                parts.append(f"    * {b}")

    if profile.projects:
        parts.append("\n--- PROJECTS ---")
        for proj in profile.projects:
            url_str = f" ({proj.url})" if proj.url else ""
            if proj.heading and proj.heading.strip():
                parts.append(f"\nPROJECT: {proj.heading.strip()}{url_str}")
            else:
                parts.append(f"\nPROJECT{url_str}")
            for b in proj.bullets:
                parts.append(f"  * {b}")

    parts.append("\n--- SKILLS ---")
    for cat in sorted(profile.skills, key=lambda c: c.category.lower()):
        skills_str = ", ".join(cat.skills)
        parts.append(f"{cat.category}: {skills_str}")

    parts.append("\n--- EDUCATION ---")
    for edu in profile.education:
        detail_str = f" ({edu.details})" if edu.details else ""
        parts.append(f"{edu.institution}, {edu.degree}{detail_str}")

    return "\n".join(parts)


# Backward compatibility aliases
render_profile = render_profile_for_scoring
render_master_profile_context = render_profile_for_resume
