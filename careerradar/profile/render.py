"""Render a Profile into the prompt prefix the scoring agent sends on every call.

This text is computed **once**, at profile-save time, and stored in `profile.summary_text`.
It is never rebuilt per request. DeepSeek caches prompt prefixes automatically.
"""

from careerradar.profile.models import Profile


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  - {item}" for item in items)


def render_profile(profile: Profile) -> str:
    """Produce the deterministic stable prompt prefix for one profile."""
    bio_text = (
        profile.executive_summary.strip()
        or profile.summary_guidance.strip()
        or "Software engineer and platform builder."
    )
    lines = ["CANDIDATE PROFILE", "", bio_text, ""]

    facts = []
    if profile.years_experience is not None:
        facts.append(f"Experience: {profile.years_experience:g} years")
    if profile.seniority:
        facts.append(f"Seniority: {profile.seniority}")
    if facts:
        lines.extend([*facts, ""])

    # Categorized skills
    if profile.skills:
        lines.append("SKILLS:")
        for cat in sorted(profile.skills, key=lambda c: c.category.lower()):
            clean_skills = [s.strip() for s in cat.skills if s.strip()]
            if clean_skills:
                lines.append(f"  - {cat.category}: {', '.join(clean_skills)}")
        lines.append("")

    # Experience & Project evidence (Ground-truth evidence)
    project_lines = []
    for role in profile.experience:
        if not role.projects:
            project_lines.append(f"  - {role.title} at {role.company}")
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

    if project_lines:
        lines += ["EXPERIENCE:", *project_lines, ""]

    # Education
    if profile.education:
        edu_lines = [
            f"{e.degree} - {e.institution}" + (f" ({e.details})" if e.details else "")
            for e in profile.education
        ]
        lines += ["EDUCATION:", _bullets(edu_lines), ""]

    # Work Eligibility & Constraints
    eligibility = profile.eligibility
    eligibility_lines = []
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
        lines += ["ELIGIBILITY:", _bullets(eligibility_lines), ""]

    # Positioning & AI Strategic Guidance
    if profile.model_guidance and profile.model_guidance.strip():
        lines += [
            "MODEL GUIDANCE:",
            f"  {profile.model_guidance.strip()}",
            "",
        ]

    # Non-negotiable Dealbreakers
    dealbreakers = profile.dealbreakers or profile.targeting.dealbreakers
    if dealbreakers:
        lines += [
            "DEAL-BREAKERS:",
            _bullets(dealbreakers),
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"
