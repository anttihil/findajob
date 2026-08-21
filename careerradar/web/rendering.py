"""Helpers the API layer leans on.

The dashboard's rendering -- URL-building, HTML escaping, markup -- moved to the Preact SPA
(`careerradar/web/frontend-src/`) along with the templates it used to feed. What's left here
is server-side-only: the filter's *parsing* (still needed to validate and normalise the query
params `/api/jobs/{id}/context` accepts) and the two joins that have to stay server-side
because their normalisation has to match `profile/models.py` and `data/roles.yaml` exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from careerradar.profile.models import normalize_requirement

if TYPE_CHECKING:
    from careerradar.taxonomy.roles import RoleTaxonomy

# Ordering for the drawer's requirement checklist. Must-haves first: they are the ones
# that decide whether an application is worth writing.
IMPORTANCE_RANK = {"must_have": 0, "important": 1, "nice_to_have": 2}

# Every filter the dashboard can express, defaulted. The names match the query parameters
# of `Database.query_jobs` exactly, which is the point -- a form field whose name is wrong
FILTER_DEFAULTS: dict[str, Any] = {
    "status": "unread",
    "access": "",
    "country": "",
    "fit": None,
    "reason_type": "",
    "max_tier": None,
    "verdict": "",
    "eligibility": "",
    "liveness": "",
    "q": "",
    "sort": "fit",
    "limit": 50,
    "offset": 0,
}

REASON_TYPE_CHOICES = [
    ("match", "Match"),
    ("skills", "Skills gap"),
    ("experience", "Experience gap"),
    ("seniority", "Seniority mismatch"),
    ("domain", "Domain mismatch"),
    ("clearance", "Clearance/Citizenship"),
    ("location", "Location constraint"),
    ("tech_stack", "Tech stack mismatch"),
    ("overqualified", "Overqualified"),
]

# Ordered as the scale reads them, best first, so `/api/meta`'s verdict list is a ranking
# rather than a list.
VERDICT_CHOICES = [
    ("strong", "Strong match"),
    ("worth_applying", "Worth applying"),
    ("stretch", "Stretch"),
    ("poor_fit", "Poor fit"),
    ("mismatch", "Mismatch"),
]


@dataclass
class FilterQuery:
    """The dashboard's filter state, parsed from the request's query parameters.

    The SPA owns the URL-building/default-stripping side of this now (see
    `frontend-src/src/lib/filterQuery.ts`) -- what's left here is the part that has to stay
    server-side: turning validated query params into `Database.query_jobs` kwargs.
    """

    values: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        merged = dict(FILTER_DEFAULTS)
        merged.update({k: v for k, v in self.values.items() if v is not None})
        self.values = merged

    def as_db_kwargs(self) -> dict[str, Any]:
        """The filter, as arguments to `Database.query_jobs`."""
        return {key: (value if value != "" else None) for key, value in self.values.items()}


def requirement_rows(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Join the posting's stated requirements against the model's assessments.

    Display-side join only; the counts on the card come from
    `database._requirement_summary`. Both use `profile/models.normalize_requirement`,
    which owns the join key. This function used `.strip().lower()` where the rest used
    `.strip().casefold()`, so a requirement could render as `unassessed` in the drawer
    while the badge above it counted the same requirement as met.

    An unassessed requirement is an expected state, not a defect in this join: the schema
    no longer refuses a verdict over it, it records an `assessment_incomplete` flag and
    lets the reader see the gap.
    """
    core = job.get("core_requirements") or []
    if not core:
        return []

    by_requirement = {
        normalize_requirement(a.get("requirement") or ""): a
        for a in (job.get("requirement_assessments") or [])
    }

    rows = []
    for req in sorted(core, key=lambda r: IMPORTANCE_RANK.get(r.get("importance"), 3)):
        assessment = by_requirement.get(normalize_requirement(req.get("requirement") or ""))
        rows.append(
            {
                "requirement": req.get("requirement") or "",
                "importance": req.get("importance") or "",
                "quote": req.get("quote") or "",
                "status": (assessment or {}).get("status") or "unassessed",
                "evidence": (assessment or {}).get("candidate_evidence") or "",
            }
        )
    return rows


# Display names for the country codes that appear in data/roles.yaml. The set of codes is
# derived from the locations rather than restated, so adding a location cannot leave the
# dashboard filtering on a country it never offers.
COUNTRY_LABELS = {
    "US": "USA",
    "FI": "Finland",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
}


def country_choices(roles: RoleTaxonomy) -> list[tuple[str, str]]:
    """(code, label) for every country the configured locations cover."""
    codes: list[str] = []
    for location in roles.locations.values():
        if location.country not in codes:
            codes.append(location.country)
    return [(code, COUNTRY_LABELS.get(code, code)) for code in codes]
