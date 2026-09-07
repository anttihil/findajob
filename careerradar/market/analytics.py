"""Market analytics: search query yield estimation and skill-demand statistics.

Provides query yield tracking for (source, query, location) search tuples,
evaluating posting volume vs scoring agent strong fits, and the Wilson score
interval used by skills gap analysis.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from careerradar.market import repository as market_repo

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
        roles: "RoleTaxonomy",
        taxonomy: "Taxonomy | None" = None,
        profile: "ProfileAdapter | None" = None,
    ) -> None:
        self.db = db
        self.config = config or {}
        self.analytics_config = self.config.get("analytics", {}) or {}
        self.roles = roles
        self.taxonomy = taxonomy
        self.profile = profile

    def _window(self, window_days: int) -> tuple[datetime, datetime]:
        end = datetime.now(timezone.utc)
        return end - timedelta(days=window_days), end

    def coverage_report(self) -> list[dict[str, Any]]:
        """Per-cell scrape health and statistics."""
        return market_repo.get_coverage_report_cells(self.db.conn)

    def query_yield(
        self,
        window_days: int | None = None,
        source: str | None = None,
        location_id: str | None = None,
        query: str | None = None,
        min_postings: int = 0,
    ) -> dict[str, Any]:
        """Yield analysis for search query tuples (source, query, location).

        Tracks how many postings were found and how many were scored as a strong fit
        by the scoring agent, allowing the user to evaluate and edit query terms over time.
        """
        window_start = None
        if window_days and window_days > 0:
            start_dt, _ = self._window(window_days)
            window_start = start_dt.isoformat()

        raw_cells = market_repo.get_query_yield_cells(
            self.db.conn,
            window_start=window_start,
            source=source,
            location_id=location_id,
            query=query,
            min_postings=min_postings,
        )

        tuples: list[dict[str, Any]] = []
        query_map: dict[str, dict[str, Any]] = {}
        source_map: dict[str, dict[str, Any]] = {}
        location_map: dict[str, dict[str, Any]] = {}

        total_postings_sum = 0
        total_unique_sum = 0
        total_scored_sum = 0
        total_strong_fits_sum = 0

        for r in raw_cells:
            tot = r["total_postings"]
            uniq = r["unique_postings"]
            scored = r["scored_postings"]
            fits = r["strong_fits"]
            no_fits = r["no_fits"]
            q = r["query"]
            src = r["source"]
            loc = r["location_id"]

            total_postings_sum += tot
            total_unique_sum += uniq
            total_scored_sum += scored
            total_strong_fits_sum += fits

            fit_rate = (fits / scored) if scored > 0 else 0.0
            yield_rate = (fits / tot) if tot > 0 else 0.0

            if fits >= 2 or (scored >= 3 and fit_rate >= 0.25):
                category = "high_yield"
            elif fits >= 1:
                category = "moderate_yield"
            elif scored >= 5 and fits == 0:
                category = "zero_yield"
            elif scored > 0 and fits == 0:
                category = "low_yield"
            elif tot > 0:
                category = "unscored"
            else:
                category = "unscraped"

            tuple_item = {
                "cell_id": r["cell_id"],
                "source": src,
                "query": q,
                "location_id": loc,
                "enabled": bool(r["enabled"]),
                "search_query_id": r["search_query_id"],
                "total_postings": tot,
                "unique_postings": uniq,
                "scored_postings": scored,
                "strong_fits": fits,
                "no_fits": no_fits,
                "fit_rate": round(fit_rate, 4),
                "fit_rate_pct": round(fit_rate * 100, 1),
                "yield_rate": round(yield_rate, 4),
                "yield_rate_pct": round(yield_rate * 100, 1),
                "total_scrapes": r["total_scrapes"],
                "last_scraped_at": r["last_scraped_at"],
                "last_success_at": r["last_success_at"],
                "yield_category": category,
            }
            tuples.append(tuple_item)

            # Aggregate by query text
            if q not in query_map:
                query_map[q] = {
                    "query": q,
                    "search_query_id": r["search_query_id"],
                    "total_postings": 0,
                    "unique_postings": 0,
                    "scored_postings": 0,
                    "strong_fits": 0,
                    "no_fits": 0,
                    "total_scrapes": 0,
                    "sources": set(),
                    "locations": set(),
                    "cells_count": 0,
                }
            q_rec = query_map[q]
            q_rec["total_postings"] += tot
            q_rec["unique_postings"] += uniq
            q_rec["scored_postings"] += scored
            q_rec["strong_fits"] += fits
            q_rec["no_fits"] += no_fits
            q_rec["total_scrapes"] += r["total_scrapes"]
            q_rec["cells_count"] += 1
            if tot > 0 or r["total_scrapes"] > 0:
                q_rec["sources"].add(src)
                q_rec["locations"].add(loc)

            # Aggregate by source
            if src not in source_map:
                source_map[src] = {
                    "source": src,
                    "total_postings": 0,
                    "scored_postings": 0,
                    "strong_fits": 0,
                }
            source_map[src]["total_postings"] += tot
            source_map[src]["scored_postings"] += scored
            source_map[src]["strong_fits"] += fits

            # Aggregate by location
            if loc not in location_map:
                location_map[loc] = {
                    "location_id": loc,
                    "total_postings": 0,
                    "scored_postings": 0,
                    "strong_fits": 0,
                }
            location_map[loc]["total_postings"] += tot
            location_map[loc]["scored_postings"] += scored
            location_map[loc]["strong_fits"] += fits

        top_queries = []
        for q_rec in query_map.values():
            s_post = q_rec["scored_postings"]
            s_fit = q_rec["strong_fits"]
            tot = q_rec["total_postings"]
            fr = (s_fit / s_post) if s_post > 0 else 0.0
            yr = (s_fit / tot) if tot > 0 else 0.0

            if s_fit >= 2 or (s_post >= 3 and fr >= 0.25):
                cat = "high_yield"
            elif s_fit >= 1:
                cat = "moderate_yield"
            elif s_post >= 5 and s_fit == 0:
                cat = "zero_yield"
            elif s_post > 0 and s_fit == 0:
                cat = "low_yield"
            elif tot > 0:
                cat = "unscored"
            else:
                cat = "unscraped"

            top_queries.append(
                {
                    "query": q_rec["query"],
                    "search_query_id": q_rec["search_query_id"],
                    "total_postings": tot,
                    "unique_postings": q_rec["unique_postings"],
                    "scored_postings": s_post,
                    "strong_fits": s_fit,
                    "no_fits": q_rec["no_fits"],
                    "fit_rate": round(fr, 4),
                    "fit_rate_pct": round(fr * 100, 1),
                    "yield_rate": round(yr, 4),
                    "yield_rate_pct": round(yr * 100, 1),
                    "total_scrapes": q_rec["total_scrapes"],
                    "sources": sorted(q_rec["sources"]),
                    "locations": sorted(q_rec["locations"]),
                    "cells_count": q_rec["cells_count"],
                    "yield_category": cat,
                }
            )

        top_queries.sort(
            key=lambda x: (x["strong_fits"], x["fit_rate"], x["total_postings"]),
            reverse=True,
        )

        zero_yield_queries = [
            q for q in top_queries if q["strong_fits"] == 0 and q["scored_postings"] >= 3
        ]
        zero_yield_queries.sort(key=lambda x: x["scored_postings"], reverse=True)

        by_source = []
        for s_rec in source_map.values():
            sc_tot = s_rec["scored_postings"]
            sc_fit = s_rec["strong_fits"]
            s_fr = (sc_fit / sc_tot * 100) if sc_tot > 0 else 0.0
            by_source.append(
                {
                    "source": s_rec["source"],
                    "total_postings": s_rec["total_postings"],
                    "scored_postings": sc_tot,
                    "strong_fits": sc_fit,
                    "fit_rate_pct": round(s_fr, 1),
                }
            )
        by_source.sort(key=lambda x: x["strong_fits"], reverse=True)

        by_location = []
        for l_rec in location_map.values():
            lc_tot = l_rec["scored_postings"]
            lc_fit = l_rec["strong_fits"]
            l_fr = (lc_fit / lc_tot * 100) if lc_tot > 0 else 0.0
            loc_obj = getattr(self.roles, "locations", {}).get(l_rec["location_id"])
            loc_label = loc_obj.label if loc_obj else l_rec["location_id"]
            by_location.append(
                {
                    "location_id": l_rec["location_id"],
                    "location_label": loc_label,
                    "total_postings": l_rec["total_postings"],
                    "scored_postings": lc_tot,
                    "strong_fits": lc_fit,
                    "fit_rate_pct": round(l_fr, 1),
                }
            )
        by_location.sort(key=lambda x: x["strong_fits"], reverse=True)

        overall_fit_rate = (
            round((total_strong_fits_sum / total_scored_sum * 100), 1)
            if total_scored_sum > 0
            else 0.0
        )

        active_tuples = [t for t in tuples if t["total_postings"] > 0 or t["total_scrapes"] > 0]
        high_yield_count = sum(1 for q in top_queries if q["strong_fits"] > 0)
        zero_yield_count = len(zero_yield_queries)

        summary = {
            "total_tuples": len(tuples),
            "active_tuples": len(active_tuples),
            "total_postings": total_postings_sum,
            "total_unique_postings": total_unique_sum,
            "total_scored": total_scored_sum,
            "total_strong_fits": total_strong_fits_sum,
            "overall_fit_rate_pct": overall_fit_rate,
            "high_yield_queries_count": high_yield_count,
            "zero_yield_queries_count": zero_yield_count,
        }

        return {
            "window_days": window_days,
            "source": source,
            "location_id": location_id,
            "query": query,
            "summary": summary,
            "top_queries": top_queries,
            "zero_yield_queries": zero_yield_queries,
            "tuples": active_tuples if min_postings > 0 else tuples,
            "by_source": by_source,
            "by_location": by_location,
            "provenance": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "window_days": window_days,
                "exclude_agencies": self.analytics_config.get("exclude_agencies", True),
                "taxonomy_hash": self.taxonomy.hash if self.taxonomy else None,
                "roles_hash": self.roles.hash,
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
            "taxonomy_hash": self.taxonomy.hash if self.taxonomy else None,
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
