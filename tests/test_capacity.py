"""Matrix estimates must follow source scopes, schedules, and effective budgets."""

import pytest

from findajob.search.capacity import calculate_capacity, capacity_for_targets
from findajob.search.targets import SearchTargets


@pytest.fixture(autouse=True)
def no_proxies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRAPER_PROXIES", raising=False)


def test_capacity_empty():
    assert calculate_capacity(0, 0)["zone"] == "empty"


def test_capacity_uses_whole_source_runs():
    res = calculate_capacity(4, 3)
    assert res["total_cells"] == 24
    assert res["sources"]["indeed"]["cycle_hours"] == 3
    assert res["sources"]["linkedin"]["cycle_hours"] == 72
    assert res["cycle_hours"] == 72
    assert res["zone"] == "overloaded"


def test_capacity_balanced_and_overloaded():
    assert calculate_capacity(2, 3)["zone"] == "balanced"
    assert calculate_capacity(6, 3)["zone"] == "overloaded"


def test_source_scopes_and_independent_schedules():
    targets = SearchTargets.from_spec(
        {
            "queries": ["Indeed role", "LinkedIn role"],
            "locations": [{"id": "remote", "label": "Remote", "enabled": True}],
        }
    )
    targets.query_sources = {"Indeed role": ("indeed",), "LinkedIn role": ("linkedin",)}
    config = {
        "scheduler": {
            "search": {
                "sources": {
                    "indeed": {"interval_minutes": 120},
                    "linkedin": {"interval_minutes": 480},
                }
            }
        }
    }
    res = capacity_for_targets(targets, config)
    assert res["total_cells"] == 2
    assert res["sources"]["indeed"]["cells"] == 1
    assert res["sources"]["linkedin"]["cells"] == 1
    assert res["sources"]["indeed"]["runs_per_day"] == 12
    assert res["cycle_hours"] == 8


def test_disabled_and_empty_sources_do_not_limit_sweep():
    config = {"scraper": {"sources": {"linkedin": False}}}
    assert calculate_capacity(1, 1, config)["cycle_hours"] == 3
    res = calculate_capacity(1, 1, cells_by_source={"indeed": 1})
    assert res["cycle_hours"] == 3


def test_request_forecast_uses_cell_boundaries_and_ignores_legacy_page_cap():
    config = {
        "scraper": {
            "policies": {
                "linkedin": {
                    "results_wanted": 60,
                    "searches_per_run": 30,
                    "requests_per_proxy": 21,
                    "max_pages_per_run": 14,
                }
            }
        }
    }
    res = calculate_capacity(1, 5, config)
    assert res["sources"]["linkedin"]["estimated_cells_per_run"] == 1
    assert res["cycle_hours"] == 30


def test_zero_budget_reports_unavailable_sweep():
    res = calculate_capacity(
        1, 1, {"scraper": {"policies": {"linkedin": {"requests_per_proxy": 0}}}}
    )
    assert res["cycle_hours"] is None
    assert res["zone"] == "overloaded"


def test_legacy_uneven_daily_schedule_uses_longest_revisit_gap():
    res = calculate_capacity(1, 1, {"scheduler": {"search": {"schedule": ["00:00", "01:00"]}}})
    assert res["cycle_hours"] == 23
    assert res["sources"]["indeed"]["runs_per_day"] == 2


def test_proxy_budget_is_used(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRAPER_PROXIES", "example:1234")
    res = calculate_capacity(1, 40)
    # One configured endpoint has 100 requests, regardless of proxy presence.
    assert res["sources"]["linkedin"]["estimated_cells_per_run"] == 1
    assert res["cycle_hours"] == 240
