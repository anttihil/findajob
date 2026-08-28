"""Render a Profile into the prompt prefix the scoring agent sends on every call.

This text is computed **once**, at profile-save time, and stored in `profile.summary_text`.
It is never rebuilt per request. DeepSeek caches prompt prefixes automatically.
"""

from careerradar.profile.models import Profile


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  - {item}" for item in items)


def render_profile(profile: Profile) -> str:
    """Produce the deterministic stable prompt prefix for one profile."""
    bio_text = profile.summary_guidance.strip() or "Software engineer and platform builder."
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
        lines.append("CORE SKILLS & TECHNOLOGIES:")
        for cat in sorted(profile.skills, key=lambda c: c.category.lower()):
            clean_skills = [s.strip() for s in cat.skills if s.strip()]
            if clean_skills:
                lines.append(f"  - {cat.category}: {', '.join(clean_skills)}")
        lines.append("")

    # Experience & Project evidence
    extracted_projects = []
    for role in profile.experience:
        for proj in role.projects:
            bullets_text = " ".join(proj.bullets[:2])
            desc = f"{role.title} at {role.company}: {proj.name}"
            if proj.heading:
                desc += f" - {proj.heading}"
            if bullets_text:
                desc += f" ({bullets_text})"
            extracted_projects.append(desc[:200])

    if extracted_projects:
        lines += ["KEY PROJECTS / ACHIEVEMENTS (GROUND TRUTH):", _bullets(extracted_projects), ""]

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
        lines += ["HARD ELIGIBILITY & CONSTRAINTS:", _bullets(eligibility_lines), ""]

    # Targeting & Preferences
    targeting = profile.targeting
    targeting_lines = []
    if targeting.target_roles:
        targeting_lines.append(f"Target roles: {', '.join(sorted(targeting.target_roles))}")
    if targeting.work_modes:
        targeting_lines.append(f"Work modes: {', '.join(sorted(targeting.work_modes))}")
    if targeting.target_industries:
        targeting_lines.append(
            f"Target industries: {', '.join(sorted(targeting.target_industries))}"
        )
    if targeting_lines:
        lines += [
            "TARGET PREFERENCES (these shade a score; they do not veto):",
            _bullets(targeting_lines),
            "",
        ]

    # Dealbreakers
    if targeting.dealbreakers:
        lines += [
            "NON-NEGOTIABLE DEALBREAKERS (a posting matching any of these is a hard veto):",
            _bullets(targeting.dealbreakers),
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"
