"""Map LLM-invented skill keys onto the canonical taxonomy."""

from typing import TYPE_CHECKING, Any

from careerradar.core.logger import get_logger
from careerradar.profile.models import MasterSkillCategory, Profile, Skill

if TYPE_CHECKING:
    from careerradar.taxonomy.skills import Taxonomy

logger = get_logger()


def canonicalize_skills(
    skills: list[Skill], taxonomy: "Taxonomy | None"
) -> tuple[list[Skill], dict[str, list[Any]]]:
    """Rewrite skill keys to taxonomy keys. Returns (skills, report)."""
    if taxonomy is None:
        return skills, {"mapped": [], "unmatched": [], "merged": []}

    by_key: dict[str, Skill] = {}
    report: dict[str, list[Any]] = {"mapped": [], "unmatched": [], "merged": []}

    for skill in skills:
        if skill.key in taxonomy:
            canonical = skill.key
        else:
            candidates = taxonomy.canonicalize([skill.label]) or taxonomy.canonicalize([skill.key])
            canonical = candidates[0] if candidates else None

        if canonical and canonical != skill.key:
            report["mapped"].append((skill.key, canonical))
            skill = skill.model_copy(update={"key": canonical})
        elif not canonical:
            report["unmatched"].append(skill.key)

        existing = by_key.get(skill.key)
        if existing is None:
            by_key[skill.key] = skill
            continue

        report["merged"].append(skill.key)
        winner, loser = (existing, skill) if existing.level >= skill.level else (skill, existing)
        merged_evidence = winner.evidence
        if loser.evidence and loser.evidence not in merged_evidence:
            merged_evidence = f"{merged_evidence} | {loser.evidence}"
        by_key[skill.key] = winner.model_copy(update={"evidence": merged_evidence})

    ordered = sorted(by_key.values(), key=lambda s: (-s.level, s.key))
    return ordered, report


def canonicalize_profile(
    profile: Profile, taxonomy: "Taxonomy | None"
) -> tuple[Profile, dict[str, list[Any]]]:
    """Return `profile` with its categorized skills canonicalized."""
    if taxonomy is None:
        return profile, {"mapped": [], "unmatched": [], "merged": []}

    report: dict[str, list[Any]] = {"mapped": [], "unmatched": [], "merged": []}
    new_categories: list[MasterSkillCategory] = []

    for cat in profile.skills:
        cat_skills: list[str] = []
        for s in cat.skills:
            clean = s.strip()
            if not clean:
                continue
            canonical = taxonomy.canonicalize([clean])
            if canonical and canonical[0] != clean:
                report["mapped"].append((clean, canonical[0]))
                cat_skills.append(canonical[0])
            else:
                cat_skills.append(clean)
        new_categories.append(cat.model_copy(update={"skills": cat_skills}))

    return profile.model_copy(update={"skills": new_categories}), report
