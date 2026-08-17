"""Scrape-budget scheduler: choose which cells to visit this run.

The matrix is 410 cells (205 per source), rotated across runs rather than swept, on the
assumption that LinkedIn rate-limits around the 10th page on a single IP -- carried forward
from prior scraping experience but never actually verified against this project's traffic
(source_state.total_429 sat at 0 across every real run). A direct probe on 2026-08-14
(scripts/probe_linkedin_page_wall.py, results in experiments/linkedin_page_wall/) found zero
429s or blocks across 99 consecutive pages on one proxied IP -- the run stopped at page 100
only because that's the guest API's own pagination ceiling (offset ~1000, same as JobSpy's
hardcoded start < 1000), not IP-based blocking. So the wall, if it exists at all, is not
where this comment used to claim. Rotation is kept for now as a request-budget control
rather than a proven rate-limit avoidance. Everything here is a pure function over
CellState, so the rotation policy is unit-testable without a network.

The budget that rotation spends is `estimate_units`, and until 2026-08-15 it counted search
pages only -- so a LinkedIn description census was charged 5 requests and spent 56. A pure
function is testable, not correct: what makes this one checkable is `cell_observations`
recording what each cell really spent, and `careerradar search cost` comparing the two.

Two properties the tests pin down, because both fail silently in production:

  Eventual coverage -- `urgency` grows without bound (capped at 3) and the dead-cell penalty
  decays but never reaches zero, so every enabled cell is eventually visited. That matters
  because "Kubernetes roles vanished from Helsinki" and "my Helsinki query broke" are
  indistinguishable unless you keep probing.

  hours_old >= 1.5x the actual revisit gap -- how far back a scrape looks is derived from
  how long that cell really went unvisited, so a long wait widens the next window instead of
  leaving a hole. A hole does not corrupt the published numbers: analytics unions the
  exposure intervals, so uncovered time drops out of the denominator. It costs postings.
  Everything published inside the hole is never collected, never scored and never shown.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from careerradar.taxonomy.roles import RoleTaxonomy

EWMA_ALPHA = 0.4

# Default productivity assumption for a cell that has never run, so first-time cells are
# neither starved nor over-favoured.
DEFAULT_NEW_PER_SCRAPE = 2.0

TIER_WEIGHT = {"core": 1.0, "adjacent": 0.6, "breadth": 0.3}

# A cell's quality modifier stays neutral (1.0) until it clears this many verdicts, so one
# or two bad LLM scores can't tank a cell before it's had a fair sample -- the same
# exploration concern novelty/DEAD_PENALTY_FLOOR already guard for the yield signal.
QUALITY_SAMPLE_MIN = 3

# fit_score runs roughly 0-100 (keyword_score.py's observed corpus range is 15-80).
# Centered on 0.5 + fit_score/100 and clamped so quality can only ever nudge productivity,
# never zero it out on its own -- reinforcing only what already scores well would narrow
# the net toward postings that match the user's existing skills, which is the opposite of
# what widening the query surface (see roles.py/cell_specs) is for.
QUALITY_CLAMP = (0.5, 1.5)

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
    tier: str | None = None
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
            "tier": self.tier,
            "est_request_units": self.est_request_units,
            "ewma_new_per_scrape": self.ewma_new_per_scrape,
            "proxies": self.extra.get("proxies") or [],
        }


def with_location_weights(scraper_config: dict[str, Any], roles: "RoleTaxonomy") -> dict[str, Any]:
    """Copy of `scraper_config` carrying roles.yaml's per-location weights.

    Weights are declared in roles.yaml but consumed here, off the scraper config. Every
    entry point into this module must go through this, because the failure mode when it is
    skipped is silent rather than loud: `.get("locations")` returns {}, every location
    weighs 1.0, and both the ranking and the `>= 1.0` filters in enforce_staleness_floor /
    overdue_cells quietly widen to include relocation cells.
    """
    merged = dict(scraper_config)
    merged["locations"] = {
        location_id: {"weight": location.weight}
        for location_id, location in roles.locations.items()
    }
    return merged


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
    if not cell.enabled:
        return False
    backoff = _parse(cell.backoff_until)
    return backoff is None or backoff <= now


def quality_multiplier(cell: CellState) -> float:
    """How much a cell's realized LLM fit_score should nudge its scrape priority.

    Neutral until `quality_samples` clears QUALITY_SAMPLE_MIN, then a clamped modifier
    derived from `ewma_fit_score` -- fed from job_verdicts at scoring time, not from the
    scrape itself, see scoring/worker.py. Clamped to QUALITY_CLAMP so it can only ever
    nudge productivity, never zero it out on its own regardless of how poorly a cell's
    postings have scored.
    """
    if cell.ewma_fit_score is None or cell.quality_samples < QUALITY_SAMPLE_MIN:
        return 1.0
    modifier = 0.5 + cell.ewma_fit_score / 100.0
    return min(QUALITY_CLAMP[1], max(QUALITY_CLAMP[0], modifier))


def cell_priority(cell: CellState, config: dict[str, Any], now: datetime) -> float:
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


def starved_cells(
    cells: list[CellState], config: dict[str, Any], now: datetime, source: str | None = None
) -> list[CellState]:
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
        key=lambda c: (
            -(hours_since(c.last_scraped_at, now) / max(cadences.get(c.tier, 168) * multiple, 1))
        ),
    )


def adaptive_hours_old(cell: CellState, config: dict[str, Any], now: datetime) -> int:
    """How far back one scrape of this cell looks, in hours.

    Derived from the cell's own revisit gap rather than read from config, because the
    scheduler rotates: which cells a run visits comes from the priority ranking, so a cell
    waits anywhere from hours to weeks and no single configured value fits all of them. A
    window shorter than that wait means postings published between the two visits are never
    seen. The 1.5x is overlap margin -- dedup drops the repeats, so overlap is cheap and a
    hole is not.

    Two limits. Past a ~224h wait the ceiling wins and the window no longer covers the whole
    wait. And results_wanted_for() does not read hours_old, so a wide window asks for the
    same number of postings and is more likely to come back truncated.
    """
    floors = config.get("hours_old_floor") or {}
    floor = floors.get(cell.tier, 168)
    gap = hours_since(cell.last_success_at, now, default=config.get("backfill_hours_old", 336))
    ceiling = config.get("max_hours_old", 336)
    return int(min(ceiling, max(floor, 1.5 * gap)))


def results_wanted_for(cell: CellState, config: dict[str, Any], source: str) -> int:
    budget = (config.get("budgets") or {}).get(source, {})
    default = budget.get("results_wanted_default", 50)
    maximum = budget.get("max_results_wanted", 200)
    wanted = default if cell.tier == "core" else int(default * 0.7)
    if cell.last_saturated:
        # Escalate after truncation, so a busy cell converges toward unsaturated -- an
        # unsaturated observation is the only one that yields an unbiased count.
        wanted = int(wanted * 1.5)
    return int(min(maximum, max(25, wanted)))


def estimate_pages(source: str, results_wanted: int, config: dict[str, Any]) -> int:
    """Search-page requests one cell will spend.

    The +1 is not padding. Both boards spend a final request discovering that there is
    nothing left to page through, and only a cell that saturates avoids it: over 11
    measured cells on 2026-08-15, every Indeed cell that returned fewer results than it
    asked for spent 2 requests for one page of results, the two that saturated spent 1,
    and LinkedIn spent 6-7 pages where results_wanted=50 at 10 per page implies 5.
    Saturation is not knowable when the budget is drawn up, so the estimate takes the
    higher of the two.
    """
    page_size = max((config.get("page_size") or {}).get(source, 25), 1)
    return math.ceil(max(results_wanted, 1) / page_size) + 1


def estimate_units(
    source: str,
    results_wanted: int,
    config: dict[str, Any],
    fetch_descriptions: bool = False,
) -> float:
    """Total HTTP requests one cell will spend -- the thing `request_units` budgets.

    Pages alone are not the cost. Indeed serves descriptions inside the search page, so a
    census there is free; LinkedIn spends one further request per posting, which makes a
    50-posting census cell cost 56 requests against the 5 that a pages-only model charged
    it. That gap is not an accounting curiosity: `request_units` is what decides how many
    cells a run visits, so undercounting by 11x meant the LinkedIn budget bought a tenth
    of the cells it appeared to, and the rotation was that much slower than configured.

    The per-description cost is read from config (`requests_per_description`) rather than
    branched on the source name here, because it is a property of the board's API, and the
    same file already states the other one (`page_size`).
    """
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

    # desc_selection must describe what actually happened, because the skill-demand views
    # gate on it.
    #
    #   census  every posting in the cell got a description -> no selection bias, so the
    #           cell can feed skill statistics.
    #   none    no descriptions at all -> the cell contributes titles only, which is still
    #           complete for role supply.
    #   top_k   a pre-score-selected subset (reserved; not currently produced).
    #
    # 'budgeted' previously mapped to top_k, which was wrong twice over: nothing fetched
    # descriptions for the selected subset, so LinkedIn postings had NO description and
    # scored on titles alone, and labelling an empty set top_k implied a bias that was not
    # there. Enabling descriptions for the whole cell (which a rotating proxy pool makes
    # affordable) is both simpler and statistically cleaner than sampling a biased subset.
    if fetch_setting is True:
        fetch_description, desc_selection = True, "census"
    else:
        fetch_description, desc_selection = False, "none"

    return ScrapeTask(
        cell_id=cell.id,
        source=source,
        query=cell.query,
        location_id=cell.location_id,
        # search_label, not label: this string is sent to the board as the location.
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
        tier=cell.tier,
        ewma_new_per_scrape=cell.ewma_new_per_scrape,
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
    ranked = sorted(eligible, key=lambda c: (-cell_priority(c, config, now), c.id))

    # Cells past their tier deadline go first, then the priority ranking fills the rest.
    # Coverage is therefore an explicit guarantee, not something emerging from the product
    # of six weighting factors.
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
        # Pages and units are two different caps: `max_pages_per_run` limits search traffic
        # to the board, `request_units` limits every request including the per-description
        # ones. They were the same number while descriptions were uncounted.
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
    """Guarantee core cells in high-weight locations are visited within the floor.

    Keys on last_SUCCESS_at, not last_scraped_at: a repeatedly-failing cell must still read
    as overdue, or the dashboard reports healthy coverage of data never collected.
    """
    floor = config.get("max_staleness_hours", 72)
    location_weights = config.get("locations") or {}
    chosen_ids = {t.cell_id for t in picked}

    overdue = [
        c
        for c in ranked
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
        result.append(_make_task(cell, config, roles, source, now, backfill=backfill))

    return result


def overdue_cells(
    cells: list[CellState], config: dict[str, Any], now: datetime | None = None
) -> list[CellState]:
    """Core cells past the staleness floor, for the coverage warning and for suppression.

    A scheduler coverage failure must surface as a *suppression reason* on the affected
    families rather than as a quietly wrong bar in the supply chart.
    """
    now = now or datetime.now(timezone.utc)
    floor = config.get("max_staleness_hours", 72)
    weights = config.get("locations") or {}
    return [
        c
        for c in cells
        if c.enabled
        and c.tier == "core"
        and weights.get(c.location_id, {}).get("weight", 1.0) >= 1.0
        and hours_since(c.last_success_at, now) > floor
    ]


def update_ewma(previous: float | None, observed: float, alpha: float = EWMA_ALPHA) -> float:
    if previous is None:
        return float(observed)
    return alpha * float(observed) + (1 - alpha) * float(previous)


def is_saturated(returned: int, requested: int, threshold: float = 0.95) -> bool:
    """Saturation is right-censoring: the board truncated us, so the count is a lower bound.

    This is the informative case, not a nuisance -- an unsaturated cell saw essentially
    every matching posting and therefore yields an unbiased count.
    """
    if not requested:
        return False
    return returned >= threshold * requested
