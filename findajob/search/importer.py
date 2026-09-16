"""Direct job URL importer, extractor, and pipeline orchestrator.

Fetches job posting HTML, strips markup, extracts structured fields via LLM,
persists to the jobs database, and coordinates optional scoring and resume tailoring.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from findajob.core.database import Database
from findajob.core.logger import get_logger

logger = get_logger()

EXTRACTION_SYSTEM_PROMPT = """You are an expert job posting extractor.
Your task is to extract structured job details from the provided webpage text.
Make sure to extract the full job description, requirements, and qualifications
into the 'description' field.
If salary information or location is available, extract it accurately.
"""

BLOCK_TAGS = {
    "title",
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "tr",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "blockquote",
    "aside",
    "nav",
    "hr",
    "table",
}

NON_CONTENT_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}


def clean_html_to_text(html_content: str) -> str:
    """Strip scripts, styles, and markup from HTML while preserving readable text."""
    soup = BeautifulSoup(html_content, "html.parser")

    # Extract structured schema.org JobPosting data from JSON-LD if present
    json_ld_pieces: list[str] = []
    for script_tag in soup.find_all("script", type="application/ld+json"):
        script_content = script_tag.get_text().strip()
        if script_content:
            try:
                data = json.loads(script_content)
                items = data["@graph"] if isinstance(data, dict) and "@graph" in data else data
                items = items if isinstance(items, list) else [items]
                for item in items:
                    if isinstance(item, dict) and str(item.get("@type", "")).endswith("JobPosting"):
                        title = item.get("title", "")
                        desc = item.get("description", "")
                        if "<" in desc and ">" in desc:
                            desc = BeautifulSoup(desc, "html.parser").get_text(
                                separator="\n", strip=True
                            )
                        hiring_org = item.get("hiringOrganization", {})
                        company = hiring_org.get("name", "") if isinstance(hiring_org, dict) else ""
                        parts = [
                            p
                            for p in [
                                title and f"Job Title: {title}",
                                company and f"Company: {company}",
                                desc,
                            ]
                            if p
                        ]
                        if parts:
                            json_ld_pieces.append("\n".join(parts))
            except Exception:  # noqa: BLE001
                pass

    for tag in soup(NON_CONTENT_TAGS):
        tag.decompose()

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for block in soup.find_all(BLOCK_TAGS):
        block.append("\n")

    raw_text = soup.get_text()
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if json_ld_pieces:
        ld_text = "\n\n".join(json_ld_pieces).strip()
        if ld_text and ld_text not in cleaned:
            cleaned = f"{cleaned}\n\n{ld_text}".strip() if cleaned else ld_text

    return cleaned


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
        raw_bytes = resp.read()
        content_encoding = resp.headers.get("Content-Encoding", "").lower()
        if "gzip" in content_encoding:
            import gzip

            raw_bytes = gzip.decompress(raw_bytes)
        elif "deflate" in content_encoding:
            import zlib

            raw_bytes = zlib.decompress(raw_bytes)

        charset = resp.headers.get_content_charset() or "utf-8"
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
    from findajob.core.llm import structured_model

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
    from findajob.search.normalizer import normalize_row
    from findajob.search.repository import upsert_posting

    if html_text is not None:
        text = html_text
    else:
        try:
            text = fetch_url_text(url)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"HTTP {exc.code} {exc.reason} when fetching {url}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"Network error when fetching {url}: {exc.reason}") from exc

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
    from findajob.profile.builder import build_resume_for_job
    from findajob.scoring.worker import score_job

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
