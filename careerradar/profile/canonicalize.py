"""Map LLM-invented skill keys onto the canonical taxonomy.

The model names skills the way a person would -- "ReactJS", "D3.js", "OCR (ocrmypdf)".
`keyword_score` and `gap_analysis` look skills up by the canonical key from
`data/skills.yaml`, which for those three is `react`, `d3`, and `ocr`.

Left alone, the mismatch is silent and expensive: a profile that says `reactjs` at level 3
answers `profile.level("react") == 0`, so every React requirement in every posting is
scored as a gap the candidate does not have. Nothing errors; the numbers are just wrong.
The first build produced exactly this for 4 of 28 skills, including React.

So canonicalization is applied at save time rather than left to the prompt. A prompt can be
ignored; an invariant enforced on the way into the database cannot.

Skills with no taxonomy match keep their original key. They stay in the rendered profile
prefix (the LLM reading a posting still benefits from knowing about them) and are simply
invisible to the keyword layer, which is the honest outcome for a skill the taxonomy has
never heard of.
"""

from careerradar.core.logger import get_logger

logger = get_logger()


def canonicalize_skills(skills, taxonomy):
    """Rewrite skill keys to taxonomy keys. Returns (skills, report)."""
    if taxonomy is None:
        return skills, {"mapped": [], "unmatched": [], "merged": []}

    by_key = {}
    report = {"mapped": [], "unmatched": [], "merged": []}

    for skill in skills:
        # Order matters, and getting it wrong is worse than not canonicalizing at all.
        #
        # An earlier version canonicalized the *label* first. Labels are prose and often
        # name more than one skill -- "Claude API (via AWS Bedrock)", "Terraform / HCL" --
        # and `canonicalize` returns every surface it recognizes, so taking the first
        # match rewrote `claude_api` to `aws` and `terraform` to `hcl`. Both keys were
        # already canonical; the rewrite invented a skill the candidate does not have and
        # deleted one they do.
        #
        # So: trust an already-valid key, and only fall back to reading the label.
        if skill.key in taxonomy:
            canonical = skill.key
        else:
            candidates = (
                taxonomy.canonicalize([skill.label])
                or taxonomy.canonicalize([skill.key])
            )
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

        # Two surfaces collapsed onto one key ("React" and "React Hooks"). Keep the
        # stronger claim and concatenate the evidence rather than dropping one silently.
        report["merged"].append(skill.key)
        winner, loser = (
            (existing, skill) if existing.level >= skill.level else (skill, existing)
        )
        merged_evidence = winner.evidence
        if loser.evidence and loser.evidence not in merged_evidence:
            merged_evidence = f"{merged_evidence} | {loser.evidence}"
        by_key[skill.key] = winner.model_copy(update={"evidence": merged_evidence})

    ordered = sorted(by_key.values(), key=lambda s: (-s.level, s.key))

    if report["mapped"]:
        logger.info(
            "Profile: canonicalized %d skill keys (%s)",
            len(report["mapped"]),
            ", ".join(f"{a}->{b}" for a, b in report["mapped"][:6]),
        )
    if report["unmatched"]:
        logger.info(
            "Profile: %d skills outside the taxonomy, kept as-is: %s",
            len(report["unmatched"]), ", ".join(report["unmatched"][:8]),
        )
    return ordered, report


def canonicalize_profile(profile, taxonomy):
    """Return `profile` with its skill keys canonicalized."""
    skills, report = canonicalize_skills(profile.skills, taxonomy)
    return profile.model_copy(update={"skills": skills}), report
