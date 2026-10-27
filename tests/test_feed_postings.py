"""Dashboard feeds expose individual postings with their own IDs and statuses."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from findajob.core.database import Database
from findajob.web.app import app


@pytest.fixture
def postings(tmp_path: Path) -> Iterator[Database]:
    db = Database(str(tmp_path / "jobs.db"))
    rows = [
        (1, "Acme, Inc.", "Senior Platform Engineer (Remote)", "CA, US", "indeed", "01"),
        (2, "ACME", "Senior Platform Engineer", "NY, US", "linkedin", "03"),
        (3, "Acme LLC", "Senior Platform Engineer", "TX, US", "indeed", "02"),
        (4, "ACME", "Senior Platform Engineer", "NY, US", "linkedin", "03"),
    ]
    for job_id, company, title, location, source, day in rows:
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, company, title, location, url, source, status, "
            "date_found, sync_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', ?, 1)",
            (
                job_id,
                f"job-{job_id}",
                company,
                title,
                location,
                f"https://example.test/{job_id}",
                source,
                f"2026-09-{day}T00:00:00Z",
            ),
        )
    db.conn.commit()
    yield db
    db.close()


def test_same_company_title_and_location_postings_remain_visible(postings: Database) -> None:
    page = postings.query_jobs(status="unread")

    assert page["total"] == 4
    assert [job["id"] for job in page["jobs"]] == [4, 2, 3, 1]
    assert postings.job_ids_for(status="unread") == [4, 2, 3, 1]
    assert all("listing_count" not in job for job in page["jobs"])
    assert page["jobs"][-1]["company"] == "Acme, Inc."
    assert page["jobs"][-1]["title"] == "Senior Platform Engineer (Remote)"


def test_pagination_counts_and_visits_each_posting(postings: Database) -> None:
    first = postings.query_jobs(status="unread", limit=2)
    second = postings.query_jobs(status="unread", limit=2, offset=2)

    assert first["total"] == second["total"] == 4
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert [job["id"] for job in first["jobs"]] == [4, 2]
    assert [job["id"] for job in second["jobs"]] == [3, 1]
    assert postings.job_ids_for(status="unread", limit=2) == [4, 2]
    assert postings.job_ids_for(status="unread", limit=2, offset=2) == [3, 1]


def test_source_filter_preserves_distinct_matching_postings(postings: Database) -> None:
    page = postings.query_jobs(status="unread", source="linkedin")

    assert page["total"] == 2
    assert [job["id"] for job in page["jobs"]] == [4, 2]
    assert postings.job_ids_for(status="unread", source="linkedin") == [4, 2]


def test_duplicate_filter_remains_explicit(postings: Database) -> None:
    postings.conn.execute("UPDATE jobs SET duplicate_of = 2 WHERE id = 4")
    postings.conn.commit()

    assert [job["id"] for job in postings.query_jobs()["jobs"]] == [2, 3, 1]
    assert postings.query_jobs()["total"] == 3
    assert postings.job_ids_for() == [2, 3, 1]
    assert [job["id"] for job in postings.query_jobs(include_duplicates=True)["jobs"]] == [
        4,
        2,
        3,
        1,
    ]
    assert postings.job_ids_for(include_duplicates=True) == [4, 2, 3, 1]


def test_api_detail_navigation_and_status_target_individual_postings(postings: Database) -> None:
    with patch("findajob.web.app.get_db", side_effect=lambda: Database(postings.db_path)):
        client = TestClient(app)
        feed = client.get("/api/jobs", params={"status": "unread", "limit": 2})
        assert feed.status_code == 200
        assert feed.json()["total"] == 4
        assert [job["id"] for job in feed.json()["jobs"]] == [4, 2]

        context = client.get("/api/jobs/4/context", params={"limit": 2})
        assert context.status_code == 200
        assert context.json()["job"]["id"] == 4
        assert context.json()["next_job_id"] == 2

        next_page = client.get("/api/jobs/3/context", params={"limit": 2, "offset": 2})
        assert next_page.status_code == 200
        assert next_page.json()["next_job_id"] == 1

        assert client.put("/api/jobs/4/status", json={"status": "saved"}).status_code == 200
        unread = client.get("/api/jobs", params={"status": "unread"})
        assert unread.status_code == 200
        assert unread.json()["total"] == 3
        assert [job["id"] for job in unread.json()["jobs"]] == [2, 3, 1]
        saved = client.get("/api/jobs", params={"status": "saved"})
        assert saved.status_code == 200
        assert [job["id"] for job in saved.json()["jobs"]] == [4]


def test_feed_omits_descriptions_but_drawer_returns_them(postings: Database) -> None:
    postings.conn.execute("UPDATE jobs SET description = 'Full job details' WHERE id = 4")
    postings.conn.commit()
    with patch("findajob.web.app.get_db", side_effect=lambda: Database(postings.db_path)):
        client = TestClient(app)
        page = client.get("/api/jobs", params={"limit": 1}).json()
        assert page["total"] == 4
        assert page["jobs"][0]["id"] == 4
        assert "description" not in page["jobs"][0]
        context = client.get("/api/jobs/4/context").json()
        assert context["job"]["description"] == "Full job details"


def test_id_page_keeps_wide_projections_out_of_window(postings: Database) -> None:
    statements: list[str] = []
    postings.conn.set_trace_callback(statements.append)
    try:
        page = postings.query_jobs(limit=2, offset=1, detail=True)
    finally:
        postings.conn.set_trace_callback(None)
    assert page["total"] == 4
    assert [job["id"] for job in page["jobs"]] == [2, 3]
    sql = next(sql for sql in statements if "WITH page AS MATERIALIZED" in sql)
    narrow, outer = sql.split("SELECT jobs.id", 1)[1].split("SELECT", 1)
    assert "COUNT(*) OVER ()" in narrow
    assert "description" not in narrow
    assert "tailored_resume_id" not in narrow
    assert "jobs.description" in outer
    plan = [row[3] for row in postings.conn.execute("EXPLAIN QUERY PLAN " + sql)]
    assert "MATERIALIZE page" in plan
    assert "SEARCH jobs USING INTEGER PRIMARY KEY (rowid=?)" in plan
