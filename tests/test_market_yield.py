from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from findajob.core.database import Database
from findajob.core.migrations import migrate
from findajob.market.analytics import MarketAnalytics
from findajob.search.targets import SearchTargets
from findajob.web.app import app


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
    assert "total_priority_postings" in summary
    assert "overall_priority_rate_pct" in summary
    assert summary["total_postings"] >= summary["total_scored"]
    assert summary["total_scored"] >= summary["total_priority_postings"]

    for q in data["top_queries"][:5]:
        assert "query" in q
        assert "total_postings" in q
        assert "scored_postings" in q
        assert "priority_postings" in q
        assert "priority_rate_pct" in q
        assert "sources" in q
        assert "locations" in q
        assert "yield_category" in q

    for t in data["tuples"][:5]:
        assert "source" in t
        assert "query" in t
        assert "location_id" in t
        assert "total_postings" in t
        assert "priority_postings" in t


def test_market_yield_api_filters(client: TestClient) -> None:
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

    res_indeed = client.get("/api/market/yield?source=indeed")
    assert res_indeed.status_code == 200
    data_indeed = res_indeed.json()
    for t in data_indeed["tuples"][:10]:
        assert t["source"] == "indeed"

    res_loc = client.get("/api/market/yield?location=us_remote")
    assert res_loc.status_code == 200
    data_loc = res_loc.json()
    for t in data_loc["tuples"][:10]:
        assert t["location_id"] == "us_remote"

    res_q = client.get("/api/market/yield?query=Full%20Stack%20Engineer")
    assert res_q.status_code == 200
    data_q = res_q.json()
    for t in data_q["tuples"][:10]:
        assert t["query"] == "Full Stack Engineer"

    res_bad = client.get("/api/market/yield?location=atlantis")
    assert res_bad.status_code == 400


def test_market_analytics_query_yield_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_market_yield.db"
    db = Database(str(db_path))
    migrate(db.conn)

    cur = db.conn.cursor()
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

    cur.execute(
        """
        INSERT INTO job_verdicts
            (id, job_id, profile_version, model, fit, priority, reason_type, created_at)
        VALUES (1, 101, 1, 'model', 1, 'low', 'match', '2026-08-25T01:10:00Z')
        """
    )
    cur.execute(
        """
        INSERT INTO job_verdicts
            (id, job_id, profile_version, model, fit, priority, reason_type, created_at)
        VALUES (2, 102, 1, 'model', 0, 'first', 'skills', '2026-08-25T02:10:00Z')
        """
    )
    cur.execute(
        """
        INSERT INTO job_verdicts
            (id, job_id, profile_version, model, fit, priority, reason_type, created_at)
        VALUES (3, 103, 1, 'model', 0, 'consider', 'domain', '2026-08-25T03:10:00Z')
        """
    )
    db.conn.commit()

    targets = SearchTargets([], {})
    analytics = MarketAnalytics(db, config={}, targets=targets)
    # Keep the query window aligned with the fixed fixture dates, regardless of today.
    end = datetime(2026, 8, 26, tzinfo=timezone.utc)
    monkeypatch.setattr(analytics, "_window", lambda days: (end - timedelta(days=days), end))

    result = analytics.query_yield(window_days=30)
    summary = result["summary"]
    assert summary["total_postings"] == 3
    assert summary["total_scored"] == 3
    assert summary["total_priority_postings"] == 2
    assert summary["overall_priority_rate_pct"] == 66.7

    assert len(result["top_queries"]) >= 2
    top_q = next(q for q in result["top_queries"] if q["query"] == "Full Stack Engineer")
    assert top_q["query"] == "Full Stack Engineer"
    assert top_q["priority_postings"] == 1
    assert top_q["scored_postings"] == 2
    assert top_q["priority_rate_pct"] == 50.0

    tuples = result["tuples"]
    fs_tuple = next(t for t in tuples if t["query"] == "Full Stack Engineer")
    assert fs_tuple["source"] == "linkedin"
    assert fs_tuple["location_id"] == "us_remote"
    assert fs_tuple["priority_postings"] == 1
    assert fs_tuple["total_postings"] == 2
    assert fs_tuple["priority_rate_pct"] == 50.0

    db.close()


@pytest.fixture
def progression_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MarketAnalytics]:
    db = Database(str(tmp_path / "progression.db"))
    migrate(db.conn)
    for cell_id, source, location in [
        (1, "indeed", "remote"),
        (2, "linkedin", "remote"),
        (3, "indeed", "helsinki"),
        (4, "indeed", "empty"),
    ]:
        db.conn.execute(
            """INSERT INTO scrape_cells
               (id, source, location_id, query, search_label, country, enabled, created_at)
               VALUES (?, ?, ?, 'Engineer', 'Remote', 'US', 1, '2026-08-01')""",
            (cell_id, source, location),
        )
    jobs = [
        (1, 1, "2026-08-23T23:59:59Z"),  # Outside the three-day window.
        (2, 1, "2026-08-24T00:00:00Z"),  # Included calendar-day boundary.
        (3, 1, "2026-08-24T11:00:00Z"),
        (4, 1, "2026-08-26T01:00:00Z"),  # Unscored.
        (5, 2, "2026-08-24T01:00:00Z"),  # Different provider.
        (6, 3, "2026-08-24T01:00:00Z"),  # Different location.
        (7, 1, "2026-08-24T01:00:00+02:00"),  # UTC date is Aug 23.
    ]
    for job_id, cell_id, date in jobs:
        db.conn.execute(
            """INSERT INTO jobs (id, job_key, title, scrape_cell_id, date_found)
               VALUES (?, ?, 'Engineer', ?, ?)""",
            (job_id, str(job_id), cell_id, date),
        )
    for job_id, profile, fit, priority in [
        (1, 1, 1, "consider"),
        (2, 1, 1, "low"),
        (2, 2, 0, "first"),
        (3, 1, 0, "first"),
        (3, 2, 1, "low"),
        (5, 1, 1, None),
        (6, 1, 1, "consider"),
    ]:
        db.conn.execute(
            """INSERT INTO job_verdicts (job_id, profile_version, model, fit, priority, created_at)
               VALUES (?, ?, 'test', ?, ?, '2026-08-26T02:00:00Z')""",
            (job_id, profile, fit, priority),
        )
    db.conn.commit()
    analytics = MarketAnalytics(db, {}, SearchTargets([], {}))
    end = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(analytics, "_window", lambda days: (end - timedelta(days=days), end))
    yield analytics
    db.close()


def test_tuple_progression_daily_cohorts(progression_analytics: MarketAnalytics) -> None:
    result = progression_analytics.tuple_progression(1, 3)
    assert result is not None
    assert result["tuple"] == {
        "id": 1,
        "query": "Engineer",
        "source": "indeed",
        "location_id": "remote",
    }
    assert result["points"] == [
        {
            "date": "2026-08-24",
            "total_postings": 2,
            "scored_postings": 2,
            "priority_postings": 1,
            "first_priority": 1,
            "consider_priority": 0,
            "low_priority": 1,
        },
        {
            "date": "2026-08-25",
            "total_postings": 0,
            "scored_postings": 0,
            "priority_postings": 0,
            "first_priority": 0,
            "consider_priority": 0,
            "low_priority": 0,
        },
        {
            "date": "2026-08-26",
            "total_postings": 1,
            "scored_postings": 0,
            "priority_postings": 0,
            "first_priority": 0,
            "consider_priority": 0,
            "low_priority": 0,
        },
    ]
    assert result["totals"] == {
        "total_postings": 3,
        "scored_postings": 2,
        "priority_postings": 1,
        "first_priority": 1,
        "consider_priority": 0,
        "low_priority": 1,
    }
    overview = progression_analytics.query_yield(
        3, source="indeed", location_id="remote", query="Engineer"
    )
    assert len(overview["tuples"]) == 1
    for key, value in result["totals"].items():
        assert overview["tuples"][0][key] == value


def test_tuple_progression_all_time_and_empty(progression_analytics: MarketAnalytics) -> None:
    result = progression_analytics.tuple_progression(1)
    assert result is not None
    assert result["start_date"] == "2026-08-23"
    assert result["end_date"] == "2026-08-26"
    assert result["totals"] == {
        "total_postings": 5,
        "scored_postings": 3,
        "priority_postings": 2,
        "first_priority": 1,
        "consider_priority": 1,
        "low_priority": 1,
    }
    for window in (None, 3):
        empty = progression_analytics.tuple_progression(4, window)
        assert empty is not None
        assert len(empty["points"]) == (window or 1)
        assert empty["totals"] == {
            "total_postings": 0,
            "scored_postings": 0,
            "priority_postings": 0,
            "first_priority": 0,
            "consider_priority": 0,
            "low_priority": 0,
        }
    assert progression_analytics.tuple_progression(999) is None


def test_tuple_progression_api(
    progression_analytics: MarketAnalytics, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    db_path = str(progression_analytics.db.db_path)
    monkeypatch.setattr("findajob.web.app.get_db", lambda: Database(db_path))
    monkeypatch.setattr(
        "findajob.web.app.analytics_context", lambda _db: ({}, SearchTargets([], {}), None)
    )
    response = client.get("/api/market/tuples/1/progression")
    assert response.status_code == 200
    assert response.json()["totals"]["total_postings"] == 5
    assert client.get("/api/market/tuples/999/progression").status_code == 404
    for window in (0, -1, 366):
        assert (
            client.get(f"/api/market/tuples/1/progression?window_days={window}").status_code == 422
        )


def test_priority_aggregates_exclude_binary_and_other_profiles(
    progression_analytics: MarketAnalytics,
) -> None:
    db = progression_analytics.db
    # A priority under another profile must not turn an unscored feed job into a scored one.
    db.conn.execute(
        """INSERT INTO job_verdicts (job_id, profile_version, model, priority, created_at)
           VALUES (7, 2, 'test', 'first', '2026-08-26')"""
    )
    result = progression_analytics.query_yield()
    summary = result["summary"]
    assert summary["total_postings"] == 7
    assert summary["total_scored"] == 4
    assert summary["total_first_priority"] == 1
    assert summary["total_consider_priority"] == 2
    assert summary["total_low_priority"] == 1
    assert summary["overall_priority_rate_pct"] == 75
    binary_only = next(t for t in result["tuples"] if t["cell_id"] == 2)
    assert binary_only["total_postings"] == 1
    assert binary_only["scored_postings"] == 0
    assert binary_only["yield_category"] == "unscored"
    for group in ("tuples", "top_queries", "by_source", "by_location"):
        for row in result[group]:
            assert row["priority_postings"] == row["first_priority"] + row["consider_priority"]
            assert row["scored_postings"] == row["priority_postings"] + row["low_priority"]
            assert "strong_fits" not in row
            assert "fit_rate" not in row
        for field in ("first_priority", "consider_priority", "low_priority"):
            assert sum(row[field] for row in result[group]) == summary[f"total_{field}"]
