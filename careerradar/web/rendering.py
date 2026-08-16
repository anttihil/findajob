"""Helpers the dashboard templates lean on.

Everything here used to live in `frontend/js/app.js` as string-building functions. The
move is not cosmetic: the filter state in particular had no single home. `fetchJobs()`
read the country and score straight off DOM nodes while the status and reachability lived
in module globals mutated by two separate click handlers, and the two handlers raced --
both fired on every reachability click, the first with a stale value, and whichever
response landed last won. `FilterQuery` exists so that "the current filter" is one object,
built from the request, with no second copy anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

from careerradar.profile.models import normalize_requirement

if TYPE_CHECKING:
    from careerradar.taxonomy.roles import RoleTaxonomy

from markupsafe import Markup, escape

# Ordering for the drawer's requirement checklist. Must-haves first: they are the ones
# that decide whether an application is worth writing.
IMPORTANCE_RANK = {"must_have": 0, "important": 1, "nice_to_have": 2}

# Every filter the dashboard can express, defaulted. The names match the query parameters
# of `Database.query_jobs` exactly, which is the point -- a form field whose name is wrong
# now fails visibly at the route signature instead of being silently dropped by FastAPI,
# which is how `/api/search-links?resume=...` went unnoticed.
FILTER_DEFAULTS: dict[str, Any] = {
    "status": "unread",
    "access": "",
    "country": "",
    "min_score": None,
    "max_tier": None,
    "verdict": "",
    "eligibility": "",
    "sort": "fit",
    "limit": 50,
    "offset": 0,
}


@dataclass
class FilterQuery:
    """The dashboard's filter state, and the only copy of it.

    Constructed from the request's query parameters and used both to run the database
    query and to render the controls. A control that renders as "active" is therefore
    active by construction -- the desync that made the reachability pills unreliable is
    not expressible here.
    """

    values: dict[str, Any] = field(default_factory=dict)
    job: int | None = None

    def __post_init__(self) -> None:
        merged = dict(FILTER_DEFAULTS)
        merged.update({k: v for k, v in self.values.items() if v is not None})
        self.values = merged

    def __getattr__(self, name: str) -> Any:
        # Templates read `query.status`, `query.sort` and so on directly.
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def _query_string(self, overrides: dict[str, Any]) -> str:
        merged = dict(self.values)
        merged.update(overrides)
        pairs = [
            (key, value)
            for key, value in merged.items()
            # A parameter left at its default is left out of the URL: the address bar
            # stays readable, and a shared link means what it says.
            if value not in (None, "", FILTER_DEFAULTS.get(key))
        ]
        return urlencode(pairs)

    def url(self, **overrides: Any) -> str:
        """A link to the dashboard with some filters changed.

        Changing any filter drops the open drawer and returns to the first page: the
        posting you were reading may not survive the new filter, and offset 3 of the
        previous result set means nothing in the new one.
        """
        overrides.setdefault("offset", 0)
        qs = self._query_string(overrides)
        return f"/?{qs}" if qs else "/"

    def page(self, offset: int) -> str:
        """A link to another page of the *same* filter."""
        qs = self._query_string({"offset": max(0, offset)})
        return f"/?{qs}" if qs else "/"

    def with_job(self, job_id: int) -> str:
        qs = self._query_string({"job": job_id})
        return f"/?{qs}"

    def without_job(self) -> str:
        qs = self._query_string({"job": None})
        return f"/?{qs}" if qs else "/"

    # The same two URLs against `/drawer`, which renders the drawer partial and nothing
    # else. htmx fetches these and pushes the `/?...` pair above into the address bar, so
    # what the browser shows stays a URL the server can render on its own -- reload, deep
    # link and the no-JS path all still go through `dashboard()`.
    #
    # Derived from the same `_query_string` as the page URLs rather than assembled
    # separately: the drawer has to be built from the filter the feed is showing, because
    # "the next posting" is only meaningful relative to that filter.
    def drawer_url(self, job_id: int) -> str:
        return f"/drawer?{self._query_string({'job': job_id})}"

    def filter_qs(self) -> str:
        """The filter alone, as a query string, with no `job`.

        For the drawer's status POST, which has to tell the server which feed it is
        triaging so the reply can be the next posting in it. `hidden_fields` deliberately
        drops `offset` -- correct for a form that changes the filter, wrong here, where
        the page being read is exactly what has to survive.
        """
        return self._query_string({"job": None})

    def drawer_close_url(self) -> str:
        qs = self._query_string({"job": None})
        return f"/drawer?{qs}" if qs else "/drawer"

    def is_active(self, key: str, value: Any) -> bool:
        return self.values.get(key) == value

    def hidden_fields(self, exclude: str = "") -> list[tuple[str, Any]]:
        """The filter as hidden inputs, for a form that sets one other field.

        Paging is left out deliberately: any form that re-submits the filter is changing
        it, and page 4 of the old result set is not page 4 of the new one.
        """
        skip = {exclude, "offset"}
        return [
            (key, value)
            for key, value in self.values.items()
            if key not in skip and value not in (None, "", FILTER_DEFAULTS.get(key))
        ]

    def as_db_kwargs(self) -> dict[str, Any]:
        """The filter, as arguments to `Database.query_jobs`."""
        return {key: (value if value != "" else None) for key, value in self.values.items()}


def short_date(value: Any) -> str:
    """ "Jul 15", the format the job cards used via `toLocaleDateString`."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return f"{value.strftime('%b')} {value.day}"


def hostname(url: str) -> str:
    """Bare host for a dossier source link, matching `new URL(u).hostname`."""
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def highlight_terms(description: str | None, skills: list[str]) -> Markup:
    """Escape the description, then wrap every matched skill in a highlight span.

    Escaping first and inserting markup second is the same order the JS used, and it is
    the order that matters: the posting text is untrusted, the spans are ours.
    """
    text = str(escape(description or ""))
    for skill in skills or []:
        if not skill:
            continue
        pattern = re.escape(str(escape(skill)))
        # Word boundaries would not survive skills like "node.js", "CI/CD" or "front-end",
        # whose punctuation is not a word character.
        if not any(ch in skill for ch in "./-"):
            pattern = rf"\b{pattern}\b"
        text = re.sub(
            pattern,
            lambda m: f'<span class="highlight-term">{m.group(0)}</span>',
            text,
            flags=re.IGNORECASE,
        )
    return Markup(text)


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
