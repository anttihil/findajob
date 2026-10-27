"""Review first counts the same filtered postings as the dashboard feed."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from findajob.core.database import Database
from findajob.web.app import app


@pytest.fixture
def dashboard(tmp_path: Path) -> Iterator[tuple[TestClient, Database]]:
    path = str(tmp_path / "jobs.db")
    db = Database(path)
    now = datetime.now(timezone.utc)
    rows = [
        (1, "Acme", "Engineer", "US", "unread", "first", None, None, 0),
        (2, "ACME, Inc.", "Engineer", "FI", "unread", "first", None, None, 60),
        (3, "Beta", "Engineer", "US", "unread", "first", 1, None, 0),
        (4, "Saved", "Engineer", "US", "saved", "first", None, None, 0),
        (5, "Duplicate", "Engineer", "US", "unread", "first", None, 1, 0),
        (6, "Consider", "Engineer", "US", "unread", "consider", None, None, 0),
        (7, None, "Engineer", "US", "unread", "first", None, None, 0),
        (8, None, "Engineer", "US", "unread", "first", None, None, 0),
        (9, "Old", "Engineer", "US", "unread", "first", None, None, 60),
        (10, "Acme", "Engineer", "US", "unread", "low", None, None, 0),
        (11, "Acme", "", "US", "unread", "first", None, None, 0),
    ]
    for job_id, company, title, country, status, priority, fit, duplicate, age in rows:
        posted = (now - timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, company, title, country, status, duplicate_of, "
            "date_posted, date_found, url, sync_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                job_id,
                str(job_id),
                company,
                title,
                country,
                status,
                duplicate,
                posted,
                posted,
                f"https://example.test/{job_id}",
            ),
        )
        db.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit, priority, stage, "
            "reason_type, created_at) VALUES (?, 1, 'test', ?, ?, 'assessed', ?, ?)",
            (job_id, fit, priority, "match" if fit else "experience", posted),
        )
    db.conn.commit()
    with patch("findajob.web.app.get_db", side_effect=lambda: Database(path)):
        yield TestClient(app), db
    db.close()


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, 7),
        ({"country": "FI"}, 1),
        ({"country": "US"}, 6),
        ({"status": "saved"}, 1),
        ({"status": "rejected"}, 0),
        ({"fit": "true"}, 7),
        ({"fit": "false"}, 7),
        ({"q": "Beta"}, 1),
        ({"date_posted": "7d"}, 5),
        ({"stage": "assessed"}, 7),
        ({"stage": "historical"}, 7),
        ({"reason_type": "match"}, 7),
        ({"liveness": "live"}, 0),
        ({"location": "missing"}, 0),
        ({"country": "FI", "date_posted": "7d"}, 0),
    ],
)
def test_review_first_matches_filtered_feed(
    dashboard: tuple[TestClient, Database], filters: dict[str, str], expected: int
) -> None:
    client, _ = dashboard
    params = {"status": "unread", **filters, "priority": "first", "limit": "1"}
    stats = client.get("/api/stats", params=params)
    feed = client.get("/api/jobs", params=params)
    assert stats.status_code == feed.status_code == 200
    assert stats.json()["review_first"] == feed.json()["total"] == expected
    assert stats.json()["total_jobs"] == 11


def test_review_first_ignores_current_priority_and_pagination_without_polluting_cache(
    dashboard: tuple[TestClient, Database],
) -> None:
    client, db = dashboard
    assert db.get_stats()["review_first"] == 8
    for country, expected in [("FI", 1), ("US", 6), ("FI", 1)]:
        stats = client.get(
            "/api/stats",
            params={"country": country, "priority": "consider", "limit": 1, "offset": 100},
        )
        assert stats.status_code == 200
        assert stats.json()["review_first"] == expected
    assert db.get_stats()["review_first"] == 8


def test_review_first_recounts_postings_after_status_changes(
    dashboard: tuple[TestClient, Database],
) -> None:
    client, _ = dashboard
    assert client.get("/api/stats").json()["review_first"] == 7
    # Each posting contributes independently, even with the same company and title.
    assert client.put("/api/jobs/1/status", json={"status": "saved"}).status_code == 200
    assert client.get("/api/stats").json()["review_first"] == 6
    assert client.put("/api/jobs/2/status", json={"status": "saved"}).status_code == 200
    assert client.get("/api/stats").json()["review_first"] == 5
    assert client.get("/api/stats?status=saved").json()["review_first"] == 3


def test_retired_filters_do_not_restrict_detail_navigation(
    dashboard: tuple[TestClient, Database],
) -> None:
    client, _ = dashboard
    filters = {"priority": "first", "fit": "false", "stage": "reviewed", "reason_type": "skills"}
    feed = client.get("/api/jobs", params={"status": "unread", **filters}).json()
    ids = [job["id"] for job in feed["jobs"]]
    assert len(ids) == 7
    assert all("stage" not in job for job in feed["jobs"])
    context = client.get(f"/api/jobs/{ids[0]}/context", params=filters)
    assert context.status_code == 200
    assert context.json()["next_job_id"] == ids[1]
    assert "stage" not in context.json()["job"]
