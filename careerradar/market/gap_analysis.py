"""Skill demand and gap analysis.

Answers the two questions the dashboard exists for: which in-demand skills does the user
already have, and which missing ones are worth acquiring next.

`blocking_gap` is the centrepiece and the reason this is more useful than a popularity
chart: it measures how often a skill the user lacks appears in postings they *otherwise*
match well. Kubernetes being popular is not actionable; Kubernetes being the one thing
standing between the user and 40% of the roles they'd otherwise be a strong fit for is.

Every estimate is post-stratified against a declared reference mix (see analytics.py) and
its confidence interval uses Kish n_eff rather than raw n. Every suppressed figure carries a
reason rather than silently vanishing.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from careerradar.market.analytics import (
    SUPPRESS_COMPANY_CONCENTRATION,
    SUPPRESS_COVERAGE_INCOMPLETE,
    SUPPRESS_SMALL_SAMPLE,
    SUPPRESS_TOO_FEW_COMPANIES,
    build_stratum_weights,
    kish_n_eff,
    weighted_median,
    wilson_interval,
)

if TYPE_CHECKING:
    from careerradar.core.database import Database
    from careerradar.profile.adapter import ProfileAdapter
    from careerradar.taxonomy.roles import RoleTaxonomy
    from careerradar.taxonomy.skills import Taxonomy

# A posting the user matches this well is "otherwise a good fit", so a missing skill in it
# is genuinely blocking rather than incidental.
#
# Stated as coverage rather than as a `match_score` threshold. The old `>= 60` was a cut on
# a weighted mean, so removing BM25 and reweighting would have silently changed what every
# skill-gap chart means -- and re-tuning 60 to some other number would only have hidden
# that. Coverage with a minimum denominator is weight-independent and says what it checks:
# the posting named at least four skills we recognise, and the candidate can evidence half.
GOOD_FIT_COVERAGE = 0.5
GOOD_FIT_MIN_REQUIREMENTS = 4

# Effort ratings temper the ranking: a high-demand skill that takes months to acquire should
# not automatically outrank a comparable one that takes a weekend.
EFFORT_MULTIPLIER = {"low": 1.15, "medium": 1.0, "high": 0.8}


def _is_good_fit(posting: dict[str, Any]) -> bool:
    """Did the candidate cover enough of what this posting actually named?

    Falls back to counting `matched_skills` when the denormalized counts are absent, so
    rows written before migration v6 still classify instead of silently dropping out of the
    good-fit stratum and shrinking it.
    """
    required = posting.get("required_count")
    matched = posting.get("matched_count")
    if required is None or matched is None:
        skills = posting.get("skills") or {}
        matched_list = posting.get("matched_skills") or []
        if isinstance(matched_list, str):
            try:
                matched_list = json.loads(matched_list)
            except ValueError:
                matched_list = []
        required, matched = len(skills), len(matched_list)
    if not required or required < GOOD_FIT_MIN_REQUIREMENTS:
        return False
    return (matched / required) >= GOOD_FIT_COVERAGE


class GapAnalysis:
    def __init__(
        self,
        db: "Database",
        config: dict[str, Any] | None,
        roles: "RoleTaxonomy",
        taxonomy: "Taxonomy",
        profile: "ProfileAdapter",
    ) -> None:
        self.db = db
        self.config = config or {}
        self.analytics_config = self.config.get("analytics", {}) or {}
        self.roles = roles
        self.taxonomy = taxonomy
        self.profile = profile

    # -- corpus loading ---------------------------------------------------------------
    def _load_corpus(
        self,
        window_days: int,
        location_id: str | None = None,
        role_family: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[int, dict[str, bool]]]:
        """Postings eligible for SKILL statistics.

        Uses v_skill_eligible, which requires desc_selection='census'. LinkedIn descriptions
        are fetched only for a top-scoring subset, and that subset is selected by a score
        correlated with the user's own skills -- pooling them would inflate demand for
        skills they already have, biasing the gap analysis in the one direction that makes
        it useless.
        """
        window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
        query = "SELECT * FROM v_skill_eligible WHERE date_found >= ?"
        params = [window_start.isoformat()]
        if self.analytics_config.get("exclude_agencies", True):
            query += " AND COALESCE(is_agency, 0) = 0"
        if location_id:
            query += " AND scrape_cell_id IN (SELECT id FROM scrape_cells WHERE location_id = ?)"
            params.append(location_id)
        if role_family:
            query += " AND role_family = ?"
            params.append(role_family)

        postings = [dict(row) for row in self.db.conn.execute(query, params)]
        if not postings:
            return [], {}

        ids = [p["id"] for p in postings]
        skills_by_job = {}
        # Chunked to stay under SQLite's variable limit.
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.db.conn.execute(
                f"SELECT job_id, skill, in_title FROM job_skills WHERE job_id IN ({placeholders})",
                chunk,
            ):
                skills_by_job.setdefault(row["job_id"], {})[row["skill"]] = bool(row["in_title"])
        return postings, skills_by_job

    def _cell_locations(self) -> dict[int, str]:
        return {
            row["id"]: row["location_id"]
            for row in self.db.conn.execute("SELECT id, location_id FROM scrape_cells")
        }

    # -- main entry point --------------------------------------------------------------
    def analyse(
        self,
        window_days: int = 90,
        location_id: str | None = None,
        role_family: str | None = None,
        weighting_mode: str | None = None,
    ) -> dict[str, Any]:
        postings, skills_by_job = self._load_corpus(window_days, location_id, role_family)
        mode = weighting_mode or self.analytics_config.get("weighting_mode", "interest")

        if not postings:
            return self._empty_result(window_days, mode, "no_eligible_postings")

        cell_locations = self._cell_locations()
        reference_mix = self.analytics_config.get("reference_mix") or {}

        # Stratum = (role_family, location). Rotation changes which strata are sampled, so
        # weights rake the observed mix toward the declared one.
        stratum_of = {}
        stratum_counts = {}
        for posting in postings:
            cell_id = posting.get("scrape_cell_id")
            location = (cell_locations.get(cell_id) if cell_id is not None else None) or "unknown"
            key = f"{posting.get('role_family')}|{location}"
            stratum_of[posting["id"]] = key
            stratum_counts[key] = stratum_counts.get(key, 0) + 1

        if mode == "observed":
            weights_by_stratum = dict.fromkeys(stratum_counts, 1.0)
            diagnostics = {
                "strata_used": len(stratum_counts),
                "strata_available": len(stratum_counts),
                "n_used": len(postings),
                "missing_weight": 0.0,
            }
        else:
            weights_by_stratum, diagnostics = build_stratum_weights(
                stratum_counts,
                reference_mix,
                min_stratum_n=self.analytics_config.get("min_stratum_n", 10),
            )

        weight_of = {p["id"]: weights_by_stratum.get(stratum_of[p["id"]], 0.0) for p in postings}
        active = [p for p in postings if weight_of[p["id"]] > 0]

        # Until the rotation has covered enough of the target mix, post-stratification
        # discards most of the corpus and every skill gets suppressed as
        # coverage_incomplete -- technically correct but an empty tab, which is how a
        # feature gets abandoned before it has data. So fall back to unweighted and label
        # it loudly. As more cells are scraped this reverts to the weighted estimate on its
        # own, and the fallback_reason disappears from the payload.
        fallback_reason = None
        max_missing = self.analytics_config.get("max_missing_weight", 0.30)
        if mode != "observed" and (not active or diagnostics["missing_weight"] > max_missing):
            fallback_reason = (
                f"target mix only {(1 - diagnostics['missing_weight']) * 100:.0f}% "
                f"covered ({diagnostics['strata_used']} of "
                f"{diagnostics['strata_available']} strata) — showing UNWEIGHTED figures, "
                f"which reflect the scrape rotation rather than the market"
            )
            active = postings
            weight_of = {p["id"]: 1.0 for p in postings}
            diagnostics = dict(diagnostics, missing_weight=0.0, weighting_fallback=True)

        total_weight = sum(weight_of[p["id"]] for p in active)
        n_eff_total = kish_n_eff([weight_of[p["id"]] for p in active])

        good_fit = [p for p in active if _is_good_fit(p)]
        good_fit_weight = sum(weight_of[p["id"]] for p in good_fit)

        stats = self._per_skill_stats(
            active, good_fit, skills_by_job, weight_of, total_weight, good_fit_weight
        )
        rows = self._score_rows(stats, n_eff_total, diagnostics)

        return {
            "window_days": window_days,
            "location_id": location_id,
            "role_family": role_family,
            "weighting_mode": mode,
            "weighting_effective": "observed" if fallback_reason else mode,
            "weighting_fallback_reason": fallback_reason,
            "rows": rows,
            "views": self._views(rows),
            "provenance": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "n_postings": len(active),
                "n_eff": round(n_eff_total, 1),
                "n_good_fit": len(good_fit),
                "good_fit_criterion": (
                    f"coverage >= {GOOD_FIT_COVERAGE} over >= "
                    f"{GOOD_FIT_MIN_REQUIREMENTS} recognised requirements"
                ),
                "missing_weight": diagnostics["missing_weight"],
                "strata_used": diagnostics["strata_used"],
                "strata_available": diagnostics["strata_available"],
                "taxonomy_hash": self.taxonomy.hash,
                "roles_hash": self.roles.hash,
                "window_below_minimum": window_days
                < self.analytics_config.get("min_window_days", 30),
                # Post-stratification cannot fix within-stratum selection bias: if a cell
                # saturated and the board ranks larger employers first, demand inside that
                # stratum skews toward big-company stacks. Surfaced, not corrected.
                "residual_bias_note": (
                    "Within-stratum selection bias is not corrected. Check "
                    "saturated_share and max_company_share per skill."
                ),
                "cold_start": n_eff_total < self.analytics_config.get("min_n_eff_for_scope", 20),
            },
        }

    def _per_skill_stats(
        self,
        active: list[dict[str, Any]],
        good_fit: list[dict[str, Any]],
        skills_by_job: dict[int, dict[str, bool]],
        weight_of: dict[int, float],
        total_weight: float,
        good_fit_weight: float,
    ) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}

        def record(skill: str) -> dict[str, Any]:
            return stats.setdefault(
                skill,
                {
                    "weighted_count": 0.0,
                    "raw_count": 0,
                    "weights": [],
                    "companies": {},
                    "in_title": 0,
                    "saturated": 0,
                    "blocking_weight": 0.0,
                    "salaries": [],
                    "cooccurring": {},
                    "familiar_share_sum": 0.0,
                    "familiar_share_n": 0,
                },
            )

        user_skills = self.profile.keys()

        for posting in active:
            job_skills = skills_by_job.get(posting["id"], {})
            weight = weight_of[posting["id"]]
            company = posting.get("company_normalized") or "?"
            salary = posting.get("salary_annual_usd")

            for skill, in_title in job_skills.items():
                entry = record(skill)
                entry["weighted_count"] += weight
                entry["raw_count"] += 1
                entry["weights"].append(weight)
                entry["companies"][company] = entry["companies"].get(company, 0) + 1
                if in_title:
                    entry["in_title"] += 1
                if salary:
                    entry["salaries"].append((salary, weight))

                # Adjacency = when this skill appears, how much of the REST of that
                # posting's stack does the user already know? Averaged over postings, this
                # is the learnability signal: a skill surrounded by familiar technology is
                # easier to reach than one embedded in an unfamiliar stack.
                #
                # An earlier version counted DISTINCT co-occurring user skills divided by
                # the posting count, which exceeded 1.0 for almost every skill and clamped
                # to a flat 1.00 -- contributing an identical constant to every candidate
                # and letting the effort multiplier decide the ranking. That put dbt
                # (blocking_gap 0.01, n=24) above on-call (blocking_gap 0.24, n=271).
                others = [s for s in job_skills if s != skill]
                if others:
                    known = sum(1 for s in others if s in user_skills)
                    entry["familiar_share_sum"] += known / len(others)
                    entry["familiar_share_n"] += 1
                for other in others:
                    if other in user_skills:
                        entry["cooccurring"][other] = entry["cooccurring"].get(other, 0) + 1

        # blocking_gap: among postings the user already matches well, how often does this
        # missing skill appear? This is the "what should I learn next" signal.
        for posting in good_fit:
            weight = weight_of[posting["id"]]
            for skill in skills_by_job.get(posting["id"], {}):
                if skill not in user_skills:
                    record(skill)["blocking_weight"] += weight

        baseline_salary = weighted_median(
            [(salary, weight_of[p["id"]]) for p in active if (salary := p.get("salary_annual_usd"))]
        )

        for entry in stats.values():
            entry["total_weight"] = total_weight
            entry["good_fit_weight"] = good_fit_weight
            entry["baseline_salary"] = baseline_salary
        return stats

    def _score_rows(
        self,
        stats: dict[str, dict[str, Any]],
        n_eff_total: float,  # noqa: ARG002 - kept for signature parity with the other row scorers
        diagnostics: dict[str, Any],
    ) -> list[dict[str, Any]]:
        min_postings = self.analytics_config.get("min_postings_for_skill", 20)
        min_companies = self.analytics_config.get("min_companies_for_skill", 3)
        max_company_share = self.analytics_config.get("max_company_share", 0.40)
        min_salary_samples = self.analytics_config.get("min_salary_samples", 12)
        gap_weights = self.analytics_config.get("gap_weights") or {}

        rows = []
        for skill, entry in stats.items():
            total_weight = entry["total_weight"] or 1.0
            demand = entry["weighted_count"] / total_weight
            n_eff = kish_n_eff(entry["weights"])
            _, ci_low, ci_high = wilson_interval(demand * max(n_eff, 1), max(n_eff, 1))

            company_total = sum(entry["companies"].values()) or 1
            top_company_share = (
                max(entry["companies"].values()) / company_total if entry["companies"] else 1.0
            )

            blocking_gap = (
                entry["blocking_weight"] / entry["good_fit_weight"]
                if entry["good_fit_weight"]
                else 0.0
            )
            adjacency = (
                entry["familiar_share_sum"] / entry["familiar_share_n"]
                if entry["familiar_share_n"]
                else 0.0
            )

            salary_lift = None
            if len(entry["salaries"]) >= min_salary_samples and entry["baseline_salary"]:
                skill_median = weighted_median(entry["salaries"])
                if skill_median:
                    salary_lift = skill_median / entry["baseline_salary"]

            suppressed = None
            if diagnostics["missing_weight"] > 0.30:
                suppressed = SUPPRESS_COVERAGE_INCOMPLETE
            elif entry["raw_count"] < min_postings:
                suppressed = SUPPRESS_SMALL_SAMPLE
            elif len(entry["companies"]) < min_companies:
                suppressed = SUPPRESS_TOO_FEW_COMPANIES
            elif top_company_share > max_company_share:
                suppressed = SUPPRESS_COMPANY_CONCENTRATION

            user_has = self.profile.has(skill)
            skill_meta = self.taxonomy.get(skill)
            effort = skill_meta.effort if skill_meta else "medium"

            rows.append(
                {
                    "skill": skill,
                    "label": self.taxonomy.label(skill),
                    "category": self.taxonomy.category(skill),
                    "user_has": user_has,
                    "user_level": self.profile.level(skill),
                    "effort": effort,
                    "demand": round(demand, 4),
                    "demand_ci_low": round(ci_low, 4),
                    "demand_ci_high": round(ci_high, 4),
                    "blocking_gap": round(blocking_gap, 4),
                    "adjacency": round(adjacency, 4),
                    "salary_lift": round(salary_lift, 3) if salary_lift else None,
                    "priority": None,
                    "n_raw": entry["raw_count"],
                    "n_eff": round(n_eff, 1),
                    "n_companies": len(entry["companies"]),
                    "max_company_share": round(top_company_share, 3),
                    "in_title_count": entry["in_title"],
                    "suppressed_reason": suppressed,
                }
            )

        for row in rows:
            row["priority"] = self._priority(row, gap_weights)
        rows.sort(key=lambda r: r["priority"] or 0, reverse=True)
        return rows

    def _priority(self, row: dict[str, Any], gap_weights: dict[str, float]) -> float | None:
        """Composite acquisition priority, renormalized over available terms.

        A missing salary_lift must not be treated as zero -- that would silently penalize
        every skill in a market where salary is rarely disclosed. It is dropped from the
        weighted mean and the remaining weights are rescaled.
        """
        if row["user_has"] or row["suppressed_reason"]:
            return None

        terms = {
            "blocking_gap": row["blocking_gap"],
            "demand": row["demand"],
            "adjacency": row["adjacency"],
        }
        if row["salary_lift"] is not None:
            terms["salary_lift"] = max(0.0, min(1.0, row["salary_lift"] - 1.0))

        weights = {k: gap_weights.get(k, 0.0) for k in terms}
        total = sum(weights.values())
        if total <= 0:
            return None

        score = sum(terms[k] * weights[k] / total for k in terms)
        score *= EFFORT_MULTIPLIER.get(row["effort"], 1.0)
        return round(min(1.0, score), 4)

    def _views(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """The three answers the user asked for."""
        published = [r for r in rows if not r["suppressed_reason"]]

        strengths = sorted(
            [r for r in published if r["user_has"] and r["demand"] >= 0.10],
            key=lambda r: -r["demand"],
        )
        gaps = [r for r in published if not r["user_has"] and r["priority"]]
        # Skills on the resume that essentially nobody asks for -- resume space with no
        # market return.
        dead_weight = sorted(
            [r for r in published if r["user_has"] and r["demand"] < 0.02],
            key=lambda r: r["demand"],
        )
        return {
            "validated_strengths": strengths[:30],
            "priority_gaps": gaps[:30],
            "dead_weight": dead_weight[:20],
            "suppressed": [
                {
                    "skill": r["skill"],
                    "label": r["label"],
                    "reason": r["suppressed_reason"],
                    "n_raw": r["n_raw"],
                }
                for r in rows
                if r["suppressed_reason"]
            ],
        }

    def _empty_result(self, window_days: int, mode: str, reason: str) -> dict[str, Any]:
        return {
            "window_days": window_days,
            "weighting_mode": mode,
            "rows": [],
            "views": {
                "validated_strengths": [],
                "priority_gaps": [],
                "dead_weight": [],
                "suppressed": [],
            },
            "provenance": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "n_postings": 0,
                "n_eff": 0.0,
                "cold_start": True,
                "suppressed_reason": reason,
                "taxonomy_hash": self.taxonomy.hash,
            },
        }

    # -- drill-down --------------------------------------------------------------------
    def skill_detail(self, skill: str, window_days: int = 90, limit: int = 40) -> dict[str, Any]:
        """Everything behind one skill's numbers, so a ranking can be audited."""
        window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
        postings = [
            dict(row)
            for row in self.db.conn.execute(
                """
                SELECT j.id, j.title, j.company, j.location, j.url, j.match_score,
                       j.role_family, j.seniority, j.salary_annual_usd, j.date_posted,
                       js.in_title
                  FROM v_skill_eligible j
                  JOIN job_skills js ON js.job_id = j.id
                 WHERE js.skill = ? AND j.date_found >= ?
                 ORDER BY j.match_score DESC
                 LIMIT ?
                """,
                (skill, window_start.isoformat(), limit),
            )
        ]

        cooccurring = self.db.conn.execute(
            """
            SELECT other.skill, COUNT(*) n
              FROM job_skills js
              JOIN job_skills other ON other.job_id = js.job_id AND other.skill != js.skill
              JOIN v_skill_eligible j ON j.id = js.job_id
             WHERE js.skill = ? AND j.date_found >= ?
             GROUP BY other.skill
             ORDER BY n DESC
             LIMIT 15
            """,
            (skill, window_start.isoformat()),
        ).fetchall()

        by_family = self.db.conn.execute(
            """
            SELECT j.role_family, COUNT(*) n
              FROM job_skills js JOIN v_skill_eligible j ON j.id = js.job_id
             WHERE js.skill = ? AND j.date_found >= ?
             GROUP BY j.role_family ORDER BY n DESC
            """,
            (skill, window_start.isoformat()),
        ).fetchall()

        return {
            "skill": skill,
            "label": self.taxonomy.label(skill),
            "category": self.taxonomy.category(skill),
            "user_has": self.profile.has(skill),
            "user_level": self.profile.level(skill),
            "evidence": self.profile.evidence(skill),
            "window_days": window_days,
            "postings": postings,
            "cooccurring": [
                {
                    "skill": r["skill"],
                    "label": self.taxonomy.label(r["skill"]),
                    "n": r["n"],
                    "user_has": self.profile.has(r["skill"]),
                }
                for r in cooccurring
            ],
            "by_role_family": [
                {
                    "role_family": r["role_family"],
                    "label": self.roles.label(r["role_family"]),
                    "n": r["n"],
                }
                for r in by_family
                if r["role_family"]
            ],
        }
