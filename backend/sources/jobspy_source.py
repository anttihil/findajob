"""JobSpy-backed source for LinkedIn and Indeed, behind the BaseJobSource interface.

Wrapping the library rather than calling it directly keeps the blast radius small: board
markup and endpoints change often, and `backend/sources/base.py` is the seam where a
hand-rolled fallback can take over.

Two things about the library's real behaviour, verified against a live scrape rather than
its README (which documents 11 columns; the actual output has 34):

  Indeed returns full descriptions in-response, so a description census costs nothing extra
  and Indeed cells carry the skill analytics.

  LinkedIn charges one extra request per description, so only a budgeted subset can be
  fetched. That subset is chosen by pre-score, which correlates with the user's own skills,
  so those descriptions are marked desc_selection='top_k' and excluded from skill-demand
  denominators. LinkedIn still counts fully toward role supply.
"""

import json
import os
import warnings
from datetime import datetime, timezone

from backend.logger import get_logger
from backend.sources.base import BaseJobSource

logger = get_logger()

# Columns JobSpy actually returns, verified live on 2026-07-29. Recorded here so a future
# library upgrade that drops one is visible rather than silently producing empty fields.
EXPECTED_COLUMNS = {
    "id", "site", "job_url", "job_url_direct", "title", "company", "location",
    "date_posted", "job_type", "salary_source", "interval", "min_amount", "max_amount",
    "currency", "is_remote", "job_level", "job_function", "listing_type", "emails",
    "description", "company_industry", "company_url", "company_logo",
    "company_url_direct", "company_addresses", "company_num_employees",
    "company_revenue", "company_description",
}


class JobSpySource(BaseJobSource):
    """Fetches postings for one scrape task."""

    def __init__(self, archive_dir=None, description_format="markdown"):
        self.description_format = description_format
        self.archive_dir = archive_dir
        self._scrape = None

    def _loader(self):
        if self._scrape is None:
            # Imported lazily so the rest of the app -- and the whole test suite -- runs
            # without pandas/numpy/tls-client present.
            from jobspy import scrape_jobs

            self._scrape = scrape_jobs
        return self._scrape

    # -- BaseJobSource -----------------------------------------------------------------
    def fetch_jobs(self, country, query):
        """Interface-compatible entry point. Prefer fetch_for_task for real runs."""
        task = {
            "source": "indeed", "query": query, "country": country,
            "indeed_country": _indeed_country(country), "location_label": "",
            "results_wanted": 25, "is_remote": False, "distance": 50,
            "hours_old": None, "fetch_description": True,
        }
        return self.fetch_for_task(task)

    def fetch_for_task(self, task):
        """Run one search and return raw row dicts.

        Raises on failure so the caller's SourceCircuit can classify it -- a 429 must trip
        the source rather than being swallowed and retried here.
        """
        scrape = self._loader()
        kwargs = build_scrape_kwargs(task, self.description_format)

        logger.info(
            "[%s] %r in %s (want %d, hours_old=%s, desc=%s)",
            task.get("source"), task.get("query"), task.get("location_label"),
            kwargs.get("results_wanted"), kwargs.get("hours_old"),
            kwargs.get("linkedin_fetch_description", True),
        )

        with warnings.catch_warnings():
            # JobSpy emits pandas FutureWarnings on concat of empty frames.
            warnings.simplefilter("ignore")
            frame = scrape(**kwargs)

        rows = frame_to_rows(frame)
        if self.archive_dir:
            self._archive(task, rows)
        return rows

    def _archive(self, task, rows):
        """Persist the raw payload so normalizer bugs can be replayed without re-scraping."""
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            slug = _slug(f"{task.get('source')}-{task.get('query')}-"
                         f"{task.get('location_id')}")
            path = os.path.join(self.archive_dir, f"{stamp}-{slug}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"task": task, "rows": rows}, handle, default=str)
        except OSError as exc:
            logger.warning(f"could not archive raw payload: {exc}")


def build_scrape_kwargs(task, description_format="markdown"):
    """Translate a ScrapeTask dict into scrape_jobs() arguments."""
    source = task.get("source", "indeed")
    kwargs = {
        "site_name": [source],
        "search_term": task.get("query"),
        "location": task.get("location_label") or None,
        "results_wanted": int(task.get("results_wanted") or 25),
        "description_format": description_format,
        "verbose": 0,
    }

    hours_old = task.get("hours_old")
    if hours_old:
        kwargs["hours_old"] = int(hours_old)

    if task.get("is_remote"):
        kwargs["is_remote"] = True

    if source == "indeed":
        kwargs["country_indeed"] = task.get("indeed_country") or "usa"
        if not task.get("is_remote") and task.get("distance"):
            kwargs["distance"] = int(task["distance"])
    elif source == "linkedin":
        # False for the cheap pass; descriptions are fetched separately for the top-scoring
        # subset only, which is why desc_selection is 'top_k' rather than 'census'.
        kwargs["linkedin_fetch_description"] = bool(task.get("fetch_description"))

    if task.get("proxies"):
        kwargs["proxies"] = task["proxies"]

    return kwargs


def frame_to_rows(frame):
    """Convert a JobSpy DataFrame into plain dicts, warning on unexpected schema drift."""
    if frame is None:
        return []
    if not hasattr(frame, "to_dict"):
        return list(frame)
    if len(frame) == 0:
        return []

    missing = EXPECTED_COLUMNS - set(frame.columns)
    if missing:
        # Not fatal -- board-specific columns are legitimately absent -- but a large gap
        # means the library changed and the normalizer may be reading nothing.
        logger.warning(
            f"JobSpy returned {len(missing)} fewer columns than expected: "
            f"{sorted(missing)[:8]}"
        )

    return frame.to_dict(orient="records")


def _indeed_country(country_code):
    return {
        "US": "usa", "SE": "sweden", "NO": "norway", "DK": "denmark", "FI": "finland",
        "GB": "uk", "CA": "canada", "DE": "germany",
    }.get((country_code or "").upper(), "usa")


def _slug(text):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in str(text))[:80]
