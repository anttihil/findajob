"""Unit and integration tests for URL job import, scoring, and resume generation."""

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from careerradar.cli import build_parser
from careerradar.core.database import Database
from careerradar.profile.models import Profile
from careerradar.search.importer import (
    ExtractedJobPosting,
    clean_html_to_text,
    extract_job_from_text,
    fetch_url_text,
    import_and_process_job,
    import_job_from_url,
)
from careerradar.web.app import app


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(db_path=tmp.name)
        yield db
        db.close()


def test_clean_html_to_text():
    html_sample = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Career Page</title>
        <style>body { color: red; }</style>
        <script>console.log("ignore me");</script>
      </head>
      <body>
        <h1>Senior Backend Engineer</h1>
        <p>Company: <strong>Acme AI</strong></p>
        <div>Location: Remote &amp; Worldwide</div>
        <p>Requirements:</p>
        <ul>
          <li>Python 3.10+</li>
          <li>FastAPI &amp; PostgreSQL</li>
        </ul>
      </body>
    </html>
    """
    text = clean_html_to_text(html_sample)
    assert "ignore me" not in text
    assert "color: red" not in text
    assert "Senior Backend Engineer" in text
    assert "Acme AI" in text
    assert "Remote & Worldwide" in text
    assert "Python 3.10+" in text


def test_clean_html_to_text_with_meta_tags():
    html_sample = """
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta property="og:title" content="Software Engineer">
        <link rel="canonical" href="https://example.com/jobs/1">
        <title>Software Engineer at Acme</title>
        <script>var x = 1;</script>
      </head>
      <body>
        <div>About the role: We are looking for an engineer.</div>
      </body>
    </html>
    """
    text = clean_html_to_text(html_sample)
    assert "Software Engineer at Acme" in text
    assert "About the role: We are looking for an engineer." in text
    assert "var x = 1" not in text


def test_clean_html_to_text_json_ld():
    html_sample = """
    <!DOCTYPE html>
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Principal Architect",
          "hiringOrganization": {"@type": "Organization", "name": "GlobalTech"},
          "description": "<p>Design distributed multi-cloud services.</p>"
        }
        </script>
      </head>
      <body>
        <div id="root"></div>
      </body>
    </html>
    """
    text = clean_html_to_text(html_sample)
    assert "Job Title: Principal Architect" in text
    assert "Company: GlobalTech" in text
    assert "Design distributed multi-cloud services." in text


@patch("urllib.request.urlopen")
def test_fetch_url_text(mock_urlopen: MagicMock):
    mock_resp = MagicMock()
    mock_resp.read.return_value = (
        b"<html><body><h1>Staff Engineer</h1><p>Great job at TechCorp</p></body></html>"
    )
    mock_resp.headers.get_content_charset.return_value = "utf-8"
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    text = fetch_url_text("https://example.com/jobs/123")
    assert "Staff Engineer" in text
    assert "Great job at TechCorp" in text


@patch("urllib.request.urlopen")
def test_fetch_url_text_gzip(mock_urlopen: MagicMock):
    import gzip

    raw_html = b"<html><body><h1>Platform Engineer</h1><p>Compressed content</p></body></html>"
    compressed = gzip.compress(raw_html)

    mock_resp = MagicMock()
    mock_resp.read.return_value = compressed
    mock_resp.headers.get.return_value = "gzip"
    mock_resp.headers.get_content_charset.return_value = "utf-8"
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    text = fetch_url_text("https://example.com/jobs/gzip-test")
    assert "Platform Engineer" in text
    assert "Compressed content" in text


@patch("careerradar.core.llm.structured_model")
def test_extract_job_from_text(mock_model_fn: MagicMock):
    mock_chain = MagicMock()
    mock_chain.invoke.return_value = ExtractedJobPosting(
        title="Senior Python Engineer",
        company="Radar Labs",
        location="Remote",
        description="Build awesome agents and pipelines.",
        is_remote=True,
        seniority="senior",
        salary_min=140000,
        salary_max=180000,
        salary_currency="USD",
        salary_interval="yearly",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_model_fn.return_value = mock_llm

    result = extract_job_from_text("some sample text", url="https://example.com/job")
    assert result["title"] == "Senior Python Engineer"
    assert result["company"] == "Radar Labs"
    assert result["salary_min"] == 140000


@patch("careerradar.search.importer.extract_job_from_text")
def test_import_job_from_url(mock_extract: MagicMock, temp_db: Database):
    mock_extract.return_value = {
        "title": "Lead Software Architect",
        "company": "NextGen Systems",
        "location": "Stockholm, Sweden",
        "description": "Lead the core backend architecture using Python, asyncio, and SQLite.",
        "is_remote": False,
        "seniority": "lead",
        "salary_min": 80000,
        "salary_max": 100000,
        "salary_currency": "EUR",
        "salary_interval": "yearly",
    }

    sample_html_text = (
        "Lead Software Architect at NextGen Systems Stockholm Sweden " * 5
    )  # > 50 chars
    job_id = import_job_from_url(
        url="https://boards.greenhouse.io/nextgen/jobs/456",
        db=temp_db,
        html_text=sample_html_text,
    )
    assert job_id > 0

    # Verify job record in database
    res = temp_db.query_jobs(job_id=job_id, detail=True)
    jobs = res.get("jobs") or []
    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Lead Software Architect"
    assert job["company"] == "NextGen Systems"
    assert job["source"] == "greenhouse"
    assert job["pipeline_state"] == "new"

    # Verify deduplication / re-import
    job_id_2 = import_job_from_url(
        url="https://boards.greenhouse.io/nextgen/jobs/456",
        db=temp_db,
        html_text=sample_html_text,
    )
    assert job_id_2 == job_id


@patch("careerradar.scoring.worker.build_graph")
@patch("careerradar.scoring.worker.load_active")
def test_score_job_success(
    mock_load_active: MagicMock,
    mock_build_graph: MagicMock,
    temp_db: Database,
):
    from careerradar.scoring.worker import score_job
    from careerradar.search.repository import upsert_posting

    # Seed an active profile
    mock_load_active.return_value = (1, Profile(name="Jane"), "Candidate summary text")

    # Insert a job
    job_id, _ = upsert_posting(
        temp_db.conn,
        {
            "job_key": "k1",
            "title": "Backend Dev",
            "company": "Alpha Inc",
            "url": "https://alpha.com/job/1",
            "description": "Python, SQLite, REST APIs",
            "pipeline_state": "new",
        },
    )
    assert job_id is not None

    # Mock scoring graph
    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {
        "verdict": {
            "fit": True,
            "reason_type": "strong_match",
            "reason_description": "Strong Python and SQLite background matches role.",
        },
        "usage": {"prompt": 100, "cache_hit": 50, "cache_miss": 50, "completion": 20},
        "attempts": 1,
    }
    mock_build_graph.return_value = mock_graph

    verdict = score_job(job_id, db=temp_db)
    assert verdict["fit"] is True
    assert verdict["reason_type"] == "strong_match"

    # Verify job state changed to scored and verdict was saved
    res = temp_db.query_jobs(job_id=job_id, detail=True)
    job = (res.get("jobs") or [])[0]
    assert job["pipeline_state"] == "scored"
    assert bool(job["fit"]) is True


@patch("careerradar.search.importer.import_job_from_url")
@patch("careerradar.scoring.worker.score_job")
@patch("careerradar.profile.builder.build_resume_for_job")
def test_import_and_process_job(
    mock_resume: MagicMock,
    mock_score: MagicMock,
    mock_import: MagicMock,
    temp_db: Database,
):
    mock_import.return_value = 42
    mock_score.return_value = {"fit": True, "reason_type": "strong_match"}
    mock_resume.return_value = {
        "docx_path": "/tmp/resume.docx",
        "pdf_path": "/tmp/resume.pdf",
        "ats_score": 9.5,
        "ats_verdict": "STRONG_MATCH",
    }

    res = import_and_process_job(
        url="https://example.com/job/42",
        score=True,
        generate_resume=True,
        db=temp_db,
    )
    assert res["job_id"] == 42
    assert res["verdict"]["fit"] is True
    assert res["resume"]["ats_score"] == 9.5
    mock_import.assert_called_once()
    mock_score.assert_called_once_with(42, db=temp_db, model=None)
    mock_resume.assert_called_once_with(42, db=temp_db)


@patch("careerradar.search.importer.import_and_process_job")
def test_api_import_job(mock_process: MagicMock):
    mock_process.return_value = {
        "job_id": 99,
        "job": {"id": 99, "title": "AI Engineer", "company": "DeepMind"},
        "verdict": {"fit": True},
        "resume": None,
    }

    client = TestClient(app)
    resp = client.post(
        "/api/jobs/import",
        json={"url": "https://deepmind.google/careers/99", "score": True, "generate_resume": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["job_id"] == 99
    assert data["job"]["title"] == "AI Engineer"


@patch("careerradar.search.importer.import_and_process_job")
def test_api_import_job_value_error(mock_process: MagicMock):
    mock_process.side_effect = ValueError("Could not extract sufficient text from https://example.com/job/bad")

    client = TestClient(app)
    resp = client.post(
        "/api/jobs/import",
        json={"url": "https://example.com/job/bad", "score": True, "generate_resume": False},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "Could not extract sufficient text" in data["detail"]


@patch("careerradar.search.importer.fetch_url_text")
def test_import_job_from_url_network_error(mock_fetch: MagicMock, temp_db: Database):
    import urllib.error

    mock_fetch.side_effect = urllib.error.HTTPError(
        url="https://example.com/job/404",
        code=404,
        msg="Not Found",
        hdrs={},  # type: ignore[arg-type]
        fp=None,
    )

    with pytest.raises(ValueError, match="HTTP 404 Not Found"):
        import_job_from_url(url="https://example.com/job/404", db=temp_db)


def test_cli_import_parser():
    parser = build_parser()
    args = parser.parse_args(["import", "https://example.com/job/1", "--resume"])
    assert args.command == "import"
    assert args.url == "https://example.com/job/1"
    assert args.resume is True
    assert args.no_score is False
