"""Market supply and skill-demand estimation.

The hard part is not computing averages, it is not lying. Coverage rotates across ~252 cells
on a ~5-day cycle, so raw posting counts across role families are NOT comparable: a family
that was scraped three times looks bigger than one scraped once, regardless of the market.
Three mechanisms keep the numbers honest.

  Flow, not counts. The headline supply metric is postings per day, computed over the
  interval UNION of each observation's [observed_at - hours_old, observed_at] window. Never
  sum hours_old: with hours_old=72 on a 24h cadence, summing triple-counts exposure and
  understates flow by ~3x.

  Saturation is right-censoring. If a board returned ~everything we asked for, there were
  probably more, so the count is a LOWER BOUND and is rendered as such. An unsaturated cell
  saw essentially every matching posting and yields an unbiased count. The truncation is the
  informative bit, not a nuisance.

  Post-stratification for skill demand. Demand is a within-posting ratio, so it survives
  uneven sampling *sizes* -- but not an uneven sampling *mix*. Weights come from a declared
  reference mix rather than the observed sample, so the estimate stays comparable as the
  rotation changes. Confidence intervals then use Kish n_eff, because reweighting an
  unbalanced sample inflates variance and a raw-n interval would understate uncertainty by
  exactly the amount the scheduler misbehaved.

Absolute supply is not estimable at all -- boards never report totals and results_wanted
truncates -- so nothing here claims "there are N Kubernetes jobs".
"""

import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from careerradar.core.database import Database
    from careerradar.profile.adapter import ProfileAdapter
    from careerradar.taxonomy.roles import RoleTaxonomy
    from careerradar.taxonomy.skills import Taxonomy

SUPPRESS_COVERAGE_GAP = "coverage_gap"
SUPPRESS_TOO_FEW_OBSERVATIONS = "too_few_observations"
SUPPRESS_SMALL_SAMPLE = "small_sample"
SUPPRESS_TOO_FEW_COMPANIES = "too_few_companies"
SUPPRESS_COMPANY_CONCENTRATION = "company_concentration"
SUPPRESS_COVERAGE_INCOMPLETE = "coverage_incomplete"
SUPPRESS_NO_SALARY_DATA = "insufficient_salary_data"


# =========================================================================================
# Interval arithmetic
# =========================================================================================


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Union of [start, end] datetime pairs, as a list of disjoint pairs."""
    cleaned = [(s, e) for s, e in intervals if s and e and e > s]
    if not cleaned:
        return []
    cleaned.sort()
    merged = [list(cleaned[0])]
    for start, end in cleaned[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def covered_days(
    intervals: list[tuple[datetime, datetime]], window_start: datetime, window_end: datetime
) -> float:
    """Days of observation coverage inside the analysis window.

    Uses the UNION of exposure intervals. Summing hours_old instead would multiply-count
    overlapping windows and deflate every flow estimate.
    """
    clipped = []
    for start, end in intervals:
        clipped.append((max(start, window_start), min(end, window_end)))
    total = sum((end - start).total_seconds() for start, end in merge_intervals(clipped))
    return total / 86400.0


# =========================================================================================
# Interval estimation
# =========================================================================================


def wilson_interval(successes: float, total: float, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval. Behaves sensibly at small n and near 0 or 1, unlike normal
    approximation -- 21 of 40 should not be presented with the same confidence as 300 of 600.
    """
    if total <= 0:
        return 0.0, 0.0, 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return proportion, max(0.0, centre - margin), min(1.0, centre + margin)


def kish_n_eff(weights: list[float]) -> float:
    """Kish effective sample size: (sum w)^2 / sum w^2.

    Reweighting an unbalanced sample costs precision. Using raw n in a confidence interval
    would understate uncertainty by precisely the amount the scrape rotation was skewed.
    """
    if not weights:
        return 0.0
    total = sum(weights)
    squares = sum(w * w for w in weights)
    if squares <= 0:
        return 0.0
    return (total * total) / squares


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Median of (value, weight) pairs."""
    cleaned = [(v, w) for v, w in pairs if v is not None and w > 0]
    if not cleaned:
        return None
    cleaned.sort()
    half = sum(w for _, w in cleaned) / 2.0
    running = 0.0
    for value, weight in cleaned:
        running += weight
        if running >= half:
            return value
    return cleaned[-1][0]


# =========================================================================================
# Post-stratification
# =========================================================================================


def build_stratum_weights(
    stratum_counts: dict[str, int],
    reference_mix: dict[str, float],
    min_stratum_n: int = 10,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Per-posting weights that rake the observed sample toward a declared mix.

    Returns (weights_by_stratum, diagnostics). `missing_weight` is the share of the target
    mix that could not be filled because those strata were not sampled enough -- the
    mechanism by which a scheduler coverage failure becomes a *visible caveat* rather than a
    quietly wrong number.
    """
    usable = {
        key: n
        for key, n in stratum_counts.items()
        if n >= min_stratum_n and reference_mix.get(key, 0) > 0
    }
    target_total = sum(reference_mix.get(key, 0) for key in usable)
    requested_total = sum(v for v in reference_mix.values() if v > 0)

    diagnostics = {
        "strata_used": len(usable),
        "strata_available": len(stratum_counts),
        "n_used": sum(usable.values()),
        "missing_weight": (
            round(1.0 - (target_total / requested_total), 4) if requested_total > 0 else 1.0
        ),
    }

    if not usable or target_total <= 0:
        # Fall back to equal weights: honest, and flagged via missing_weight.
        return dict.fromkeys(stratum_counts, 1.0), diagnostics

    n_used = diagnostics["n_used"]
    weights = {}
    for key, n in usable.items():
        target_share = reference_mix[key] / target_total
        observed_share = n / n_used
        weights[key] = target_share / observed_share if observed_share else 0.0
    # Strata outside the reference mix contribute nothing to a weighted estimate.
    for key in stratum_counts:
        weights.setdefault(key, 0.0)
    return weights, diagnostics


# =========================================================================================
# Comparability
# =========================================================================================


def hours_old_bucket(hours_old: int | None) -> str:
    if hours_old is None:
        return "unknown"
    if hours_old <= 72:
        return "<=72"
    if hours_old <= 168:
        return "<=168"
    return "<=336"


def results_bucket(requested: int | None) -> str:
    if not requested:
        return "unknown"
    if requested <= 50:
        return "<=50"
    if requested <= 100:
        return "<=100"
    return "<=200"


def comparability_class(
    location_id: str | None,
    source: str | None,
    hours_old: int | None,
    requested: int | None,
) -> str:
    """Flow is comparable only within one of these classes.

    Backfill observations (hours_old=336, results_wanted=200) therefore form their own class
    and never pool with incremental ones for flow -- though they count fully for skill
    demand, which does not depend on hours_old.
    """
    return f"{location_id}|{source}|{hours_old_bucket(hours_old)}|{results_bucket(requested)}"


# =========================================================================================
# Supply
# =========================================================================================


class MarketAnalytics:
    def __init__(
        self,
        db: "Database",
        config: dict[str, Any] | None,
        roles: "RoleTaxonomy",
        taxonomy: "Taxonomy",
        profile: "ProfileAdapter | None" = None,
    ) -> None:
        self.db = db
        self.config = config or {}
        self.analytics_config = self.config.get("analytics", {}) or {}
        self.roles = roles
        self.taxonomy = taxonomy
        self.profile = profile

    # -- helpers ----------------------------------------------------------------------
    def _window(self, window_days: int) -> tuple[datetime, datetime]:
        end = datetime.now(timezone.utc)
        return end - timedelta(days=window_days), end

    def _eligibility_sql(self, view: str = "v_supply_eligible") -> str:
        clause = f"SELECT * FROM {view} WHERE 1=1"
        if self.analytics_config.get("exclude_agencies", True):
            clause += " AND COALESCE(is_agency, 0) = 0"
        return clause

    def _gate(self, value: float, minimum: float, reason: str) -> str | None:
        return None if value < minimum else reason

    # -- role supply ------------------------------------------------------------------
    def role_supply(
        self,
        window_days: int = 30,
        location_id: str | None = None,
        source: str | None = "indeed",
    ) -> dict[str, Any]:
        """Postings per day per role family, within one comparability class.

        `location_id` and `source` are required rather than optional, because pooling across
        them is exactly the comparison that is not valid. The API surfaces small multiples
        (one panel per location) instead of a single pooled ranking.
        """
        window_start, window_end = self._window(window_days)
        min_coverage = self.analytics_config.get("min_coverage_fraction", 0.5)
        min_observations = self.analytics_config.get("min_observations_per_cell", 3)

        observations = self.db.conn.execute(
            """
            SELECT role_family, location_id, source, hours_old, requested, returned,
                   returned_on_topic, saturated, window_start, window_end, status
              FROM cell_observations
             WHERE status IN ('ok', 'empty')
               AND observed_at >= ?
               AND (? IS NULL OR location_id = ?)
               AND (? IS NULL OR source = ?)
            """,
            (window_start.isoformat(), location_id, location_id, source, source),
        ).fetchall()

        grouped = {}
        for row in observations:
            key = row["role_family"]
            record = grouped.setdefault(
                key,
                {
                    "intervals": [],
                    "observations": 0,
                    "on_topic": 0,
                    "saturated": 0,
                    "classes": set(),
                },
            )
            start = _parse(row["window_start"])
            end = _parse(row["window_end"])
            if start and end:
                record["intervals"].append((start, end))
            record["observations"] += 1
            record["on_topic"] += row["returned_on_topic"] or 0
            record["saturated"] += 1 if row["saturated"] else 0
            record["classes"].add(
                comparability_class(
                    row["location_id"], row["source"], row["hours_old"], row["requested"]
                )
            )

        postings = self._postings_by_family(window_start, location_id, source)

        rows = []
        for family, record in grouped.items():
            if not family:
                continue
            days = covered_days(record["intervals"], window_start, window_end)
            coverage_fraction = min(1.0, days / window_days) if window_days else 0.0
            counts = postings.get(family, {"n": 0, "companies": set(), "remote": 0, "salaries": []})
            censored = record["saturated"] > 0

            suppressed = None
            if record["observations"] < min_observations:
                suppressed = SUPPRESS_TOO_FEW_OBSERVATIONS
            elif coverage_fraction < min_coverage:
                suppressed = SUPPRESS_COVERAGE_GAP

            flow = (counts["n"] / days) if days > 0 else None
            salaries = sorted(counts["salaries"])

            rows.append(
                {
                    "role_family": family,
                    "label": self.roles.label(family),
                    "tier": self.roles.tier(family),
                    "resume": self.roles.resume_for(family),
                    "flow_per_day": round(flow, 2) if flow is not None else None,
                    # A censored flow is a lower bound. The UI must render it with an open bar
                    # cap and a ">=" label rather than as an ordinary value.
                    "censored": censored,
                    "n_postings": counts["n"],
                    "n_companies": len(counts["companies"]),
                    "remote_share": (
                        round(counts["remote"] / counts["n"], 3) if counts["n"] else None
                    ),
                    "median_salary_usd": (
                        salaries[len(salaries) // 2] if len(salaries) >= 5 else None
                    ),
                    "covered_days": round(days, 2),
                    "coverage_fraction": round(coverage_fraction, 3),
                    "saturated_share": round(record["saturated"] / record["observations"], 3)
                    if record["observations"]
                    else None,
                    "n_observations": record["observations"],
                    "comparability_classes": sorted(record["classes"]),
                    # Distinguishes "we looked and found none" from "we measured low demand".
                    # Both are 0.0/day, but only the second is a market fact, and rendering a
                    # zero-length bar for the first reads as the second.
                    "zero_yield": counts["n"] == 0,
                    "suppressed_reason": suppressed,
                }
            )

        published = [r for r in rows if not r["suppressed_reason"]]
        total_flow = sum(r["flow_per_day"] or 0 for r in published)
        for row in published:
            row["share"] = round((row["flow_per_day"] or 0) / total_flow, 4) if total_flow else None
        for row in rows:
            row.setdefault("share", None)

        rows.sort(key=lambda r: r["flow_per_day"] or -1, reverse=True)

        return {
            "window_days": window_days,
            "location_id": location_id,
            "source": source,
            "rows": rows,
            "provenance": self._provenance(window_days, len(published), len(rows)),
        }

    def _postings_by_family(
        self, window_start: datetime, location_id: str | None, source: str | None
    ) -> dict[str, dict[str, Any]]:
        query = self._eligibility_sql() + " AND date_found >= ?"
        params = [window_start.isoformat()]
        if source:
            query += " AND source = ?"
            params.append(source)
        if location_id:
            # Filter through the cell that produced the posting. Without this every location
            # reported identical counts -- Swedish SRE postings equalling US ones -- because
            # the exposure intervals were location-scoped but the postings were not.
            query += " AND scrape_cell_id IN (SELECT id FROM scrape_cells WHERE location_id = ?)"
            params.append(location_id)

        grouped = {}
        for row in self.db.conn.execute(query, params):
            family = row["role_family"]
            record = grouped.setdefault(
                family,
                {
                    "n": 0,
                    "companies": set(),
                    "remote": 0,
                    "salaries": [],
                },
            )
            record["n"] += 1
            if row["company_normalized"]:
                record["companies"].add(row["company_normalized"])
            if row["is_remote"]:
                record["remote"] += 1
            if row["salary_annual_usd"]:
                record["salaries"].append(row["salary_annual_usd"])
        return grouped

    def coverage_report(self) -> list[dict[str, Any]]:
        """Per-cell health, so a silently degrading scraper becomes obvious.

        `query` and the two EWMAs are part of the report rather than internal scheduler
        state: a cell is (source, family, location, QUERY), and without the query term the
        report cannot answer which phrasing is earning its cell. That question had to be
        answered by a live A/B probe against the boards once already, purely because the
        numbers the scheduler was already keeping were not exposed anywhere.
        """
        rows = self.db.conn.execute(
            """
            SELECT source, location_id, role_family, tier, enabled, query,
                   last_scraped_at, last_success_at, last_result_count,
                   last_saturated, consecutive_empty, consecutive_error,
                   total_scrapes, backoff_until,
                   ewma_new_per_scrape, ewma_fit_score, quality_samples
              FROM scrape_cells
             ORDER BY source, location_id, role_family
            """
        ).fetchall()
        now = datetime.now(timezone.utc)
        out = []
        for row in rows:
            record = dict(row)
            success = _parse(row["last_success_at"])
            record["hours_since_success"] = (
                round((now - success).total_seconds() / 3600, 1) if success else None
            )
            out.append(record)
        return out

    def _provenance(self, window_days: int, published: int, total: int) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "min_window_days": self.analytics_config.get("min_window_days", 30),
            "window_below_minimum": window_days < self.analytics_config.get("min_window_days", 30),
            "published_rows": published,
            "total_rows": total,
            "suppressed_rows": total - published,
            "exclude_agencies": self.analytics_config.get("exclude_agencies", True),
            "taxonomy_hash": self.taxonomy.hash,
            "roles_hash": self.roles.hash,
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
