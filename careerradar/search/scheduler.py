"""Scrape-budget scheduler: choose which cells to visit this run.

The matrix of enabled cells is rotated across runs rather than swept in one go.
Everything here is a pure function over CellState, so the rotation policy is
unit-testable without a network.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from careerradar.core.database import Database

if TYPE_CHECKING:
    from careerradar.taxonomy.roles import RoleTaxonomy

EWMA_ALPHA = 0.4

# Default productivity assumption for a cell that has never run, so first-time cells are
# neither starved nor over-favoured.
DEFAULT_NEW_PER_SCRAPE = 2.0

# A cell's quality modifier stays neutral (1.0) until it clears this many verdicts, so one
# or two bad LLM scores can't tank a cell before it's had a fair sample -- the same
# exploration concern novelty/DEAD_PENALTY_FLOOR already guard for the yield signal.
QUALITY_SAMPLE_MIN = 3

# fit_score runs roughly 0-100 (keyword_score.py's observed corpus range is 15-80).
# Centered on 0.5 + fit_score/100 and clamped so quality can only ever nudge productivity,
# never zero it out on its own.
QUALITY_CLAMP = (0.5, 1.5)

# The dead-cell penalty is floored rather than allowed to decay toward zero.
DEAD_PENALTY_FLOOR = 0.25


@dataclass
class CellState:
    id: int
    source: str
    role_family: str
    location_id: str
    query: str
    active: bool = True
    enabled: int = 1
    last_scraped_at: str | None = None
    last_success_at: str | None = None
    last_result_count: int = 0
    last_saturated: int = 0
    last_hours_old: int | None = None
    ewma_new_per_scrape: float | None = None
    ewma_fit_score: float | None = None
    quality_samples: int = 0
    consecutive_empty: int = 0
    consecutive_error: int = 0
    total_scrapes: int = 0
    backoff_until: str | None = None


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
    # Provenance only. role_family is re-derived from each posting's title, because the
    # query is a poor predictor of what the board returns.
    role_family: str | None = None
    active: bool = True
    est_request_units: float = 0.0
    # The cell's persisted EWMA of new-postings-per-scrape, carried through so the
    # observation writer can smooth against it. Without it the writer has no prior and the
    # stored value degenerates to "whatever the last run returned".
    ewma_new_per_scrape: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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
            "role_family": self.role_family,
            "active": self.active,
            "est_request_units": self.est_request_units,
            "ewma_new_per_scrape": self.ewma_new_per_scrape,
            "proxies": self.extra.get("proxies") or [],
        }


def with_location_weights(scraper_config: dict[str, Any], roles: "RoleTaxonomy") -> dict[str, Any]:
    """Copy of `scraper_config` carrying per-location weights."""
    out = dict(scraper_config)
    locs = out.setdefault("locations", {})
    for location in roles.locations.values():
        locs[location.id] = {"weight": location.weight}
    return out


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


def quality_multiplier(cell: CellState) -> float:
    """How much a cell's realized LLM fit_score should nudge its scrape priority."""
    if cell.ewma_fit_score is None or cell.quality_samples < QUALITY_SAMPLE_MIN:
        return 1.0
    modifier = 0.5 + cell.ewma_fit_score / 100.0
    return min(QUALITY_CLAMP[1], max(QUALITY_CLAMP[0], modifier))


def _read_cadence(config: dict[str, Any]) -> int:
    val = config.get("cadence_hours", 24)
    if isinstance(val, dict):
        return int(val.get("core", 24))
    try:
        return int(val)
    except (ValueError, TypeError):
        return 24


def _read_floor(config: dict[str, Any]) -> int:
    val = config.get("hours_old_floor", 72)
    if isinstance(val, dict):
        return int(val.get("core", 72))
    try:
        return int(val)
    except (ValueError, TypeError):
        return 72


def cell_priority(cell: CellState, config: dict[str, Any], now: datetime) -> float:
    """Higher is more urgent."""
    cadence = _read_cadence(config)
    staleness = hours_since(cell.last_scraped_at, now)
    urgency = staleness / max(cadence, 1)

    location_weight = (config.get("locations") or {}).get(cell.location_id, {}).get("weight", 1.0)

    # Log-damped so one hot cell cannot monopolise the rotation.
    yield_estimate = (
        cell.ewma_new_per_scrape if cell.ewma_new_per_scrape is not None else DEFAULT_NEW_PER_SCRAPE
    )
    volume = math.log1p(max(yield_estimate, 0.0)) / math.log1p(20.0)
    productivity = volume * quality_multiplier(cell)

    novelty = 1.5 if cell.total_scrapes == 0 else 1.0
    # A truncated cell was under-sampled, so revisit it sooner.
    saturation_bonus = 1.25 if cell.last_saturated else 1.0
    # Floored rather than decaying toward zero.
    dead_penalty = max(DEAD_PENALTY_FLOOR, 0.6 ** min(cell.consecutive_empty, 5))

    return (
        urgency
        * location_weight
        * (0.35 + 0.65 * productivity)
        * novelty
        * saturation_bonus
        * dead_penalty
    )


def starved_cells(
    cells: list[CellState], config: dict[str, Any], now: datetime, source: str | None = None
) -> list[CellState]:
    """Cells past their hard starvation deadline, scheduled immediately regardless of rank."""
    cadence = _read_cadence(config)
    multiple = int(config.get("starvation_multiple", 3))
    deadline = cadence * multiple
    out = []
    for cell in cells:
        if source and cell.source != source:
            continue
        if not is_eligible(cell, now):
            continue
        if hours_since(cell.last_scraped_at, now) > deadline:
            out.append(cell)
    # Most overdue first.
    return sorted(out, key=lambda c: -hours_since(c.last_scraped_at, now))


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
    roles: "RoleTaxonomy",
    source: str,
    now: datetime,
    backfill: bool = False,
) -> ScrapeTask:
    location = roles.locations.get(cell.location_id)
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
        location_label=location.search_label if location else cell.location_id,
        country=location.country if location else None,
        indeed_country=location.indeed_country if location else "usa",
        is_remote=location.is_remote if location else False,
        distance=location.distance if location else 50,
        results_wanted=results_wanted,
        hours_old=(
            config.get("backfill_hours_old", 336)
            if backfill
            else adaptive_hours_old(cell, config, now)
        ),
        fetch_description=fetch_description,
        desc_selection=desc_selection,
        role_family=cell.role_family,
        active=cell.active,
        est_request_units=estimate_units(source, results_wanted, config, fetch_description),
        extra={"proxies": config.get("proxies_list") or []},
    )


def select_cells(
    cells: list[CellState],
    config: dict[str, Any],
    roles: "RoleTaxonomy",
    source: str,
    now: datetime | None = None,
    backfill: bool = False,
) -> list[ScrapeTask]:
    """Pick this run's cells for one source, within budget."""
    now = now or datetime.now(timezone.utc)
    budget = (config.get("budgets") or {}).get(source, {})
    max_searches = budget.get("searches_per_run", 10)
    max_units = budget.get("request_units", 100)
    max_pages = budget.get("max_pages_per_run")

    eligible = [c for c in cells if is_eligible(c, now)]
    ranked = sorted(eligible, key=lambda c: (-cell_priority(c, config, now), c.id))

    starved = starved_cells(eligible, config, now, source)
    starved_ids = {c.id for c in starved}
    order = starved + [c for c in ranked if c.id not in starved_ids]

    picked = []
    units = 0.0
    pages = 0

    for cell in order:
        if len(picked) >= max_searches:
            break
        task = _make_task(cell, config, roles, source, now, backfill=backfill)
        cell_pages = estimate_pages(source, task.results_wanted, config)
        if units + task.est_request_units > max_units:
            continue
        if max_pages is not None and pages + cell_pages > max_pages:
            continue
        picked.append(task)
        units += task.est_request_units
        pages += cell_pages

    return enforce_staleness_floor(
        picked, ranked, cells, config, roles, source, now, backfill=backfill
    )


def enforce_staleness_floor(
    picked: list[ScrapeTask],
    ranked: list[CellState],
    cells: list[CellState],  # noqa: ARG001 - signature parity with the other staleness passes
    config: dict[str, Any],
    roles: "RoleTaxonomy",
    source: str,
    now: datetime,
    backfill: bool = False,
) -> list[ScrapeTask]:
    """Guarantee cells in high-weight locations are visited within the floor."""
    floor = config.get("max_staleness_hours", 72)
    location_weights = config.get("locations") or {}
    chosen_ids = {t.cell_id for t in picked}

    overdue = [
        c
        for c in ranked
        if location_weights.get(c.location_id, {}).get("weight", 1.0) >= 1.0
        and hours_since(c.last_success_at, now) > floor
        and c.id not in chosen_ids
    ]
    if not overdue:
        return picked

    result = list(picked)
    for cell in overdue:
        if not result:
            break
        result.pop()
        result.append(_make_task(cell, config, roles, source, now, backfill=backfill))

    return result


def overdue_cells(
    cells: list[CellState], config: dict[str, Any], now: datetime | None = None
) -> list[CellState]:
    """Cells past the staleness floor, for the coverage warning and for suppression."""
    now = now or datetime.now(timezone.utc)
    floor = config.get("max_staleness_hours", 72)
    weights = config.get("locations") or {}
    return [
        c
        for c in cells
        if c.enabled
        and weights.get(c.location_id, {}).get("weight", 1.0) >= 1.0
        and hours_since(c.last_success_at, now) > floor
    ]


def update_ewma(previous: float | None, observed: float, alpha: float = EWMA_ALPHA) -> float:
    if previous is None:
        return float(observed)
    return alpha * float(observed) + (1 - alpha) * float(previous)


def is_saturated(returned: int, requested: int, threshold: float = 0.95) -> bool:
    """Saturation is right-censoring: the board truncated us, so the count is a lower bound."""
    if not requested:
        return False
    return returned >= threshold * requested


def scrape_tasks(
    db: Database, config: dict[str, Any], roles: "RoleTaxonomy", source: str
) -> list["ScrapeTask"]:
    """The tasks `run_sync` would pick for one source, right now."""
    cells = db.get_cells(source=source)
    scraper_config = with_location_weights(config.get("scraper", {}), roles)
    return select_cells(cells, scraper_config, roles, source)
