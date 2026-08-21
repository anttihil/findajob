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

if TYPE_CHECKING:
    from careerradar.taxonomy.roles import RoleTaxonomy

# Every filter the dashboard can express, defaulted. The names match the query parameters
# of `Database.query_jobs` exactly, which is the point -- a form field whose name is wrong
FILTER_DEFAULTS: dict[str, Any] = {
    "status": "unread",
    "access": "",
    "country": "",
    "fit": None,
    "reason_type": "",
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
