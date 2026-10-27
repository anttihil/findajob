"""Market analytics: search query yield estimation and skill-demand statistics.

Provides query yield tracking for (source, query, location) search tuples,
evaluating posting volume vs priority scores, and the Wilson score
interval used by skills gap analysis.
"""

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from findajob.market import repository as market_repo

if TYPE_CHECKING:
    from findajob.core.database import Database
    from findajob.profile.adapter import ProfileAdapter
    from findajob.search.targets import SearchTargets

SUPPRESS_COVERAGE_GAP = "coverage_gap"
SUPPRESS_TOO_FEW_OBSERVATIONS = "too_few_observations"
SUPPRESS_SMALL_SAMPLE = "small_sample"
SUPPRESS_TOO_FEW_COMPANIES = "too_few_companies"
SUPPRESS_COMPANY_CONCENTRATION = "company_concentration"


YIELD_COUNTS: tuple[str, ...] = (
    "total_postings",
    "scored_postings",
    "priority_postings",
    "first_priority",
    "consider_priority",
    "low_priority",
)


def _yield_metrics(counts: Mapping[str, Any]) -> dict[str, Any]:
    total, scored, useful = (counts[k] for k in YIELD_COUNTS[:3])
    rate = useful / scored if scored else 0.0
    yield_rate = useful / total if total else 0.0
    if useful >= 2 or (scored >= 3 and rate >= 0.25):
        category = "high_yield"
    elif useful:
        category = "moderate_yield"
    elif scored >= 5:
        category = "zero_yield"
    elif scored:
        category = "low_yield"
    elif total:
        category = "unscored"
    else:
        category = "unscraped"
    return {
        "priority_rate": round(rate, 4),
        "priority_rate_pct": round(rate * 100, 1),
        "yield_rate": round(yield_rate, 4),
        "yield_rate_pct": round(yield_rate * 100, 1),
        "yield_category": category,
    }


# =========================================================================================
# Statistical estimation (used by gap_analysis.py)
# =========================================================================================


def wilson_interval(successes: float, total: float, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval for proportion confidence."""
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


# =========================================================================================
# Market Analytics
# =========================================================================================


class MarketAnalytics:
    def __init__(
        self,
        db: "Database",
        config: dict[str, Any] | None,
        targets: "SearchTargets",
        profile: "ProfileAdapter | None" = None,
    ) -> None:
        self.db = db
        self.config = config or {}
        self.analytics_config = self.config.get("analytics", {}) or {}
        self.targets = targets
        self.profile = profile

    def _window(self, window_days: int) -> tuple[datetime, datetime]:
        end = datetime.now(timezone.utc)
        return end - timedelta(days=window_days), end

    def coverage_report(self) -> list[dict[str, Any]]:
        """Per-cell scrape health and statistics."""
        return market_repo.get_coverage_report_cells(self.db.conn)

    def tuple_progression(
        self, cell_id: int, window_days: int | None = None
    ) -> dict[str, Any] | None:
        """Daily discovery cohorts, including zero days, for one search tuple."""
        cell = self.db.conn.execute(
            "SELECT id, query, source, location_id FROM scrape_cells WHERE id = ?", (cell_id,)
        ).fetchone()
        if cell is None:
            return None
        _, end = self._window(window_days or 1)
        # Use complete UTC calendar dates, including today, for an N-day window.
        start = (end - timedelta(days=(window_days or 1) - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = market_repo.get_tuple_daily_yield(
            self.db.conn, cell_id, start.isoformat() if window_days else None, end.isoformat()
        )
        by_date = {row["date"]: row for row in rows}
        first_day = start.date() if window_days else end.date()
        if not window_days and rows:
            first_day = datetime.fromisoformat(rows[0]["date"]).date()
        points = []
        day = first_day
        while day <= end.date():
            date = day.isoformat()
            points.append(
                by_date.get(
                    date,
                    {"date": date, **dict.fromkeys(YIELD_COUNTS, 0)},
                )
            )
            day += timedelta(days=1)
        return {
            "tuple": dict(cell),
            "window_days": window_days,
            "start_date": first_day.isoformat(),
            "end_date": end.date().isoformat(),
            "points": points,
            "totals": {key: sum(point[key] for point in points) for key in YIELD_COUNTS},
        }

    def query_yield(
        self,
        window_days: int | None = None,
        source: str | None = None,
        location_id: str | None = None,
        query: str | None = None,
        min_postings: int = 0,
    ) -> dict[str, Any]:
        """Priority yield for discovery cohorts, using the same profile as the feed.

        First + Consider is the useful yield; Low completes the scored denominator.
        Historical binary verdicts never count as priority scores.
        """
        window_start = None
        if window_days and window_days > 0:
            _, end = self._window(window_days)
            window_start = (
                (end - timedelta(days=window_days - 1))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            )
        cells = market_repo.get_query_yield_cells(
            self.db.conn, window_start, source, location_id, query, min_postings
        )
        tuples = []
        query_map: dict[tuple[str, str], dict[str, Any]] = {}
        source_map: dict[str, dict[str, Any]] = {}
        location_map: dict[str, dict[str, Any]] = {}
        totals = dict.fromkeys(YIELD_COUNTS, 0)
        unique_total = 0
        for cell in cells:
            tuples.append({**cell, "enabled": bool(cell["enabled"]), **_yield_metrics(cell)})
            src, term, loc = cell["source"], cell["query"], cell["location_id"]
            q = query_map.setdefault(
                (src, term),
                {
                    "source": src,
                    "query": term,
                    "search_query_id": cell["search_query_id"],
                    **dict.fromkeys(YIELD_COUNTS, 0),
                    "unique_postings": 0,
                    "total_scrapes": 0,
                    "sources": set(),
                    "locations": set(),
                    "cells_count": 0,
                },
            )
            s = source_map.setdefault(src, {"source": src, **dict.fromkeys(YIELD_COUNTS, 0)})
            location = getattr(self.targets, "locations", {}).get(loc)
            location_totals = location_map.setdefault(
                loc,
                {
                    "location_id": loc,
                    "location_label": location.label if location else loc,
                    **dict.fromkeys(YIELD_COUNTS, 0),
                },
            )
            for aggregate in (totals, q, s, location_totals):
                for key in YIELD_COUNTS:
                    aggregate[key] += cell[key]
            unique_total += cell["unique_postings"]
            q["unique_postings"] += cell["unique_postings"]
            q["total_scrapes"] += cell["total_scrapes"]
            q["cells_count"] += 1
            if cell["total_postings"] or cell["total_scrapes"]:
                q["sources"].add(src)
                q["locations"].add(loc)

        top_queries = [
            {
                **q,
                "sources": sorted(q["sources"]),
                "locations": sorted(q["locations"]),
                **_yield_metrics(q),
            }
            for q in query_map.values()
        ]
        top_queries.sort(
            key=lambda q: (q["priority_postings"], q["priority_rate"], q["total_postings"]),
            reverse=True,
        )
        zero_yield_queries = sorted(
            (q for q in top_queries if not q["priority_postings"] and q["scored_postings"] >= 3),
            key=lambda q: q["scored_postings"],
            reverse=True,
        )
        by_source = [{**r, **_yield_metrics(r)} for r in source_map.values()]
        by_location = [{**r, **_yield_metrics(r)} for r in location_map.values()]
        for group in (by_source, by_location):
            group.sort(key=lambda r: r["priority_postings"], reverse=True)
        return {
            "window_days": window_days,
            "source": source,
            "location_id": location_id,
            "query": query,
            "summary": {
                "total_tuples": len(tuples),
                "active_tuples": sum(
                    bool(t["total_postings"] or t["total_scrapes"]) for t in tuples
                ),
                "total_postings": totals["total_postings"],
                "total_unique_postings": unique_total,
                "total_scored": totals["scored_postings"],
                "total_priority_postings": totals["priority_postings"],
                "total_first_priority": totals["first_priority"],
                "total_consider_priority": totals["consider_priority"],
                "total_low_priority": totals["low_priority"],
                "overall_priority_rate_pct": _yield_metrics(totals)["priority_rate_pct"],
                "high_yield_queries_count": sum(q["priority_postings"] > 0 for q in top_queries),
                "zero_yield_queries_count": len(zero_yield_queries),
            },
            "top_queries": top_queries,
            "zero_yield_queries": zero_yield_queries,
            "tuples": [t for t in tuples if t["total_postings"] or t["total_scrapes"]]
            if min_postings > 0
            else tuples,
            "by_source": by_source,
            "by_location": by_location,
            "provenance": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "window_days": window_days,
                "exclude_agencies": self.analytics_config.get("exclude_agencies", True),
                "search_targets_hash": self.targets.fingerprint,
            },
        }

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
            "search_targets_hash": self.targets.fingerprint,
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
