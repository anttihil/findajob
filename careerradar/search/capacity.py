"""Search matrix capacity and freshness calculations."""

from typing import Any

# Recommended thresholds for active search pairs (queries x locations)
OPTIMAL_THRESHOLD_PAIRS = 20  # <= 20 pairs -> swept within <= 24 hours
BALANCED_THRESHOLD_PAIRS = 40  # 21-40 pairs -> swept within 24-48 hours


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
    elif search_pairs <= OPTIMAL_THRESHOLD_PAIRS:
        zone = "optimal"
        message = (
            f"Optimal freshness: Full matrix swept in ~{cycle_hours}h. "
            "All new postings discovered within 24 hours."
        )
    elif search_pairs <= BALANCED_THRESHOLD_PAIRS:
        zone = "balanced"
        message = (
            f"Balanced: Full matrix swept in ~{cycle_hours}h. "
            "Postings discovered well within the 3-day window."
        )
    else:
        zone = "overloaded"
        message = (
            f"Capacity Warning: Full sweep takes ~{cycle_days} days. "
            "Postings may pass the optimal application window. Consider pruning queries."
        )

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
        "optimal_threshold": OPTIMAL_THRESHOLD_PAIRS,
        "balanced_threshold": BALANCED_THRESHOLD_PAIRS,
    }
