"""Build the user's canonical skill profile from the resume corpus.

The gap analysis needs to answer "does the user have skill X?" -- but that is not a boolean.
Terraform named once in a competencies list and Terraform used across two current projects
with 472 commits are different claims, and a posting that wants deep Terraform should treat
them differently. So each skill carries a level:

  3  strong   -- evidence in achievements.md project metadata (a real stack with volume)
  2  claimed  -- appears in a resume skills/competencies section
  1  mentioned-- appears only as an inline bold/backticked term in prose
  0  absent

Levels 1-3 are derived from the corpus so they stay correct when the resumes change.
Level overrides in skills.yaml exist for skills no resume mentions -- spoken languages being
the case that matters here, since candidates may have EU work authorization and no resume says so.
"""

import os

from backend.resume_parser import AchievementsParser, ResumeParser
from backend.taxonomy import load_taxonomy

LEVEL_ABSENT = 0
LEVEL_MENTIONED = 1
LEVEL_CLAIMED = 2
LEVEL_STRONG = 3

# A project needs some substance before its stack counts as strong evidence; a 10-commit
# collaborative repo should not outrank a competencies-section claim on its own.
STRONG_EVIDENCE_MIN_COMMITS = 20


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class UserProfile:
    def __init__(self, skills, variants, taxonomy, sources):
        # {canonical_key: {"level": int, "evidence": [str], "commits": int, "current": bool}}
        self.skills = skills
        # {resume_filename: {"title": str, "skills": [canonical_key]}}
        self.variants = variants
        self.taxonomy = taxonomy
        self.sources = sources

    # -- queries -----------------------------------------------------------------------
    def has(self, key, min_level=LEVEL_MENTIONED):
        return self.skills.get(key, {}).get("level", 0) >= min_level

    def level(self, key):
        return self.skills.get(key, {}).get("level", 0)

    def keys(self, min_level=LEVEL_MENTIONED):
        return {k for k, v in self.skills.items() if v["level"] >= min_level}

    def evidence(self, key):
        return self.skills.get(key, {}).get("evidence", [])

    def by_category(self):
        grouped = {}
        for key, record in self.skills.items():
            if record["level"] <= 0:
                continue
            grouped.setdefault(self.taxonomy.category(key), []).append(key)
        return grouped

    def resume_for_family(self, resume_filename):
        return self.variants.get(resume_filename)

    def to_dict(self):
        return {
            "skills": {
                key: {
                    "label": self.taxonomy.label(key),
                    "category": self.taxonomy.category(key),
                    **record,
                }
                for key, record in sorted(self.skills.items())
                if record["level"] > 0
            },
            "variants": {
                name: {"title": info["title"], "skill_count": len(info["skills"])}
                for name, info in self.variants.items()
            },
            "sources": self.sources,
            "taxonomy_hash": self.taxonomy.hash,
        }

    def __len__(self):
        return len(self.keys())

    def __contains__(self, key):
        return self.has(key)


def _record(skills, key, level, note):
    record = skills.setdefault(
        key, {"level": LEVEL_ABSENT, "evidence": [], "commits": 0, "current": False}
    )
    record["level"] = max(record["level"], level)
    if note and note not in record["evidence"]:
        record["evidence"].append(note)
    return record


def build_profile(resumes_dir=None, achievements_path=None, current_resume_path=None,
                  taxonomy=None):
    """Assemble the profile from every available source.

    Reads eight sources, not six: the six tailored resumes plus current_resume.md and
    achievements.md. current_resume.md is outside resumes/ and the runtime parser never
    scanned it, yet it is the only file naming Go, Figma, and Jenkins together -- omitting
    it makes those skills invisible or weakly evidenced.
    """
    taxonomy = taxonomy or load_taxonomy()
    root = _repo_root()
    resumes_dir = resumes_dir or os.path.join(root, "resumes")
    achievements_path = achievements_path or os.path.join(root, "achievements.md")
    current_resume_path = current_resume_path or os.path.join(root, "current_resume.md")

    skills = {}
    variants = {}
    sources = []

    # 1. Taxonomy-declared levels first, so corpus evidence can only raise them.
    for key, skill in taxonomy.skills.items():
        if skill.user_level is not None:
            _record(skills, key, skill.user_level, "declared in skills.yaml")

    # 2. The tailored resume variants.
    parser = ResumeParser(resumes_dir)
    for name, info in parser.parse_all().items():
        section_keys = taxonomy.canonicalize(info["candidates"])
        inline_keys = taxonomy.canonicalize(info["inline_mentions"])
        for key in section_keys:
            _record(skills, key, LEVEL_CLAIMED, f"{name} (skills section)")
        for key in inline_keys:
            if key not in section_keys:
                _record(skills, key, LEVEL_MENTIONED, f"{name} (prose)")
        variants[name] = {
            "title": info["title"],
            "skills": sorted(set(section_keys) | set(inline_keys)),
        }
        sources.append(name)

    # 3. current_resume.md -- same treatment, but not a submission variant.
    if os.path.exists(current_resume_path):
        info = ResumeParser(os.path.dirname(current_resume_path)).parse_file(
            current_resume_path
        )
        for key in taxonomy.canonicalize(info["candidates"]):
            _record(skills, key, LEVEL_CLAIMED, "current_resume.md (skills section)")
        for key in taxonomy.canonicalize(info["inline_mentions"]):
            _record(skills, key, LEVEL_MENTIONED, "current_resume.md (prose)")
        sources.append(os.path.basename(current_resume_path))

    # 4. achievements.md -- the only source with recency and volume, so the only one that
    #    can justify LEVEL_STRONG.
    if os.path.exists(achievements_path):
        achievements = AchievementsParser(achievements_path).parse()
        for key in taxonomy.canonicalize(achievements["candidates"]):
            _record(skills, key, LEVEL_CLAIMED, "achievements.md (technology summary)")

        for surface, evidence in achievements["skill_evidence"].items():
            for key in taxonomy.canonicalize([surface]):
                substantial = evidence["commits"] >= STRONG_EVIDENCE_MIN_COMMITS
                level = LEVEL_STRONG if substantial else LEVEL_CLAIMED
                project_list = ", ".join(evidence["projects"][:2])
                note = f"{project_list} ({evidence['commits']} commits)"
                record = _record(skills, key, level, note)
                record["commits"] = max(record["commits"], evidence["commits"])
                record["current"] = record["current"] or evidence["is_current"]
        sources.append(os.path.basename(achievements_path))

    return UserProfile(skills, variants, taxonomy, sources)


if __name__ == "__main__":
    profile = build_profile()
    print(f"sources: {', '.join(profile.sources)}")
    print(f"skills with any evidence: {len(profile)}")
    print(f"taxonomy_hash: {profile.taxonomy.hash}\n")

    by_level = {}
    for key, record in profile.skills.items():
        by_level.setdefault(record["level"], []).append(key)

    names = {3: "STRONG (project evidence)", 2: "CLAIMED (skills section)",
             1: "MENTIONED (prose only)", 0: "ABSENT"}
    for level in (3, 2, 1, 0):
        keys = sorted(by_level.get(level, []))
        if not keys:
            continue
        print(f"{names[level]} -- {len(keys)}")
        print(f"  {', '.join(profile.taxonomy.label(k) for k in keys)}\n")
