"""Backwards-compatible wrapper over backend/scoring.py.

The old JobMatcher scored by regex keyword count (+25 if a resume skill appeared in the
title, +5 in the body, capped at 100) and called the result a percent. It compared raw
resume strings against raw description text, so "AWS (EC2, S3, IAM)" matched nothing, and a
posting listing many technologies outscored one that actually fit.

Scoring now lives in backend/scoring.py, which shares a canonical vocabulary with the job
corpus and decomposes each score into named components. This shim remains so that
`evaluate_job(title, description)` keeps working for any caller that has not moved over.
New code should use JobScorer directly -- it returns the component breakdown, the matched
and missing skills, and the resume mapping.
"""

from backend.profile import build_profile
from backend.roles import load_roles
from backend.scoring import JobScorer
from backend.taxonomy import load_taxonomy


class JobMatcher:
    """Legacy interface: evaluate_job(title, description) -> (resume, score, skills)."""

    def __init__(self, resumes_dict=None, profile=None, roles=None, taxonomy=None):
        # resumes_dict is accepted and ignored: the profile is now derived from the full
        # corpus (including current_resume.md and achievements.md) rather than from
        # whatever dict the caller happened to build.
        self.taxonomy = taxonomy or load_taxonomy()
        self.roles = roles or load_roles()
        self.profile = profile or build_profile(taxonomy=self.taxonomy)
        self.resumes = resumes_dict or {}
        self._scorer = JobScorer(self.profile, self.roles, self.taxonomy)

    def evaluate_job(self, title, description):
        """Returns (best_resume_filename, score, matched_skill_labels)."""
        family, seniority = self.roles.classify(
            title or "", has_tech_skills=True
        )
        result = self._scorer.score({
            "title": title or "",
            "description": description or "",
            "role_family": family,
            "seniority": seniority,
        })
        labels = [self.taxonomy.label(key) for key in result["matched_skills"]]
        resume = result["resume_match"] or (
            next(iter(self.resumes), None) if self.resumes else None
        )
        return resume, result["score"], labels

    def evaluate_detailed(self, posting):
        """Full result dict, for callers that want the component breakdown."""
        return self._scorer.score(posting)
