"""Page-oriented adapter using pinned JobSpy parsers and our explicit transport."""

from collections.abc import Generator
from typing import Any, TypedDict

from findajob.search.scheduler import ScrapeTaskPayload
from findajob.search.sources.base import BaseJobSource, SearchPage
from findajob.search.traffic import SourceTraffic


def posting_row(job: Any, source: str) -> dict[str, Any]:
    """Keep JobSpy's model parsing, without its full-search loop or DataFrame sorting."""
    row = job.model_dump()
    row["site"] = source
    row["company"] = row.pop("company_name", None)
    row["location"] = job.location.display_location() if job.location else None
    if job.compensation:
        comp = job.compensation
        row.update(
            interval=comp.interval.value if comp.interval else None,
            min_amount=comp.min_amount,
            max_amount=comp.max_amount,
            currency=comp.currency,
            salary_source="direct_data",
        )
    return row


def extract_description_salary(row: dict[str, Any]) -> None:
    """Fill missing compensation from the merged job description, when present."""
    if row.get("min_amount") is not None or row.get("max_amount") is not None:
        return
    from jobspy.util import extract_salary

    interval, low, high, currency = extract_salary(
        row.get("description"), enforce_annual_salary=False
    )
    if low is not None or high is not None:
        row.update(
            interval=interval,
            min_amount=low,
            max_amount=high,
            currency=currency,
            salary_source="description",
        )


class JobSpySource(BaseJobSource):
    def __init__(self, description_format: str = "markdown"):
        self.description_format = description_format

    def iter_pages(self, task: ScrapeTaskPayload, traffic: SourceTraffic) -> Generator[SearchPage]:
        """Yield one board page at a time; the caller controls the budget boundary.

        Only pinned JobSpy parsers and Indeed's page builder are reused. The
        library's pagination, pacing, retries, and swallowed HTTP errors are bypassed.
        """
        from bs4 import BeautifulSoup
        from jobspy.indeed import Indeed
        from jobspy.linkedin import LinkedIn
        from jobspy.model import Country, DescriptionFormat, ScraperInput, Site

        source = task["source"]
        kwargs: Any = build_scrape_kwargs(task, self.description_format)
        scraper_input = ScraperInput(
            site_type=[Site(source)],
            search_term=kwargs["search_term"],
            location=kwargs["location"],
            distance=kwargs.get("distance", 50),
            country=Country.from_string(task["indeed_country"] or "usa"),
            is_remote=task["is_remote"],
            hours_old=task["hours_old"],
            description_format=DescriptionFormat(self.description_format),
            results_wanted=task["results_wanted"],
        )
        client: Any = Indeed() if source == "indeed" else LinkedIn()
        headers = dict(client.session.headers)
        client.session.close()
        client.scraper_input = scraper_input
        cursor = None
        cursors: set[str] = set()
        offset = 0
        seen: set[str] = set()
        if source == "indeed":
            assert scraper_input.country is not None
            domain, client.api_country_code = scraper_input.country.indeed_domain_value
            client.base_url = f"https://{domain}.indeed.com"
        while True:
            proxy = traffic.least_used()
            session = traffic.bound_session(proxy)
            session.headers.update(headers)
            client.session = session
            reason = None
            if source == "indeed":
                jobs, next_cursor = client._scrape_page(cursor)
                raw_count = len(session.last_response.json()["data"]["jobSearch"]["results"])
                if not raw_count or not next_cursor:
                    reason = "exhausted"
                elif next_cursor in cursors:
                    reason = "pagination_limit"
                else:
                    cursors.add(next_cursor)
                    cursor = next_cursor
            else:
                params = {
                    "keywords": task["query"],
                    "location": kwargs["location"],
                    "distance": kwargs.get("distance", 50),
                    "start": offset,
                    "pageNum": 0,
                }
                if task["hours_old"]:
                    params["f_TPR"] = f"r{task['hours_old'] * 3600}"
                if task["is_remote"]:
                    params["f_WT"] = 2
                response = session.get(
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params=params,
                    timeout=10,
                )
                cards = BeautifulSoup(response.text, "html.parser").select(".base-search-card")
                offset += len(cards)
                jobs = []
                for card in cards:
                    link = card.select_one("a.base-card__full-link")
                    if not link:
                        continue
                    identifier = str(link.get("href", "")).split("?")[0].rstrip("/").split("-")[-1]
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    job = client._process_job(card, identifier, False)
                    if job:
                        jobs.append(job)
                if not cards:
                    reason = "exhausted"
                elif offset >= 1000:
                    reason = "pagination_limit"
            yield SearchPage(
                [posting_row(job, source) for job in jobs],
                proxy,
                reason,
            )
            if reason:
                return


class ScrapeJobsKwargs(TypedDict, total=False):
    """Arguments for jobspy's `scrape_jobs()`.

    Optional throughout, because the call is assembled per board rather than filled in
    once: `country_indeed` and `distance` are Indeed-only, `linkedin_fetch_description` is
    LinkedIn-only, and the rest are omitted when the cell does not ask for them. Omitting a
    key and passing its default are not the same to the library.
    """

    site_name: list[str]
    search_term: str
    location: str | None
    results_wanted: int
    description_format: str
    verbose: int
    hours_old: int
    is_remote: bool
    country_indeed: str
    distance: int
    linkedin_fetch_description: bool
    proxies: list[str]


def build_scrape_kwargs(
    task: ScrapeTaskPayload, description_format: str = "markdown"
) -> ScrapeJobsKwargs:
    """Translate a ScrapeTask payload into scrape_jobs() arguments.

    JobSpy translates ``is_remote`` to LinkedIn's ``f_WT=2`` query parameter.  That
    filter is global unless it is paired with a geographic ``location``.  Older US
    remote cells used ``Remote`` as the location label, which made the resulting
    LinkedIn request worldwide.  Keep the legacy cell id working while sending the
    location that scopes the remote filter to the US.
    """
    source = task["source"]
    location = task["location_label"] or None
    if source == "linkedin" and task["location_id"] == "us_remote":
        location = "United States"

    kwargs: ScrapeJobsKwargs = {
        "site_name": [source],
        "search_term": task["query"],
        "location": location,
        "results_wanted": task["results_wanted"],
        "description_format": description_format,
        "verbose": 0,
    }

    if task["hours_old"]:
        kwargs["hours_old"] = task["hours_old"]

    if task["is_remote"]:
        kwargs["is_remote"] = True

    if source == "indeed":
        kwargs["country_indeed"] = task["indeed_country"] or "usa"
    elif source == "linkedin":
        # Discovery only. The runner reuses stored details before requesting misses.
        kwargs["linkedin_fetch_description"] = False

    if not task["is_remote"] and task["distance"]:
        # JobSpy sends this through to both Indeed and LinkedIn.  Keeping it outside
        # the board branch avoids silently broadening LinkedIn searches.
        kwargs["distance"] = task["distance"]

    if task["proxies"]:
        kwargs["proxies"] = task["proxies"]

    return kwargs
