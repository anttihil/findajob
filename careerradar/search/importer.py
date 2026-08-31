"""Direct job URL importer, extractor, and pipeline orchestrator.

Fetches job posting HTML, strips markup, extracts structured fields via LLM,
persists to the jobs database, and coordinates optional scoring and resume tailoring.
"""

import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, Field

from careerradar.core.database import Database
from careerradar.core.logger import get_logger

logger = get_logger()

EXTRACTION_SYSTEM_PROMPT = """You are an expert job posting extractor.
Your task is to extract structured job details from the provided webpage text.
Make sure to extract the full job description, requirements, and qualifications
into the 'description' field.
If salary information or location is available, extract it accurately.
"""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._ignore_tags = {"script", "style", "head", "meta", "noscript", "svg"}
        self._current_ignored = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],  # noqa: ARG002
    ) -> None:
        if tag.lower() in self._ignore_tags:
            self._current_ignored += 1
        elif tag.lower() in {
            "p",
            "div",
            "br",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "tr",
            "section",
            "article",
        }:
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._ignore_tags and self._current_ignored > 0:
            self._current_ignored -= 1
        elif tag.lower() in {
            "p",
            "div",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "tr",
            "section",
            "article",
        }:
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if self._current_ignored == 0:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw_text = "".join(self._pieces)
        unescaped = html.unescape(raw_text)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in unescaped.splitlines()]
        cleaned = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def clean_html_to_text(html_content: str) -> str:
    """Strip scripts, styles, and markup from HTML while preserving readable text."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html_content)
    return extractor.get_text()


def fetch_url_text(url: str, timeout: int = 15) -> str:
    """Fetch URL contents and convert HTML to clean plaintext."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw_bytes = resp.read()
        try:
            content = raw_bytes.decode(charset, errors="replace")
        except LookupError:
            content = raw_bytes.decode("utf-8", errors="replace")
        return clean_html_to_text(content)


class ExtractedJobPosting(BaseModel):
    """Schema for extracting structured job posting data from web content."""

    title: str = Field(description="The job title, e.g. 'Senior Backend Engineer'")
    company: str = Field(description="The employer/company name offering the job")
    location: str | None = Field(
        default=None,
        description="Location, e.g. 'San Francisco, CA', 'Stockholm, Sweden', or 'Remote'",
    )
    description: str = Field(
        description="Comprehensive description text of the job, responsibilities, and requirements"
    )
    is_remote: bool | None = Field(default=None, description="Whether the job is explicitly remote")
    seniority: str | None = Field(
        default=None,
        description="Seniority level: entry, mid, senior, lead, principal, etc.",
    )
    salary_min: float | None = Field(default=None, description="Minimum salary if specified")
    salary_max: float | None = Field(default=None, description="Maximum salary if specified")
    salary_currency: str | None = Field(
        default=None, description="Salary currency code: USD, EUR, SEK, GBP, etc."
    )
    salary_interval: str | None = Field(
        default=None, description="Salary interval: yearly, monthly, hourly"
    )


def extract_job_from_text(
    text: str,
    url: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Use structured model output to parse job fields from raw page text."""
    from careerradar.core.llm import structured_model

    llm = structured_model(model, role="scoring")
    chain = llm.with_structured_output(ExtractedJobPosting, method="function_calling", strict=True)

    truncated_text = text[:25000]
    messages = [
        ("system", EXTRACTION_SYSTEM_PROMPT),
        ("user", f"Source URL: {url}\n\nWebpage content:\n{truncated_text}"),
    ]

    extracted: ExtractedJobPosting = chain.invoke(messages)
    return extracted.model_dump()


def import_job_from_url(
    url: str,
    db: Database | None = None,
    model: str | None = None,
    html_text: str | None = None,
) -> int:
    """Fetch URL, extract job fields, and persist to database. Returns job_id."""
    from careerradar.search.normalizer import normalize_row
    from careerradar.search.repository import upsert_posting

    text = html_text if html_text is not None else fetch_url_text(url)
    if not text or len(text.strip()) < 50:
        raise ValueError(f"Could not extract sufficient text from {url}")

    data = extract_job_from_text(text, url=url, model=model)
    if not data.get("title") or not data.get("company"):
        raise ValueError("Failed to extract valid job title or company from URL content")

    hostname = urllib.parse.urlparse(url).netloc.lower()
    source = "direct"
    if "linkedin" in hostname:
        source = "linkedin"
    elif "indeed" in hostname:
        source = "indeed"
    elif "greenhouse" in hostname:
        source = "greenhouse"
    elif "lever.co" in hostname:
        source = "lever"
    elif "ashbyhq" in hostname:
        source = "ashby"

    raw_row = {
        "title": data.get("title"),
        "company": data.get("company"),
        "job_url": url,
        "description": data.get("description"),
        "site": source,
        "location": data.get("location"),
        "is_remote": data.get("is_remote"),
        "seniority": data.get("seniority"),
        "min_amount": data.get("salary_min"),
        "max_amount": data.get("salary_max"),
        "currency": data.get("salary_currency"),
        "interval": data.get("salary_interval"),
    }

    task = {
        "source": source,
        "role_family": "",
        "location_id": "",
        "country": "",
        "hours_old": None,
    }

    normalized = normalize_row(raw_row, task)
    normalized["pipeline_state"] = "new"

    owned = db is None
    database = db or Database()
    try:
        job_id, _is_new = upsert_posting(database.conn, normalized)
        database.conn.commit()
        if job_id is None:
            existing = database.conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
            if existing:
                job_id = existing["id"]
            else:
                raise RuntimeError("Failed to insert or retrieve job record")
        return job_id
    finally:
        if owned:
            database.close()


def import_and_process_job(
    url: str,
    score: bool = True,
    generate_resume: bool = False,
    model: str | None = None,
    db: Database | None = None,
    html_text: str | None = None,
) -> dict[str, Any]:
    """Import a job from URL, optionally score it, and optionally generate a resume."""
    from careerradar.profile.builder import build_resume_for_job
    from careerradar.scoring.worker import score_job

    owned = db is None
    database = db or Database()
    try:
        job_id = import_job_from_url(url, db=database, model=model, html_text=html_text)

        verdict = None
        if score:
            try:
                verdict = score_job(job_id, db=database, model=model)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Scoring failed during import for job %d: %s", job_id, exc)

        resume = None
        if generate_resume:
            resume = build_resume_for_job(job_id, db=database)

        job_res = database.query_jobs(job_id=job_id, detail=True)
        jobs = job_res.get("jobs") or []
        job = jobs[0] if jobs else None

        return {
            "job_id": job_id,
            "job": job,
            "verdict": verdict,
            "resume": resume,
        }
    finally:
        if owned:
            database.close()
