"""Unit tests for capacity and freshness calculations."""

from careerradar.search.capacity import calculate_capacity


def test_capacity_empty():
    res = calculate_capacity(0, 0)
    assert res["zone"] == "empty"
    assert res["search_pairs"] == 0
    assert res["total_cells"] == 0


def test_capacity_optimal_zone():
    # 4 queries x 3 locations = 12 search pairs (<= 20)
    res = calculate_capacity(active_queries_count=4, active_locations_count=3)
    assert res["search_pairs"] == 12
    assert res["total_cells"] == 24
    assert res["zone"] == "optimal"
    assert res["cycle_days"] <= 1.0
    assert res["cycle_hours"] <= 24.0


def test_capacity_balanced_zone():
    # 7 queries x 4 locations = 28 search pairs (21-40)
    res = calculate_capacity(active_queries_count=7, active_locations_count=4)
    assert res["search_pairs"] == 28
    assert res["total_cells"] == 56
    assert res["zone"] == "balanced"
    assert res["cycle_days"] <= 2.0


def test_capacity_overloaded_zone():
    # 15 queries x 5 locations = 75 search pairs (> 40)
    res = calculate_capacity(active_queries_count=15, active_locations_count=5)
    assert res["search_pairs"] == 75
    assert res["total_cells"] == 150
    assert res["zone"] == "overloaded"
    assert res["cycle_days"] > 1.5


def test_custom_budget_and_schedule():
    custom_config = {
        "scraper": {
            "budgets": {
                "linkedin": {"searches_per_run": 30},
                "indeed": {"searches_per_run": 50},
            }
        },
        "scheduler": {
            "search": {
                "schedule": ["00:00", "06:00", "12:00", "18:00"],
            }
        },
    }
    # 4 runs * 30 min = 120 daily capacity pairs
    res = calculate_capacity(
        active_queries_count=10, active_locations_count=5, config=custom_config
    )
    assert res["search_pairs"] == 50
    assert res["daily_capacity_pairs"] == 120
    assert res["cycle_days"] == 0.42
