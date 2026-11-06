"""Pinned Google page methods with explicit transport and strict response handling."""

import re
from collections.abc import Generator
from typing import Any

from jobspy.google import Google
from jobspy.model import ScraperInput

from findajob.search.sources.base import SearchPage
from findajob.search.sources.jobspy_source import posting_row
from findajob.search.traffic import PageSession, SourceTraffic


class GoogleSession(PageSession):
    def get(self, url: str, **kwargs: Any) -> Any:
        response = super().get(url, **kwargs, timeout=10)
        text = response.text.lower()
        if any(
            marker in text
            for marker in ("/sorry/", "unusual traffic", "recaptcha", "consent.google.com")
        ):
            raise ValueError("Google Jobs returned a challenge or consent page")
        return response


class GooglePages(Google):
    """Reuse JobSpy parsing while counting records before its URL deduplication."""

    raw_count = 0

    def _parse_job(self, job_info: Any) -> Any:
        if not isinstance(job_info, list) or len(job_info) < 4 or not job_info[0]:
            raise ValueError("Malformed Google Jobs record")
        self.raw_count += 1
        info = list(job_info) + [None] * max(0, 29 - len(job_info))
        # Optional fields in the pinned parser assume non-null descriptions and dates
        # expressed in days. Unknown age labels must remain unknown.
        description = info[19]
        info[19] = description or ""
        if not isinstance(info[12], str) or not re.fullmatch(r"\d+ days? ago", info[12]):
            info[12] = None
        if not info[3] or not info[3][0]:
            return None
        post = super()._parse_job(info)
        if post:
            post.description = description
            if info[28] is None:
                post.id = None
        return post


def google_pages(scraper_input: ScraperInput, traffic: SourceTraffic) -> Generator[SearchPage]:
    client: Any = GooglePages()
    client.scraper_input = scraper_input
    session = GoogleSession(traffic)
    client.session = session
    cursor = None
    cursors: set[str] = set()
    while True:
        client.raw_count = 0
        if cursor is None:
            next_cursor, jobs = client._get_initial_cursor_and_jobs()
            # The upstream initial parser returns [] for arbitrary HTML. Accept only
            # explicit empty search responses; changed markup must surface as failure.
            if not client.raw_count:
                text = session.last_response.text.lower()
                if "520084652" in text or not any(
                    marker in text for marker in ("no jobs found", "did not match any jobs")
                ):
                    raise ValueError("Unrecognized Google Jobs initial response")
        else:
            jobs, next_cursor = client._get_jobs_next_page(cursor)
        reason = None
        if not client.raw_count or not next_cursor:
            reason = "exhausted"
        elif next_cursor in cursors:
            reason = "pagination_limit"
        else:
            cursors.add(next_cursor)
            cursor = next_cursor
        yield SearchPage(
            [posting_row(job, "google") for job in jobs],
            end_reason=reason,
            raw_count=client.raw_count,
        )
        if reason:
            return
