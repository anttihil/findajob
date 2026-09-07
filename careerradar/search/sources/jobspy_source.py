"""JobSpy-backed source for LinkedIn and Indeed, behind the BaseJobSource interface.

Wrapping the library rather than calling it directly keeps the blast radius small: board
markup and endpoints change often, and `sources/base.py` is the seam where a
hand-rolled fallback can take over.

Two things about the library's real behaviour, verified against a live scrape rather than
its README (which documents 11 columns; the actual output has 34):

  Indeed returns full descriptions in-response, so a description census costs nothing extra
  and Indeed cells carry the skill analytics.

  LinkedIn charges one extra request per description, and that request is rewritten to the
  guest fragment endpoint (see `guest_description_endpoint`), which returns the same
  description ~2.6x faster than the full job page the library asks for. On a single IP the
  per-description request is still unaffordable,
  so those cells run desc_selection='none' -- titles only, complete for role supply,
  contributing nothing to skill demand. With a proxy pool configured the with_proxies
  budget flips them to a full census, and LinkedIn feeds the skill analytics too. The
  'top_k' pre-scored subset the earlier design called for is not produced by either path:
  selecting on pre-score correlates with the user's own skills, which is exactly the bias
  the skill-demand denominators must not contain.
"""

import json
import logging
import os
import re
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TypedDict

from careerradar.core.logger import get_logger
from careerradar.search.scheduler import ScrapeTaskPayload
from careerradar.search.sources.base import BaseJobSource

logger = get_logger()

# JobSpy names its per-board loggers "JobSpy:<Board>" (jobspy/util.py create_logger).
JOBSPY_LOGGERS = {
    "indeed": "JobSpy:Indeed",
    "linkedin": "JobSpy:LinkedIn",
}


class ScraperReportedError(RuntimeError):
    """JobSpy logged a failure instead of raising it.

    Its LinkedIn scraper catches network faults and non-2xx responses inside the pagination
    loop, logs them, and returns whatever it had already collected:

        except Exception as e:
            log.error(f"LinkedIn: {str(e)}")
            return JobResponse(jobs=job_list)     # partial, no raise

    So a cell truncated by a read timeout or a 429 arrives here looking like an ordinary
    short result. Nothing raises, the circuit breaker sees a clean run, and the observation
    is stored as a complete count -- it is not even marked saturated, since `returned` is
    well under `requested`. Downstream that reads as genuinely thin supply rather than as a
    censored measurement, which is the silent-deflation failure adaptive_hours_old exists to
    prevent, arriving by a path that bypasses every guard.

    Carries `classify_text` so SourceCircuit keys on the board's own wording alone. The
    human-readable message also names row and error counts, and those digits can collide
    with the status-code markers -- "after 429 row(s)" reading as a rate limit would trip
    the source and persist a backoff of up to a day off the back of a number we wrote
    ourselves.
    """

    def __init__(self, message: str, board_messages: tuple[str, ...] | list[str] = ()):
        super().__init__(message)
        self.board_messages = list(board_messages)
        self.classify_text = "; ".join(self.board_messages)


class _ErrorRecorder(logging.Handler):
    """Collects ERROR-level records emitted by a board's logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001 - a broken log record must not fail a scrape
            self.messages.append("<unformattable log record>")


@contextmanager
def capture_scraper_errors(source: str | None) -> Iterator[_ErrorRecorder]:
    """Watch a board's JobSpy logger for the duration of one scrape.

    Records arrive from JobSpy's worker thread rather than this one (scrape_jobs runs each
    site in a ThreadPoolExecutor), which is fine -- handlers are called on the emitting
    thread and syncs are single-threaded, so one recorder per fetch is unambiguous.
    """
    name = JOBSPY_LOGGERS.get((source or "").lower())
    recorder = _ErrorRecorder()
    if not name:
        yield recorder
        return

    board_logger = logging.getLogger(name)
    board_logger.addHandler(recorder)
    try:
        yield recorder
    finally:
        board_logger.removeHandler(recorder)


class _RequestCounter:
    """Counts the HTTP requests one scrape issues."""

    def __init__(self) -> None:
        self.count = 0


@contextmanager
def count_requests() -> Iterator[_RequestCounter]:
    """Count every request JobSpy sends for the duration of one scrape.

    Both boards build their session through jobspy.util.create_session with is_tls=False,
    and every .get/.post on it funnels through RequestsRotating.request -- so one patch
    counts search pages and per-description fetches alike. TLSRotating is patched too,
    because create_session picks it on is_tls=True and an unpatched class would report a
    confident zero rather than an absence.

    What this counts is requests ISSUED by the library. urllib3 retries below this seam
    (LinkedIn's session sets has_retry with a 429 in the status_forcelist), so the number
    is a lower bound on packets that reached the board.

    The patch is global and the counter is not thread-safe, which is sound only because
    syncs are single-threaded: one cell is scraped at a time, and scrape_jobs runs one
    site per call. Two concurrent scrapes would share the count.
    """
    counter = _RequestCounter()
    try:
        from jobspy.util import RequestsRotating, TLSRotating
    except ImportError:
        # The whole test suite runs without jobspy installed; counting is not what fails.
        yield counter
        return

    patched = [(RequestsRotating, "request"), (TLSRotating, "execute_request")]
    originals = [(cls, name, getattr(cls, name)) for cls, name in patched]

    def wrap(original: Any) -> Any:
        def counted(*args: Any, **kwargs: Any) -> Any:
            counter.count += 1
            return original(*args, **kwargs)

        return counted

    for cls, name, original in originals:
        setattr(cls, name, wrap(original))
    try:
        yield counter
    finally:
        for cls, name, original in originals:
            setattr(cls, name, original)


# JobSpy fetches each LinkedIn description from the full public job page. This is the same
# posting as a guest fragment: measured over 16 paired postings on 2026-08-15, it returns a
# byte-identical description in 49KB median instead of 302KB, at 240ms median instead of
# 630ms -- faster on 16 of 16, ~19s off a 50-description cell. See
# scripts/probe_linkedin_description_endpoint.py and its results in experiments/.
JOB_PAGE_URL = re.compile(r"^https?://[^/]*linkedin\.com/jobs/view/(\d+)/?$")
GUEST_FRAGMENT_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"


@contextmanager
def guest_description_endpoint(enabled: bool) -> Iterator[None]:
    """Route JobSpy's per-description fetches to the guest fragment.

    Rewriting the URL under the library rather than reimplementing `_get_job_details`: the
    fragment carries the same markup, so JobSpy's own parsing of description, industry,
    job function, employment type, apply URL and logo all still apply -- verified field by
    field in the same probe. The one field that differs is `job_level`, which the fragment
    reports as "Not Applicable" on most postings; nothing in this project reads it.

    The request count does not change. A description still costs one request; it is a
    cheaper one.

    If LinkedIn ever retires the fragment, `_get_job_details` swallows the failure and
    returns {} -- descriptions silently become None and the skill-demand denominators
    deflate. `_scrape_one` watches for that: a census cell that returns postings with no
    full descriptions logs a warning rather than storing the deflation quietly.
    """
    if not enabled:
        yield
        return
    try:
        from jobspy.util import RequestsRotating
    except ImportError:
        yield
        return

    original = RequestsRotating.request

    def rewritten(self: Any, method: str, url: str, **kwargs: Any) -> Any:
        match = JOB_PAGE_URL.match(url)
        if match:
            url = GUEST_FRAGMENT_URL.format(job_id=match.group(1))
        return original(self, method, url, **kwargs)

    RequestsRotating.request = rewritten
    try:
        yield
    finally:
        RequestsRotating.request = original


# Columns JobSpy actually returns, verified live on 2026-07-29. Recorded here so a future
# library upgrade that drops one is visible rather than silently producing empty fields.
EXPECTED_COLUMNS = {
    "id",
    "site",
    "job_url",
    "job_url_direct",
    "title",
    "company",
    "location",
    "date_posted",
    "job_type",
    "salary_source",
    "interval",
    "min_amount",
    "max_amount",
    "currency",
    "is_remote",
    # job_level is deliberately absent: nothing reads it, and the guest description
    # endpoint reports it as "Not Applicable" anyway.
    "job_function",
    "listing_type",
    "emails",
    "description",
    "company_industry",
    "company_url",
    "company_logo",
    "company_url_direct",
    "company_addresses",
    "company_num_employees",
    "company_revenue",
    "company_description",
}


class JobSpySource(BaseJobSource):
    """Fetches postings for one scrape task."""

    def __init__(self, archive_dir: str | None = None, description_format: str = "markdown"):
        super().__init__()
        self.description_format = description_format
        self.archive_dir = archive_dir
        self._scrape: Any = None

    def _loader(self) -> Any:
        if self._scrape is None:
            # Imported lazily so the rest of the app -- and the whole test suite -- runs
            # without pandas/numpy/tls-client present.
            from jobspy import scrape_jobs

            self._scrape = scrape_jobs
        return self._scrape

    # -- BaseJobSource -----------------------------------------------------------------
    def fetch_for_task(self, task: ScrapeTaskPayload) -> list[dict[str, Any]]:
        """Run one search and return raw row dicts.

        Raises on failure so the caller's SourceCircuit can classify it -- a 429 must trip
        the source rather than being swallowed and retried here.
        """
        scrape = self._loader()
        kwargs = build_scrape_kwargs(task, self.description_format)

        logger.info(
            "[%s] %r in %s (want %d, hours_old=%s, desc=%s)",
            task["source"],
            task["query"],
            task["location_label"],
            kwargs.get("results_wanted"),
            kwargs.get("hours_old"),
            kwargs.get("linkedin_fetch_description", True),
        )

        self.last_fetch = {"duration_ms": 0, "requests_made": 0}
        started = time.monotonic()
        with warnings.catch_warnings():
            # JobSpy emits pandas FutureWarnings on concat of empty frames.
            warnings.simplefilter("ignore")
            with (
                capture_scraper_errors(task["source"]) as reported,
                count_requests() as calls,
                guest_description_endpoint(task["source"] == "linkedin"),
            ):
                try:
                    frame = scrape(**kwargs)
                finally:
                    # In `finally`, because a failed cell's cost is the interesting one:
                    # a rate limit costs the requests that earned it, and the caller
                    # records the failure with the same numbers as a success.
                    self.last_fetch = {
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "requests_made": calls.count,
                    }

        rows = frame_to_rows(frame)
        # Archive before raising: a truncated payload is exactly the one worth replaying.
        if self.archive_dir:
            self._archive(task, rows)

        if reported.messages:
            # Re-raise carrying JobSpy's own wording, because SourceCircuit classifies on
            # message text -- "Read timed out" lands as transient and gets retried on a
            # fresh exit IP, "429" as a rate limit that trips the source.
            raise ScraperReportedError(
                f"{task['source']} reported {len(reported.messages)} error(s) after "
                f"{len(rows)} row(s): {'; '.join(reported.messages[:3])}",
                board_messages=reported.messages,
            )
        return rows

    def _archive(self, task: ScrapeTaskPayload, rows: list[dict[str, Any]]) -> None:
        """Persist the raw payload so normalizer bugs can be replayed without re-scraping."""
        if not self.archive_dir:
            return
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            slug = _slug(f"{task['source']}-{task['query']}-{task['location_id']}")
            path = os.path.join(self.archive_dir, f"{stamp}-{slug}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"task": task, "rows": rows}, handle, default=str)
        except OSError as exc:
            logger.warning(f"could not archive raw payload: {exc}")


def prune_archives(archive_dir: str | None, max_age_days: float = 14) -> tuple[int, int]:
    """Delete raw payloads older than the retention window. Returns (removed, freed_bytes).

    The archive exists so a normalizer bug can be replayed without re-scraping, which is
    worth real disk -- but only for as long as a payload could plausibly be replayed. At
    ~0.6MB per cell and 280 cells a day, unbounded retention is tens of GB a year of files
    nobody will ever open. Age is taken from mtime rather than the filename stamp so a
    partially-written or hand-copied file is still collected.

    Failures are logged and swallowed: housekeeping must never be the thing that fails a
    scrape run.
    """
    if not archive_dir or not os.path.isdir(archive_dir) or not max_age_days:
        return 0, 0

    cutoff = datetime.now(timezone.utc).timestamp() - (float(max_age_days) * 86400)
    removed = 0
    freed = 0
    for name in os.listdir(archive_dir):
        path = os.path.join(archive_dir, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            size = os.path.getsize(path)
            os.remove(path)
            removed += 1
            freed += size
        except OSError as exc:
            logger.warning(f"could not prune archive {name}: {exc}")

    if removed:
        logger.info(
            f"Pruned {removed} raw payload(s) older than {max_age_days}d "
            f"({freed / 1048576:.1f} MB freed)"
        )
    return removed, freed


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
    """Translate a ScrapeTask payload into scrape_jobs() arguments."""
    source = task["source"]
    kwargs: ScrapeJobsKwargs = {
        "site_name": [source],
        "search_term": task["query"],
        "location": task["location_label"] or None,
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
        if not task["is_remote"] and task["distance"]:
            kwargs["distance"] = task["distance"]
    elif source == "linkedin":
        # All-or-nothing, matching the cell's desc_selection: 'census' when the budget
        # affords a description per posting, 'none' otherwise. Never a top-scoring subset
        # -- selecting on pre-score correlates with the user's own skills, which is exactly
        # the bias the skill-demand denominators must not contain.
        kwargs["linkedin_fetch_description"] = task["fetch_description"]

    if task["proxies"]:
        kwargs["proxies"] = task["proxies"]

    return kwargs


def frame_to_rows(frame: Any) -> list[dict[str, Any]]:
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
            f"JobSpy returned {len(missing)} fewer columns than expected: {sorted(missing)[:8]}"
        )

    return frame.to_dict(orient="records")


def _slug(text: Any) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in str(text))[:80]
