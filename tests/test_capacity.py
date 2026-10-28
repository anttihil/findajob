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
    assert res["sources"]["linkedin"]["cycle_hours"] == 12
    assert res["cycle_hours"] == 12
    assert res["zone"] == "optimal"


def test_capacity_balanced_and_overloaded():
    assert calculate_capacity(10, 5)["zone"] == "balanced"
    assert calculate_capacity(20, 5)["zone"] == "overloaded"


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


def test_request_and_page_budgets_limit_capacity():
    config = {
        "scraper": {
            "budgets": {
                "linkedin": {
                    "searches_per_run": 30,
                    "request_units": 21,
                    "max_pages_per_run": 14,
                }
            }
        }
    }
    res = calculate_capacity(1, 5, config)
    assert res["sources"]["linkedin"]["searches_per_run"] == 2
    assert res["cycle_hours"] == 18


def test_zero_budget_reports_unavailable_sweep():
    res = calculate_capacity(1, 1, {"scraper": {"budgets": {"linkedin": {"request_units": 0}}}})
    assert res["cycle_hours"] is None
    assert res["zone"] == "overloaded"


def test_legacy_uneven_daily_schedule_uses_longest_revisit_gap():
    res = calculate_capacity(1, 1, {"scheduler": {"search": {"schedule": ["00:00", "01:00"]}}})
    assert res["cycle_hours"] == 23
    assert res["sources"]["indeed"]["runs_per_day"] == 2


def test_proxy_budget_is_used(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRAPER_PROXIES", "example:1234")
    res = calculate_capacity(1, 40)
    # 30 cells fit the proxy request/page limits at default result sizes.
    assert res["sources"]["linkedin"]["searches_per_run"] == 30
    assert res["cycle_hours"] == 12
