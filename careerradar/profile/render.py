"""Render a Profile into the prompt prefix the scoring agent sends on every call.

This text is computed **once**, at profile-approval time, and stored in
`profiles.summary_text`. It is never rebuilt per request.

That is a cost decision, not a style one. DeepSeek caches prompt prefixes automatically at
1/50th the input rate, matched byte-for-byte. Rendering per request would expose the prefix
to dict iteration order, float formatting, and any future "as of <date>" line -- each of
which silently drops the cache hit rate to zero and multiplies scoring cost by ~50 with no
error to notice. Freezing it removes the whole class of bug.

Everything below is therefore sorted deterministically and carries no clock.
"""

from careerradar.profile.models import (
    LEVEL_CLAIMED,
    LEVEL_MENTIONED,
    LEVEL_STRONG,
    Profile,
)

_LEVEL_HEADINGS = [
    (LEVEL_STRONG, "STRONG -- built and shipped substantial work with these"),
    (LEVEL_CLAIMED, "WORKING -- used in real work"),
    (LEVEL_MENTIONED, "FAMILIAR -- touched or studied, not yet proven"),
]


def _bullets(items):
    return "\n".join(f"  - {item}" for item in items)


def render_profile(profile: Profile) -> str:
    """Produce the stable prompt prefix for one profile."""
    lines = ["CANDIDATE PROFILE", "", profile.bio.strip(), ""]

    facts = []
    if profile.years_experience is not None:
        facts.append(f"Experience: {profile.years_experience:g} years")
    if profile.seniority:
        facts.append(f"Seniority: {profile.seniority}")
    if facts:
        lines.extend(facts + [""])

    for level, heading in _LEVEL_HEADINGS:
        skills = sorted(
            (s for s in profile.skills if s.level == level),
            key=lambda s: s.key,
        )
        if not skills:
            continue
        lines.append(f"{heading}:")
        lines.append("  " + ", ".join(s.label for s in skills))
        lines.append("")

    if profile.strengths:
        lines += ["STRENGTHS:", _bullets(profile.strengths), ""]
    if profile.weaknesses:
        # Included on purpose. A profile that lists only strengths produces a scorer that
        # rates every posting 'strong' -- it has nothing to weigh a stretch against.
        lines += ["HONEST GAPS:", _bullets(profile.weaknesses), ""]

    constraints = profile.constraints
    constraint_lines = []
    if constraints.work_authorization:
        constraint_lines.append(
            f"Authorized to work in: {', '.join(sorted(constraints.work_authorization))}"
        )
    if constraints.locations:
        constraint_lines.append(f"Will work from: {', '.join(sorted(constraints.locations))}")
    if constraints.willing_to_relocate is not None:
        constraint_lines.append(
            f"Willing to relocate: {'yes' if constraints.willing_to_relocate else 'no'}"
        )
    if constraints.comp_floor_usd:
        constraint_lines.append(
            f"Will not accept below: ${constraints.comp_floor_usd:,} base"
        )
    if constraints.languages:
        constraint_lines.append(f"Languages: {', '.join(sorted(constraints.languages))}")
    if constraints.notes:
        constraint_lines.append(constraints.notes)
    if constraint_lines:
        lines += ["HARD CONSTRAINTS:", _bullets(constraint_lines), ""]

    preferences = profile.preferences
    preference_lines = []
    if preferences.role_families:
        preference_lines.append(f"Roles: {', '.join(sorted(preferences.role_families))}")
    if preferences.work_mode:
        preference_lines.append(f"Work mode: {preferences.work_mode}")
    if preferences.company_sizes:
        preference_lines.append(f"Company size: {', '.join(sorted(preferences.company_sizes))}")
    if preferences.industries:
        preference_lines.append(f"Industries: {', '.join(sorted(preferences.industries))}")
    if preferences.notes:
        preference_lines.append(preferences.notes)
    if preference_lines:
        lines += ["PREFERENCES (these shade a score; they do not veto):",
                  _bullets(preference_lines), ""]

    if profile.non_negotiables:
        lines += ["NON-NEGOTIABLE -- a posting matching any of these is a mismatch:",
                  _bullets(profile.non_negotiables), ""]
    if profile.red_flags:
        lines += ["RED FLAGS -- treat as warning signs, not automatic disqualifiers:",
                  _bullets(profile.red_flags), ""]

    return "\n".join(lines).rstrip() + "\n"
