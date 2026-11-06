"""Latest query attribution stays consistent across the feed, counters, and drawer."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from findajob.core.database import Database
from findajob.web.app import app

TERM = 'Platform & "SRE" + C++'


@pytest.fixture
def query_jobs(tmp_path: Path) -> Iterator[tuple[TestClient, Database]]:
    db = Database(str(tmp_path / "jobs.db"))
    db.conn.executemany(
        "INSERT INTO scrape_cells (id, source, location_id, query, search_label, country, "
        "indeed_country, is_remote, distance, created_at, enabled) "
        "VALUES (?, ?, ?, ?, 'Remote', 'US', 'usa', 1, 50, '2026-01-01', ?)",
        [
            (1, "indeed", "remote", TERM, 1),
            (2, "linkedin", "remote", TERM, 1),
            (3, "indeed", "remote", "Backend", 0),
        ],
    )
    for job_id, cell_id, status in [
        (1, 1, "unread"),
        (2, 2, "unread"),
        (3, 3, "unread"),
        (4, None, "unread"),
        (5, 1, "saved"),
    ]:
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, company, url, scrape_cell_id, status, "
            "date_found) VALUES (?, ?, 'Backend developer', 'Example', ?, ?, ?, '2026-11-01')",
            (job_id, str(job_id), f"https://example.test/{job_id}", cell_id, status),
        )
        db.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, priority, created_at) "
            "VALUES (?, 1, 'test', 'first', '2026-11-01')",
            (job_id,),
        )
    db.conn.execute("INSERT INTO search_queries (query, enabled) VALUES ('No results', 0)")
    db.conn.commit()
    with patch("findajob.web.app.get_db", side_effect=lambda: Database(db.db_path)):
        yield TestClient(app), db
    db.close()


def test_query_is_exact_provenance_and_combines_with_other_filters(
    query_jobs: tuple[TestClient, Database],
) -> None:
    client, db = query_jobs
    params = {"search_query": TERM, "status": "unread", "limit": 1}
    page = client.get("/api/jobs", params=params).json()
    assert page["total"] == 2
    assert page["has_more"] is True
    assert [(job["id"], job["search_query"]) for job in page["jobs"]] == [(2, TERM)]
    second = client.get("/api/jobs", params={**params, "offset": 1}).json()
    assert [job["id"] for job in second["jobs"]] == [1]
    assert second["has_more"] is False
    assert db.job_ids_for(search_query=TERM, status="unread") == [2, 1]
    assert client.get("/api/jobs", params={**params, "q": "Backend"}).json()["total"] == 2
    assert client.get("/api/jobs", params={**params, "location": "missing"}).json()["total"] == 0
    assert (
        client.get("/api/jobs", params={**params, "search_query": "Platform"}).json()["total"] == 0
    )
    assert (
        client.get("/api/jobs", params={**params, "search_query": "Backend"}).json()["total"] == 1
    )
    assert (
        client.get("/api/jobs", params={**params, "search_query": "' OR 1=1 --"}).json()["total"]
        == 0
    )
    assert client.get("/api/jobs", params={**params, "search_query": ""}).json()["total"] == 4


def test_query_scopes_stats_and_next_job_and_survives_status_changes(
    query_jobs: tuple[TestClient, Database],
) -> None:
    client, _ = query_jobs
    params = {"search_query": TERM, "status": "unread"}
    assert client.get("/api/stats", params=params).json()["review_first"] == 2
    context = client.get("/api/jobs/2/context", params=params).json()
    assert context["job"]["search_query"] == TERM
    assert context["next_job_id"] == 1
    assert client.get("/api/jobs/1/context", params=params).json()["next_job_id"] is None
    assert client.put("/api/jobs/2/status", json={"status": "saved"}).status_code == 200
    assert client.get("/api/stats", params=params).json()["review_first"] == 1
    assert client.get("/api/jobs", params=params).json()["total"] == 1


def test_unknown_and_latest_attribution(query_jobs: tuple[TestClient, Database]) -> None:
    client, db = query_jobs
    assert client.get("/api/jobs/4/context").json()["job"]["search_query"] is None
    assert next(job for job in db.query_jobs()["jobs"] if job["id"] == 4)["search_query"] is None
    # A repeat search updates the existing attribution; no match history is introduced.
    db.upsert_posting({"job_key": "1", "scrape_cell_id": 3})
    assert db.query_jobs(job_id=1)["jobs"][0]["search_query"] == "Backend"
    assert db.job_ids_for(search_query=TERM, status="unread") == [2]
    assert db.job_ids_for(search_query="Backend", status="unread") == [3, 1]


def test_choices_include_zero_result_and_historical_terms(
    query_jobs: tuple[TestClient, Database],
) -> None:
    client, _ = query_jobs
    terms = client.get("/api/meta").json()["search_queries"]
    assert terms == ["Backend", "No results", TERM]
