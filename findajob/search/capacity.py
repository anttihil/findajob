"""Search matrix capacity and freshness calculations."""

import math
from collections import Counter
from typing import TYPE_CHECKING, Any

from findajob.core.config import DEFAULT_CONFIG, deep_merge
from findajob.core.search_schedule import SearchSchedule, search_schedules
from findajob.search.policy import source_policy
from findajob.search.proxies import load_proxies
from findajob.search.scheduler import estimate_units, results_wanted_for

if TYPE_CHECKING:
    from findajob.search.targets import SearchTargets

OPTIMAL_THRESHOLD_HOURS = 24
BALANCED_THRESHOLD_HOURS = 48


def _sweep_hours(schedule: SearchSchedule, visits: int) -> float:
    if not visits:
        return 0
    if schedule.interval_minutes:
        return visits * schedule.interval_minutes / 60
    minutes = [int(t[:2]) * 60 + int(t[3:]) for t in schedule.times]
    gaps = [b - a for a, b in zip(minutes, [*minutes[1:], minutes[0] + 1440], strict=True)]
    days, remainder = divmod(visits, len(gaps))
    return (days * 1440 + max(sum((gaps + gaps)[i : i + remainder]) for i in range(len(gaps)))) / 60


def capacity_for_targets(targets: "SearchTargets", config: dict[str, Any]) -> dict[str, Any]:
    """Use the same source-scoped matrix for API responses and CLI seeding."""
    schedules = search_schedules(config)
    counts = dict(Counter(spec["source"] for spec in targets.cell_specs(sources=tuple(schedules))))
    return calculate_capacity(
        len(targets.queries),
        sum(loc.enabled for loc in targets.locations.values()),
        config,
        cells_by_source=counts,
    )


def calculate_capacity(
    active_queries_count: int,
    active_locations_count: int,
    config: dict[str, Any] | None = None,
    scheduled_cells: int | None = None,
    cells_by_source: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Forecast full rotation time assuming a cold cache and full result sets.

    This is a nominal schedule estimate: jitter, source backoff, network duration,
    and time queued behind other writing stages can lengthen the observed sweep.
    """
    config = deep_merge(DEFAULT_CONFIG, config or {})
    scraper = config["scraper"]
    endpoints = max(1, len(load_proxies(config)))
    schedules = search_schedules(config)
    search_pairs = active_queries_count * active_locations_count
    sources = {}
    for source, schedule in schedules.items():
        cells = cells_by_source.get(source, 0) if cells_by_source is not None else search_pairs
        policy = source_policy(scraper, source)
        wanted = results_wanted_for(scraper, source)
        units = estimate_units(
            source,
            wanted,
            scraper,
            True,
        )
        allowance = policy["requests_per_proxy"] * endpoints
        # A page-limited cell still occupies one visit; this forecasts full 500-row
        # coverage work, not a guarantee that one visit exhausts a query.
        per_run = max(0, min(cells, math.ceil(allowance / units))) if allowance else 0
        cycle_hours = _sweep_hours(schedule, math.ceil(cells / per_run)) if per_run else None
        sources[source] = {
            "cells": cells,
            "estimated_cells_per_run": per_run,
            "runs_per_day": schedule.runs_per_day,
            "daily_capacity": per_run * schedule.runs_per_day,
            "cycle_hours": round(cycle_hours, 1) if cycle_hours is not None else None,
        }
    active = [row for row in sources.values() if row["cells"]]
    blocked = any(row["cycle_hours"] is None for row in active)
    cycle_hours = None if blocked else max((row["cycle_hours"] for row in active), default=0)
    total_cells = sum(row["cells"] for row in sources.values())
    if scheduled_cells is not None and cells_by_source is None:
        total_cells = scheduled_cells
    if not total_cells:
        zone, message = "empty", "No active search targets configured."
    elif cycle_hours is None:
        zone, message = "overloaded", "A source budget cannot fit one search cell."
    elif cycle_hours <= OPTIMAL_THRESHOLD_HOURS:
        zone, message = "optimal", "Every enabled source's matrix is swept within a day."
    elif cycle_hours <= BALANCED_THRESHOLD_HOURS:
        zone, message = "balanced", "Every enabled source's matrix is swept within two days."
    else:
        zone, message = "overloaded", "Consider pausing low-yield queries or locations."
    return {
        "active_queries": active_queries_count,
        "active_locations": active_locations_count,
        "search_pairs": search_pairs,
        "total_cells": total_cells,
        "runs_per_day": min((row["runs_per_day"] for row in active), default=0),
        "daily_capacity_pairs": min((row["daily_capacity"] for row in active), default=0),
        "cycle_days": round(cycle_hours / 24, 2) if cycle_hours is not None else None,
        "cycle_hours": cycle_hours,
        "sources": sources,
        "zone": zone,
        "message": message,
        "optimal_threshold_hours": OPTIMAL_THRESHOLD_HOURS,
        "balanced_threshold_hours": BALANCED_THRESHOLD_HOURS,
    }
