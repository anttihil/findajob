"""Search matrix capacity and freshness calculations."""

from typing import Any

# Sweep health is determined by the estimated time to revisit every search pair.
# Pair counts alone are not meaningful here: a matrix that is large for one
# schedule may still be swept quickly with a higher per-run budget.
OPTIMAL_THRESHOLD_HOURS = 24
BALANCED_THRESHOLD_HOURS = 48


def calculate_capacity(
    active_queries_count: int,
    active_locations_count: int,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate search matrix size, daily throughput, cycle time, and freshness health.

    Formula:
      Active Search Pairs = Active Target Queries x Active Target Locations
      Total Scrape Cells = Active Search Pairs x 2 (Indeed + LinkedIn)
      Daily Capacity Pairs = Bottleneck Daily Searches across Enabled Sources
      Cycle Days = Active Search Pairs / Daily Capacity Pairs
    """
    config = config or {}
    scraper_cfg = config.get("scraper", {})
    budgets = scraper_cfg.get("budgets", {})
    scheduler_cfg = config.get("scheduler", {})
    search_sched = scheduler_cfg.get("search", {})
    default_schedule = ["01:00", "07:00", "13:00", "19:00"]
    search_schedule = search_sched.get("schedule", default_schedule)
    runs_per_day = len(search_schedule) or 4

    # Default per-run search limits per source
    linkedin_per_run = budgets.get("linkedin", {}).get("searches_per_run", 10)
    indeed_per_run = budgets.get("indeed", {}).get("searches_per_run", 40)

    # Lowest throughput source is the bottleneck for full matrix cycles
    daily_linkedin = linkedin_per_run * runs_per_day
    daily_indeed = indeed_per_run * runs_per_day
    bottleneck_daily_pairs = min(daily_linkedin, daily_indeed)

    search_pairs = active_queries_count * active_locations_count
    total_cells = search_pairs * 2

    cycle_days = round(search_pairs / max(bottleneck_daily_pairs, 1), 2)
    cycle_hours = round(cycle_days * 24, 1)

    if search_pairs == 0:
        zone = "empty"
        message = "No active search targets configured."
    elif cycle_hours <= OPTIMAL_THRESHOLD_HOURS:
        zone = "optimal"
        message = "Every active search pair is checked within a day."
    elif cycle_hours <= BALANCED_THRESHOLD_HOURS:
        zone = "balanced"
        message = "Every active search pair is checked within two days."
    else:
        zone = "overloaded"
        message = "Consider pausing low-yield queries or locations."

    return {
        "active_queries": active_queries_count,
        "active_locations": active_locations_count,
        "search_pairs": search_pairs,
        "total_cells": total_cells,
        "runs_per_day": runs_per_day,
        "daily_capacity_pairs": bottleneck_daily_pairs,
        "cycle_days": cycle_days,
        "cycle_hours": cycle_hours,
        "zone": zone,
        "message": message,
        "optimal_threshold_hours": OPTIMAL_THRESHOLD_HOURS,
        "balanced_threshold_hours": BALANCED_THRESHOLD_HOURS,
    }
