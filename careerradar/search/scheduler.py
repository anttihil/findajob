"""Scrape-budget scheduler: choose which cells to visit this run.

Cells are visited oldest-attempt-first, and the run takes as many as the budget allows.
That is the whole policy. It used to be a six-term priority product with two override
passes on top, but the budget is not scarce: Indeed runs 44 enabled cells against a
40-search budget, so the ranking only ever decided which four to skip, and a skipped cell
sorts to the front of the next run regardless.

Everything here is a pure function over CellState, so the rotation policy is
unit-testable without a network.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypedDict

from careerradar.core.database import Database


@dataclass
class CellState:
    id: int
    source: str
    location_id: str
    query: str
    search_label: str = ""
    country: str = ""
    indeed_country: str = "usa"
    is_remote: bool = False
    distance: int = 50
    # Decides which locations the staleness floor guarantees, nothing else.
    weight: float = 1.0
    active: bool = True
    enabled: int = 1
    last_scraped_at: str | None = None
    last_success_at: str | None = None
    last_saturated: int = 0
    backoff_until: str | None = None


class ScrapeTaskPayload(TypedDict):
    """One cell visit, as it crosses the BaseJobSource boundary.

    Every key is always present -- `ScrapeTask.to_dict` writes all of them -- so a source
    may ignore a field but must never expect it to be absent.
    """

    cell_id: int
    source: str
    query: str
    location_id: str
    location_label: str
    country: str | None
    indeed_country: str
    is_remote: bool
    distance: int
    results_wanted: int
    hours_old: int
    fetch_description: bool
    desc_selection: str
    active: bool
    est_request_units: float
    proxies: list[str]


@dataclass
class ScrapeTask:
    cell_id: int
    source: str
    query: str
    location_id: str
    location_label: str
    country: str | None
    indeed_country: str
    is_remote: bool
    distance: int
    results_wanted: int
    hours_old: int
    fetch_description: bool
    desc_selection: str
    active: bool = True
    est_request_units: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> ScrapeTaskPayload:
        return {
            "cell_id": self.cell_id,
            "source": self.source,
            "query": self.query,
            "location_id": self.location_id,
            "location_label": self.location_label,
            "country": self.country,
            "indeed_country": self.indeed_country,
            "is_remote": self.is_remote,
            "distance": self.distance,
            "results_wanted": self.results_wanted,
            "hours_old": self.hours_old,
            "fetch_description": self.fetch_description,
            "desc_selection": self.desc_selection,
            "active": self.active,
            "est_request_units": self.est_request_units,
            "proxies": self.extra.get("proxies") or [],
        }


def _parse(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hours_since(value: datetime | str | None, now: datetime, default: float = 1e4) -> float:
    parsed = _parse(value)
    if parsed is None:
        return default
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


def is_eligible(cell: CellState, now: datetime) -> bool:
    """Whether a cell can be considered for scheduling in this run."""
    if not cell.enabled:
        return False
    backoff = _parse(cell.backoff_until)
    return backoff is None or backoff <= now


def _read_floor(config: dict[str, Any]) -> int:
    val = config.get("hours_old_floor", 72)
    if isinstance(val, dict):
        return int(val.get("core", 72))
    try:
        return int(val)
    except (ValueError, TypeError):
        return 72


def adaptive_hours_old(cell: CellState, config: dict[str, Any], now: datetime) -> int:
    """How far back one scrape of this cell looks, in hours."""
    floor = _read_floor(config)
    gap = hours_since(cell.last_success_at, now, default=config.get("backfill_hours_old", 168))
    ceiling = int(config.get("max_hours_old", 168))
    return int(min(ceiling, max(floor, 1.5 * gap)))


def results_wanted_for(cell: CellState, config: dict[str, Any], source: str) -> int:
    budget = (config.get("budgets") or {}).get(source, {})
    default = budget.get("results_wanted_default", 50)
    maximum = budget.get("max_results_wanted", 200)
    wanted = default
    if cell.last_saturated:
        wanted = int(wanted * 1.5)
    return int(min(maximum, max(25, wanted)))


def estimate_pages(source: str, results_wanted: int, config: dict[str, Any]) -> int:
    """Search-page requests one cell will spend."""
    page_size = max((config.get("page_size") or {}).get(source, 25), 1)
    return math.ceil(max(results_wanted, 1) / page_size) + 1


def estimate_units(
    source: str,
    results_wanted: int,
    config: dict[str, Any],
    fetch_descriptions: bool = False,
) -> float:
    """Total HTTP requests one cell will spend -- the thing `request_units` budgets."""
    pages = estimate_pages(source, results_wanted, config)
    per_description = (config.get("requests_per_description") or {}).get(source, 0)
    descriptions = per_description * max(results_wanted, 1) if fetch_descriptions else 0
    return float(pages + descriptions)


def _make_task(
    cell: CellState,
    config: dict[str, Any],
    source: str,
    now: datetime,
    backfill: bool = False,
) -> ScrapeTask:
    budget = (config.get("budgets") or {}).get(source, {})
    fetch_setting = budget.get("fetch_descriptions", False)
    if backfill:
        results_wanted = budget.get("max_results_wanted", 200)
    else:
        results_wanted = results_wanted_for(cell, config, source)

    if fetch_setting is True:
        fetch_description, desc_selection = True, budget.get("desc_selection", "census")
    else:
        fetch_description, desc_selection = False, "none"

    return ScrapeTask(
        cell_id=cell.id,
        source=source,
        query=cell.query,
        location_id=cell.location_id,
        location_label=cell.search_label or cell.location_id,
        country=cell.country or None,
        indeed_country=cell.indeed_country,
        is_remote=cell.is_remote,
        distance=cell.distance,
        results_wanted=results_wanted,
        hours_old=(
            config.get("backfill_hours_old", 336)
            if backfill
            else adaptive_hours_old(cell, config, now)
        ),
        fetch_description=fetch_description,
        desc_selection=desc_selection,
        active=cell.active,
        est_request_units=estimate_units(source, results_wanted, config, fetch_description),
        extra={"proxies": config.get("proxies_list") or []},
    )


def select_cells(
    cells: list[CellState],
    config: dict[str, Any],
    source: str,
    now: datetime | None = None,
    backfill: bool = False,
) -> list[ScrapeTask]:
    """Pick this run's cells for one source, oldest attempt first, within budget."""
    now = now or datetime.now(timezone.utc)
    budget = (config.get("budgets") or {}).get(source, {})
    max_searches = budget.get("searches_per_run", 10)
    max_units = budget.get("request_units", 100)
    max_pages = budget.get("max_pages_per_run")

    eligible = [c for c in cells if is_eligible(c, now)]
    order = sorted(eligible, key=lambda c: (-hours_since(c.last_scraped_at, now), c.id))

    picked: list[ScrapeTask] = []
    units = 0.0
    pages = 0

    for cell in order:
        if len(picked) >= max_searches:
            break
        task = _make_task(cell, config, source, now, backfill=backfill)
        cell_pages = estimate_pages(source, task.results_wanted, config)
        if units + task.est_request_units > max_units:
            continue
        if max_pages is not None and pages + cell_pages > max_pages:
            continue
        picked.append(task)
        units += task.est_request_units
        pages += cell_pages

    return picked


def overdue_cells(
    cells: list[CellState], config: dict[str, Any], now: datetime | None = None
) -> list[CellState]:
    """Cells past the staleness floor, for the coverage warning and for suppression."""
    now = now or datetime.now(timezone.utc)
    floor = config.get("max_staleness_hours", 72)
    return [
        c
        for c in cells
        if c.enabled and c.weight >= 1.0 and hours_since(c.last_success_at, now) > floor
    ]


def is_saturated(returned: int, requested: int, threshold: float = 0.95) -> bool:
    """Saturation is right-censoring: the board truncated us, so the count is a lower bound."""
    if not requested:
        return False
    return returned >= threshold * requested


def scrape_tasks(db: Database, config: dict[str, Any], source: str) -> list["ScrapeTask"]:
    """The tasks `run_sync` would pick for one source, right now."""
    return select_cells(db.get_cells(source=source), config.get("scraper", {}), source)
