from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from careerradar.core.database import Database
from careerradar.core.migrations import migrate
from careerradar.market.analytics import MarketAnalytics
from careerradar.search.targets import SearchTargets
from careerradar.web.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_market_yield_api(client: TestClient) -> None:
    res = client.get("/api/market/yield")
    assert res.status_code == 200
    data: dict[str, Any] = res.json()

    assert "summary" in data
    assert "top_queries" in data
    assert "tuples" in data
    assert "by_source" in data
    assert "by_location" in data

    summary = data["summary"]
    assert "total_postings" in summary
    assert "total_scored" in summary
    assert "total_strong_fits" in summary
    assert "overall_fit_rate_pct" in summary
    assert summary["total_postings"] >= summary["total_scored"]
    assert summary["total_scored"] >= summary["total_strong_fits"]

    # Verify query rollup structure
    for q in data["top_queries"][:5]:
        assert "query" in q
        assert "total_postings" in q
        assert "scored_postings" in q
        assert "strong_fits" in q
        assert "fit_rate_pct" in q
        assert "sources" in q
        assert "locations" in q
        assert "yield_category" in q

    # Verify tuple structure
    for t in data["tuples"][:5]:
        assert "source" in t
        assert "query" in t
        assert "location_id" in t
        assert "total_postings" in t
        assert "strong_fits" in t


def test_market_yield_api_filters(client: TestClient) -> None:
    # Ensure test location exists
    client.post(
        "/api/targets/locations",
        json={
            "id": "us_remote",
            "label": "United States (remote)",
            "search_label": "Remote",
            "country": "US",
            "is_remote": True,
            "distance": 50,
            "enabled": True,
        },
    )

    # Filter by source
    res_indeed = client.get("/api/market/yield?source=indeed")
    assert res_indeed.status_code == 200
    data_indeed = res_indeed.json()
    for t in data_indeed["tuples"][:10]:
        assert t["source"] == "indeed"

    # Filter by location
    res_loc = client.get("/api/market/yield?location=us_remote")
    assert res_loc.status_code == 200
    data_loc = res_loc.json()
    for t in data_loc["tuples"][:10]:
        assert t["location_id"] == "us_remote"

    # Filter by query term
    res_q = client.get("/api/market/yield?query=Full%20Stack%20Engineer")
    assert res_q.status_code == 200
    data_q = res_q.json()
    for t in data_q["tuples"][:10]:
        assert t["query"] == "Full Stack Engineer"

    # Invalid location returns 400
    res_bad = client.get("/api/market/yield?location=atlantis")
    assert res_bad.status_code == 400


def test_market_analytics_query_yield_unit(tmp_path: Path) -> None:
    db_path = tmp_path / "test_market_yield.db"
    db = Database(str(db_path))
    migrate(db.conn)

    cur = db.conn.cursor()
    # Insert scrape cell
    cur.execute(
        """
        INSERT INTO scrape_cells (
            id, source, location_id, query, search_label, country, enabled, created_at,
            total_scrapes
        ) VALUES (
            1, 'linkedin', 'us_remote', 'Full Stack Engineer', 'Remote', 'US',
            1, '2026-08-25T00:00:00Z', 2
        )
        """
    )
    cur.execute(
        """
        INSERT INTO scrape_cells (
            id, source, location_id, query, search_label, country, enabled, created_at,
            total_scrapes
        ) VALUES (
            2, 'indeed', 'helsinki', 'Platform Engineer', 'Helsinki, Finland', 'FI',
            1, '2026-08-25T00:00:00Z', 1
        )
        """
    )

    # Insert jobs
    cur.execute(
        """
        INSERT INTO jobs (id, job_key, title, scrape_cell_id, source, date_found, description)
        VALUES (
            101, 'fs1', 'Senior Full Stack Engineer', 1, 'linkedin', '2026-08-25T01:00:00Z', 'desc'
        )
        """
    )
    cur.execute(
        """
        INSERT INTO jobs (id, job_key, title, scrape_cell_id, source, date_found, description)
        VALUES (
            102, 'fs2', 'Lead Full Stack Engineer', 1, 'linkedin', '2026-08-25T02:00:00Z', 'desc'
        )
        """
    )
    cur.execute(
        """
        INSERT INTO jobs (id, job_key, title, scrape_cell_id, source, date_found, description)
        VALUES (
            103, 'pe1', 'Platform Engineer', 2, 'indeed', '2026-08-25T03:00:00Z', 'desc'
        )
        """
    )

    # Insert verdicts (job 101 fit=1, job 102 fit=0, job 103 fit=0)
    cur.execute(
        """
        INSERT INTO job_verdicts (id, job_id, profile_version, model, fit, reason_type, created_at)
        VALUES (1, 101, 1, 'model', 1, 'match', '2026-08-25T01:10:00Z')
        """
    )
    cur.execute(
        """
        INSERT INTO job_verdicts (id, job_id, profile_version, model, fit, reason_type, created_at)
        VALUES (2, 102, 1, 'model', 0, 'skills', '2026-08-25T02:10:00Z')
        """
    )
    cur.execute(
        """
        INSERT INTO job_verdicts (id, job_id, profile_version, model, fit, reason_type, created_at)
        VALUES (3, 103, 1, 'model', 0, 'domain', '2026-08-25T03:10:00Z')
        """
    )
    db.conn.commit()

    targets = SearchTargets([], {})
    analytics = MarketAnalytics(db, config={}, targets=targets)

    result = analytics.query_yield(window_days=30)
    summary = result["summary"]
    assert summary["total_postings"] == 3
    assert summary["total_scored"] == 3
    assert summary["total_strong_fits"] == 1
    assert summary["overall_fit_rate_pct"] == 33.3

    # Check top queries
    assert len(result["top_queries"]) >= 2
    top_q = result["top_queries"][0]
    assert top_q["query"] == "Full Stack Engineer"
    assert top_q["strong_fits"] == 1
    assert top_q["scored_postings"] == 2
    assert top_q["fit_rate_pct"] == 50.0

    # Check tuples
    tuples = result["tuples"]
    fs_tuple = next(t for t in tuples if t["query"] == "Full Stack Engineer")
    assert fs_tuple["source"] == "linkedin"
    assert fs_tuple["location_id"] == "us_remote"
    assert fs_tuple["strong_fits"] == 1
    assert fs_tuple["total_postings"] == 2
    assert fs_tuple["fit_rate_pct"] == 50.0

    db.close()
