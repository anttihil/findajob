"""LinkedIn detail HTTP and parsing. No persistence or reuse policy lives here."""

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from findajob.search.normalizer import description_quality


@dataclass(frozen=True)
class LinkedInDetails:
    description: str
    company_industry: str | None
    url_direct: str | None


class LinkedInDetailsAdapter:
    """One explicit request per fetch, using the existing JobSpy proxy transport.

    Unlike JobSpy's private detail method, failures propagate to the caller. The
    transport has retries disabled, so rate limits cannot silently multiply cost.
    """

    def __init__(
        self, proxies: list[str], description_format: str = "markdown", *, session: Any = None
    ) -> None:
        self.proxies = proxies
        self.description_format = description_format
        self.last_fetch = {"requests_made": 0, "duration_ms": 0}
        self._session: Any = session
        if session is not None:
            session.headers.update({"User-Agent": "Mozilla/5.0"})

    def fetch(self, site_job_id: str) -> LinkedInDetails:
        job_id = site_job_id.removeprefix("li-")
        if not re.fullmatch(r"\d+", job_id):
            raise ValueError("Invalid LinkedIn job ID")
        if self._session is None:
            from jobspy.util import create_session

            self._session = create_session(
                proxies=self.proxies[0] if self.proxies else None, is_tls=False, has_retry=False
            )
            self._session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.last_fetch = {"requests_made": 1, "duration_ms": 0}
        started = time.monotonic()
        try:
            response = self._session.get(
                f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
                timeout=10,
                allow_redirects=False,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError("LinkedIn detail endpoint redirected or returned no content")
            return parse_details(response.text, self.description_format)
        finally:
            self.last_fetch["duration_ms"] = round((time.monotonic() - started) * 1000)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


def parse_details(html: str, description_format: str = "markdown") -> LinkedInDetails:
    """Parse only detail fields consumed by our normalized posting model."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".show-more-less-html__markup")
    if content is None:
        raise ValueError("LinkedIn detail response has no description")
    for node in [content, *content.find_all(True)]:
        node.attrs = {}
    description = str(content)
    if description_format == "markdown":
        from jobspy.util import markdown_converter

        description = markdown_converter(description)
    elif description_format == "plain":
        description = content.get_text("\n", strip=True)
    if description_quality(description) != "full":
        raise ValueError("LinkedIn detail response has an incomplete description")

    industry = None
    for heading in soup.select(".description__job-criteria-subheader"):
        if heading.get_text(strip=True) == "Industries":
            value = heading.find_next_sibling("span")
            industry = value.get_text(strip=True) if value else None
    direct = None
    apply_code = soup.find("code", id="applyUrl")
    if apply_code:
        match = re.search(r'(?<=\?url=)[^"\s]+', apply_code.decode_contents().strip())
        if match:
            direct = unquote(match.group())
    apply = soup.select_one("a.apply-button, a.topcard__apply-button")
    if apply and not direct:
        href = str(apply.get("href", ""))
        direct = parse_qs(urlparse(href).query).get("url", [None])[0]
        if (
            not direct
            and href.startswith("https://")
            and "linkedin.com" not in urlparse(href).netloc
        ):
            direct = href
    return LinkedInDetails(description, industry, direct)
