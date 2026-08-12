"""Expose an LLM-built Profile through the interface the keyword layer already speaks.

`keyword_score.py`, `gap_analysis.py`, and `digest.py` all take a profile object and call
`has()`, `level()`, `keys()`, and `evidence()` on it. Those modules are not being replaced:
the keyword score still feeds `match_score`, and `gap_analysis` still measures its blocking
gap against postings the user matches at GOOD_FIT_THRESHOLD or better.

So the new profile grows the old query surface rather than every caller growing a new one.
This is not a compatibility shim kept out of caution -- it is the boundary that lets one
well-tested subsystem (skill demand and gap analytics, ~900 lines with careful censoring)
survive a change of what a "profile" is.

`variants` is the one place the shape genuinely changed. The old profile carried six
tailored resumes and picked between them; the new one is a single unified picture, so which
resume to send is derived from `role_family` via roles.yaml instead. The attribute stays as
an empty mapping so `keyword_score._resume_component` degrades to its documented
"mapped-but-missing" branch rather than raising.
"""

from careerradar.profile.models import LEVEL_MENTIONED, Profile


class ProfileAdapter:
    """A Profile wearing the old UserProfile interface."""

    def __init__(self, profile: Profile, version=None, taxonomy=None):
        self.profile = profile
        self.version = version
        self.taxonomy = taxonomy
        self.skills = {
            skill.key: {
                "level": skill.level,
                "label": skill.label,
                "evidence": [skill.evidence] if skill.evidence else [],
                "recency": skill.recency,
            }
            for skill in profile.skills
        }
        # The unified profile has no resume variants; see the module docstring.
        self.variants = {}
        self.sources = [f"profile v{version}"] if version else ["profile"]

    def has(self, key, min_level=LEVEL_MENTIONED):
        return self.level(key) >= min_level

    def level(self, key):
        record = self.skills.get(key)
        return record["level"] if record else 0

    def keys(self, min_level=LEVEL_MENTIONED):
        return {k for k, v in self.skills.items() if v["level"] >= min_level}

    def evidence(self, key):
        record = self.skills.get(key)
        return record["evidence"] if record else []

    def by_category(self):
        """Group skills by taxonomy category, for the dashboard.

        Falls back to a single bucket when no taxonomy is attached, rather than failing --
        callers use this for display only.
        """
        grouped = {}
        for key, record in self.skills.items():
            # `taxonomy.skills[key]` is a Skill object with __slots__, not a mapping, so
            # the .get() this used to do raised AttributeError for every known key -- the
            # method only ever "worked" on the no-taxonomy path that buckets everything
            # under "other". `category()` is the accessor that exists for this.
            category = "other"
            if self.taxonomy is not None:
                category = self.taxonomy.category(key) or "other"
            grouped.setdefault(category, []).append(
                {"key": key, "label": record["label"], "level": record["level"]}
            )
        for entries in grouped.values():
            entries.sort(key=lambda e: (-e["level"], e["key"]))
        return grouped

    def to_dict(self):
        return {
            "version": self.version,
            "bio": self.profile.bio,
            "years_experience": self.profile.years_experience,
            "seniority": self.profile.seniority,
            "skills": {
                key: {"level": r["level"], "label": r["label"], "evidence": r["evidence"]}
                for key, r in self.skills.items()
            },
            "strengths": self.profile.strengths,
            "weaknesses": self.profile.weaknesses,
            "constraints": self.profile.constraints.model_dump(),
            "preferences": self.profile.preferences.model_dump(),
            "non_negotiables": self.profile.non_negotiables,
            "red_flags": self.profile.red_flags,
            "by_category": self.by_category(),
        }

    def __len__(self):
        return len(self.skills)

    def __contains__(self, key):
        return key in self.skills


class NoActiveProfile(RuntimeError):
    """Raised instead of silently scoring against nothing.

    The retired regex profile degraded quietly: a missing corpus produced a thin profile
    and every match score shifted, with only a log line to say so. An absent profile is now
    a hard stop, because the alternative is a full corpus scored against an empty candidate.
    """


def load_profile(db=None, taxonomy=None, required=True):
    """Load the active profile as a ProfileAdapter."""
    from careerradar.profile.store import load_active

    loaded = load_active(db=db)
    if loaded is None:
        if required:
            raise NoActiveProfile(
                "No active profile. Build one first:  careerradar profile build"
            )
        return None
    version, profile, _summary = loaded
    return ProfileAdapter(profile, version=version, taxonomy=taxonomy)
