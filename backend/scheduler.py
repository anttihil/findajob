"""Scrape-budget scheduler: choose which cells to visit this run.

The matrix is ~252 cells (126 per source) and LinkedIn rate-limits around the 10th page on
a single IP, so the matrix is *rotated* across runs rather than swept. Everything here is a
pure function over CellState, so the rotation policy is unit-testable without a network.

Two properties the tests pin down, because both fail silently in production:

  Eventual coverage -- `urgency` grows without bound (capped at 3) and the dead-cell penalty
  decays but never reaches zero, so every enabled cell is eventually visited. That matters
  because "Kubernetes roles vanished from Helsinki" and "my Helsinki query broke" are
  indistinguishable unless you keep probing.

  hours_old >= 1.5x the actual revisit gap -- derived per cell from last_success_at rather
  than read from config, so a missed run or an outage self-heals on the next visit. Violate
  it and postings existed inside the gap and were never observed, deflating that cell's flow
  estimate with no detectable symptom afterwards.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

EWMA_ALPHA = 0.4

# Default productivity assumption for a cell that has never run, so first-time cells are
# neither starved nor over-favoured.
DEFAULT_NEW_PER_SCRAPE = 2.0

TIER_WEIGHT = {"core": 1.0, "adjacent": 0.6, "breadth": 0.3}

# The dead-cell penalty is floored rather than allowed to decay toward zero. Compounding an
# unfloored penalty with tier_weight pushed a persistently-empty breadth cell's effective
# revisit interval past a year in simulation.
DEAD_PENALTY_FLOOR = 0.25


@dataclass
class CellState:
    id: int
    source: str
    role_family: str
    location_id: str
    query: str
    tier: str = "breadth"
    enabled: int = 1
    last_scraped_at: str = None
    last_success_at: str = None
    last_result_count: int = 0
    last_saturated: int = 0
    last_hours_old: int = None
    ewma_new_per_scrape: float = None
    consecutive_empty: int = 0
    consecutive_error: int = 0
    total_scrapes: int = 0
    backoff_until: str = None


@dataclass
class ScrapeTask:
    cell_id: int
    source: str
    query: str
    location_id: str
    location_label: str
    country: str
    indeed_country: str
    is_remote: bool
    distance: int
    results_wanted: int
    hours_old: int
    fetch_description: bool
    desc_selection: str
    # Provenance only. role_family is re-derived from each posting's title, because the
    # query is a poor predictor of what the board returns.
    role_family: str = None
    tier: str = None
    est_request_units: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "cell_id": self.cell_id, "source": self.source, "query": self.query,
            "location_id": self.location_id, "location_label": self.location_label,
            "country": self.country, "indeed_country": self.indeed_country,
            "is_remote": self.is_remote, "distance": self.distance,
            "results_wanted": self.results_wanted, "hours_old": self.hours_old,
            "fetch_description": self.fetch_description,
            "desc_selection": self.desc_selection,
            "role_family": self.role_family, "tier": self.tier,
            "est_request_units": self.est_request_units,
        }


def _parse(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hours_since(value, now, default=1e4):
    parsed = _parse(value)
    if parsed is None:
        return default
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


def is_eligible(cell, now):
    if not cell.enabled:
        return False
    backoff = _parse(cell.backoff_until)
    return backoff is None or backoff <= now


def cell_priority(cell, config, now):
    """Higher is more urgent.

    `urgency` is deliberately UNBOUNDED. An earlier version capped it, which looked like
    anti-starvation but caused starvation: once every contending cell sits at the cap,
    tier_weight becomes an absolute ordering and low-tier cells are never selected again.
    A simulation over 200 runs left breadth cells at zero visits. Unbounded urgency means a
    neglected cell keeps climbing until it outranks a fresher, higher-tier one.

    Even so, priority alone is not relied upon for coverage -- `starved_cells` provides an
    explicit per-tier deadline, because an emergent guarantee that depends on the product of
    six factors is not one worth betting the statistics on.
    """
    cadence = (config.get("cadence_hours") or {}).get(cell.tier, 168)
    staleness = hours_since(cell.last_scraped_at, now)
    urgency = staleness / max(cadence, 1)

    tier_weight = TIER_WEIGHT.get(cell.tier, 0.3)
    location_weight = (
        (config.get("locations") or {}).get(cell.location_id, {}).get("weight", 1.0)
    )

    # Log-damped so one hot cell cannot monopolise the rotation.
    yield_estimate = (
        cell.ewma_new_per_scrape
        if cell.ewma_new_per_scrape is not None
        else DEFAULT_NEW_PER_SCRAPE
    )
    productivity = math.log1p(max(yield_estimate, 0.0)) / math.log1p(20.0)

    novelty = 1.5 if cell.total_scrapes == 0 else 1.0
    # A truncated cell was under-sampled, so revisit it sooner.
    saturation_bonus = 1.25 if cell.last_saturated else 1.0
    # Floored rather than decaying toward zero: a quiet family must still be probed, or
    # "the market moved" and "my query broke" stay indistinguishable.
    dead_penalty = max(DEAD_PENALTY_FLOOR, 0.6 ** min(cell.consecutive_empty, 5))

    return (
        urgency
        * tier_weight
        * location_weight
        * (0.35 + 0.65 * productivity)
        * novelty
        * saturation_bonus
        * dead_penalty
    )


def starved_cells(cells, config, now, source=None):
    """Cells past their tier's hard deadline, which must be scheduled regardless of rank.

    This is an explicit guarantee rather than an emergent one. Deadline is
    cadence_hours[tier] * starvation_multiple, so with the default multiple of 4:
    core within 4 days, adjacent within 12, breadth within 28.
    """
    multiple = config.get("starvation_multiple", 4)
    cadences = config.get("cadence_hours") or {}
    out = []
    for cell in cells:
        if source and cell.source != source:
            continue
        if not is_eligible(cell, now):
            continue
        deadline = cadences.get(cell.tier, 168) * multiple
        if hours_since(cell.last_scraped_at, now) > deadline:
            out.append(cell)
    # Most overdue relative to its own deadline first.
    return sorted(
        out,
        key=lambda c: -(
            hours_since(c.last_scraped_at, now)
            / max(cadences.get(c.tier, 168) * multiple, 1)
        ),
    )


def adaptive_hours_old(cell, config, now):
    """Derive hours_old from the ACTUAL revisit gap, not from a static config value.

    Overlap is desirable: dedup absorbs it, and overlap is what keeps the exposure-interval
    union continuous rather than gappy.
    """
    floors = config.get("hours_old_floor") or {}
    floor = floors.get(cell.tier, 168)
    gap = hours_since(
        cell.last_success_at, now, default=config.get("backfill_hours_old", 336)
    )
    ceiling = config.get("max_hours_old", 336)
    return int(min(ceiling, max(floor, 1.5 * gap)))


def results_wanted_for(cell, config, source):
    budget = (config.get("budgets") or {}).get(source, {})
    default = budget.get("results_wanted_default", 50)
    maximum = budget.get("max_results_wanted", 200)
    wanted = default if cell.tier == "core" else int(default * 0.7)
    if cell.last_saturated:
        # Escalate after truncation, so a busy cell converges toward unsaturated -- an
        # unsaturated observation is the only one that yields an unbiased count.
        wanted = int(wanted * 1.5)
    return int(min(maximum, max(25, wanted)))


def estimate_units(source, results_wanted, config):
    """Cost model that makes the two sources commensurable.

    Indeed returns ~15 results per GraphQL page with descriptions included. LinkedIn
    returns 25 per page but charges one extra request per description, which is why its
    per-eligible-posting cost is roughly 15x Indeed's.
    """
    page_size = (config.get("page_size") or {}).get(source, 25)
    pages = math.ceil(max(results_wanted, 1) / max(page_size, 1))
    return float(pages)


def _make_task(cell, config, roles, source, now, backfill=False):
    location = roles.locations.get(cell.location_id)
    budget = (config.get("budgets") or {}).get(source, {})
    fetch_setting = budget.get("fetch_descriptions", False)
    if backfill:
        results_wanted = budget.get("max_results_wanted", 200)
    else:
        results_wanted = results_wanted_for(cell, config, source)

    # 'budgeted' means: run the search cheaply, then fetch descriptions only for the
    # top-scoring subset. Those descriptions are NOT a census, so they must never reach
    # skill-demand denominators -- desc_selection records which regime produced them.
    if fetch_setting is True:
        fetch_description, desc_selection = True, "census"
    elif fetch_setting == "budgeted":
        fetch_description, desc_selection = False, "top_k"
    else:
        fetch_description, desc_selection = False, "none"

    return ScrapeTask(
        cell_id=cell.id,
        source=source,
        query=cell.query,
        location_id=cell.location_id,
        location_label=location.label if location else cell.location_id,
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
        tier=cell.tier,
        est_request_units=estimate_units(source, results_wanted, config),
    )


def select_cells(cells, config, roles, source, now=None, backfill=False):
    """Pick this run's cells for one source, within budget.

    `cells` is every persisted cell for the source; filtering, ranking, and budgeting all
    happen here so the policy is testable in isolation.
    """
    now = now or datetime.now(timezone.utc)
    budget = (config.get("budgets") or {}).get(source, {})
    max_searches = budget.get("searches_per_run", 10)
    max_units = budget.get("request_units", 100)
    max_pages = budget.get("max_pages_per_run")

    eligible = [c for c in cells if is_eligible(c, now)]
    ranked = sorted(
        eligible, key=lambda c: (-cell_priority(c, config, now), c.id)
    )

    # Cells past their tier deadline go first, then the priority ranking fills the rest.
    # Coverage is therefore an explicit guarantee, not something emerging from the product
    # of six weighting factors.
    starved = starved_cells(eligible, config, now, source)
    starved_ids = {c.id for c in starved}
    order = starved + [c for c in ranked if c.id not in starved_ids]

    picked = []
    units = 0.0
    pages = 0
    page_size = max((config.get("page_size") or {}).get(source, 25), 1)

    for cell in order:
        if len(picked) >= max_searches:
            break
        task = _make_task(cell, config, roles, source, now, backfill=backfill)
        cell_pages = math.ceil(task.results_wanted / page_size)
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


def enforce_staleness_floor(picked, ranked, cells, config, roles, source, now,
                            backfill=False):
    """Guarantee core cells in high-weight locations are visited within the floor.

    Keys on last_SUCCESS_at, not last_scraped_at: a repeatedly-failing cell must still read
    as overdue, or the dashboard reports healthy coverage of data never collected.
    """
    floor = config.get("max_staleness_hours", 72)
    location_weights = config.get("locations") or {}
    chosen_ids = {t.cell_id for t in picked}

    overdue = [
        c for c in ranked
        if c.tier == "core"
        and location_weights.get(c.location_id, {}).get("weight", 1.0) >= 1.0
        and hours_since(c.last_success_at, now) > floor
        and c.id not in chosen_ids
    ]
    if not overdue:
        return picked

    result = list(picked)
    for cell in overdue:
        if not result:
            break
        # `picked` is priority-ordered, so the last entry is the cheapest to drop.
        result.pop()
        result.append(
            _make_task(cell, config, roles, source, now, backfill=backfill)
        )

    return result


def overdue_cells(cells, config, now=None):
    """Core cells past the staleness floor, for the coverage warning and for suppression.

    A scheduler coverage failure must surface as a *suppression reason* on the affected
    families rather than as a quietly wrong bar in the supply chart.
    """
    now = now or datetime.now(timezone.utc)
    floor = config.get("max_staleness_hours", 72)
    weights = config.get("locations") or {}
    return [
        c for c in cells
        if c.enabled
        and c.tier == "core"
        and weights.get(c.location_id, {}).get("weight", 1.0) >= 1.0
        and hours_since(c.last_success_at, now) > floor
    ]


def update_ewma(previous, observed, alpha=EWMA_ALPHA):
    if previous is None:
        return float(observed)
    return alpha * float(observed) + (1 - alpha) * float(previous)


def is_saturated(returned, requested, threshold=0.95):
    """Saturation is right-censoring: the board truncated us, so the count is a lower bound.

    This is the informative case, not a nuisance -- an unsaturated cell saw essentially
    every matching posting and therefore yields an unbiased count.
    """
    if not requested:
        return False
    return returned >= threshold * requested
