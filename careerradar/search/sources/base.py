from abc import ABC, abstractmethod
from typing import Any


class BaseJobSource(ABC):
    """The seam where a hand-rolled scraper can replace the JobSpy-backed one.

    Board markup and endpoints change often, so the pipeline reaches a board only through
    this interface. One cell visit is one call.
    """

    def __init__(self) -> None:
        # What the last fetch cost: {"duration_ms": int, "requests_made": int}. The caller
        # stores it on the observation, so an implementation that cannot measure requests
        # must leave the key out rather than write 0 -- unmeasured and free look identical
        # afterwards, and the scheduler's request budget is tuned against these numbers.
        # Reset at the start of every fetch, and set even when the fetch raises: the
        # requests a rate-limited cell spent are the ones worth knowing about.
        self.last_fetch: dict[str, int] = {}

    @abstractmethod
    def fetch_for_task(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """Run one cell's search and return the board's raw rows.

        `task` is a `ScrapeTask.to_dict()` (careerradar/search/scheduler.py). The fields an
        implementation must honour, because the analytics read them back off the recorded
        observation rather than re-deriving them from the rows:

            source             board to query: 'indeed' | 'linkedin'
            query              search term
            location_label     the location string sent to the board
            indeed_country     Indeed's country slug ('usa', 'sweden', ...)
            is_remote          request remote-flagged postings only
            distance           radius, ignored when is_remote
            results_wanted     upper bound on rows. Returning fewer is precisely what makes
                               an observation unsaturated, so never pad -- and never
                               truncate a full page, or a censored count is stored as a
                               complete one
            hours_old          only postings newer than this. The observation window is
                               recorded as [observed_at - hours_old, observed_at], so
                               ignoring it silently widens what the window claims to cover
            fetch_description  whether to spend requests on full descriptions. Honour it
                               for every row or none: a partial fetch is a selected subset,
                               and `desc_selection` would then be labelling a bias that the
                               skill-demand denominators must not contain
            proxies            zero or one proxy endpoint, already pinned for this cell

        Returns RAW board rows, unnormalized -- whatever columns the board gives.
        `careerradar/search/normalizer.py` owns the mapping onto database columns and keys
        on JobSpy's names: `title`, `company`, `location`, `job_url`, `job_url_direct`,
        `description`, `id`, `site`, `date_posted`, `is_remote`, `min_amount`,
        `max_amount`, `interval`, `currency`, `salary_source`. Rows without `title` or
        `job_url` are dropped there.

        MUST RAISE on failure rather than returning what it collected before the failure.
        A short batch is indistinguishable from a thin market, so a swallowed error is
        stored as a genuine measurement and deflates that cell's supply figure with no
        detectable symptom afterwards -- see `ScraperReportedError` in jobspy_source.py,
        which exists because the underlying library does exactly this. The caller's
        `SourceCircuit` classifies the exception's text, so let the board's own wording
        through rather than replacing it with prose of your own.
        """
