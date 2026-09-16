"""Skill demand and gap analysis.

Answers the two questions the dashboard exists for: which in-demand skills does the user
already have, and which missing ones are worth acquiring next.

`blocking_gap` is the centrepiece and the reason this is more useful than a popularity
chart: it measures how often a skill the user lacks appears in postings they *otherwise*
match well. Kubernetes being popular is not actionable; Kubernetes being the one thing
standing between the user and 40% of the targets they'd otherwise be a strong fit for is.

Estimates are plain unweighted counts over the eligible corpus. Every suppressed figure
carries a reason rather than silently vanishing.
"""

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from careerradar.market import repository as market_repo
from careerradar.market.analytics import (
    SUPPRESS_COMPANY_CONCENTRATION,
    SUPPRESS_SMALL_SAMPLE,
    SUPPRESS_TOO_FEW_COMPANIES,
    wilson_interval,
)

if TYPE_CHECKING:
    from careerradar.core.database import Database
    from careerradar.profile.adapter import ProfileAdapter
    from careerradar.search.targets import SearchTargets

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
        targets: "SearchTargets",
        profile: "ProfileAdapter | None" = None,
    ) -> None:
        self.db = db
        self.config = config or {}
        self.analytics_config = self.config.get("analytics", {}) or {}
        self.targets = targets
        self.profile = profile or self._empty_profile()

    def _empty_profile(self) -> Any:
        class _StubProfile:
            def __init__(self) -> None:
                self.skills: dict[str, Any] = {}

            def has(self, key: str) -> bool:  # noqa: ARG002
                return False

            def level(self, key: str) -> int:  # noqa: ARG002
                return 0

            def keys(self) -> set[str]:
                return set()

            def evidence(self, key: str) -> list[str]:  # noqa: ARG002
                return []

        return _StubProfile()

    def _load_corpus(
        self,
        window_days: int,
        location_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[int, dict[str, bool]]]:
        """Postings eligible for SKILL statistics.

        Uses v_skill_eligible, which requires desc_selection='census'. LinkedIn descriptions
        are fetched only for a top-scoring subset, and that subset is selected by a score
        correlated with the user's own skills -- pooling them would inflate demand for
        skills they already have, biasing the gap analysis in the one direction that makes
        it useless.
        """
        window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
        postings = market_repo.load_gap_analysis_corpus(
            self.db.conn,
            eligibility_sql="SELECT * FROM v_skill_eligible",
            window_start=window_start.isoformat(),
            location_id=location_id,
            query=query,
            exclude_agencies=self.analytics_config.get("exclude_agencies", True),
        )
        if not postings:
            return [], {}

        ids = [p["id"] for p in postings]
        skills_by_job = market_repo.load_job_skills_chunked(self.db.conn, ids)
        return postings, skills_by_job

    def analyse(
        self,
        window_days: int = 90,
        location_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        postings, skills_by_job = self._load_corpus(window_days, location_id, query)

        if not postings:
            return self._empty_result(window_days, "no_eligible_postings")

        good_fit = [p for p in postings if _is_good_fit(p)]
        stats = self._per_skill_stats(postings, good_fit, skills_by_job)
        rows = self._score_rows(stats)

        return {
            "window_days": window_days,
            "location_id": location_id,
            "query": query,
            "rows": rows,
            "views": self._views(rows),
            "provenance": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "n_postings": len(postings),
                "n_good_fit": len(good_fit),
                "good_fit_criterion": (
                    f"coverage >= {GOOD_FIT_COVERAGE} over >= "
                    f"{GOOD_FIT_MIN_REQUIREMENTS} recognised requirements"
                ),
                "search_targets_hash": self.targets.fingerprint,
                "window_below_minimum": window_days
                < self.analytics_config.get("min_window_days", 30),
                # Counts reflect what the rotation scraped, not the market. A cell that
                # saturated on a board that ranks larger employers first skews demand
                # toward big-company stacks. Surfaced, not corrected.
                "residual_bias_note": (
                    "Counts reflect the scrape rotation, not the market. Check "
                    "saturated_share and max_company_share per skill."
                ),
                "cold_start": len(postings)
                < self.analytics_config.get("min_postings_for_scope", 20),
            },
        }

    def _per_skill_stats(
        self,
        active: list[dict[str, Any]],
        good_fit: list[dict[str, Any]],
        skills_by_job: dict[int, dict[str, bool]],
    ) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}

        def record(skill: str) -> dict[str, Any]:
            return stats.setdefault(
                skill,
                {
                    "raw_count": 0,
                    "companies": {},
                    "in_title": 0,
                    "saturated": 0,
                    "blocking_count": 0,
                    "salaries": [],
                    "cooccurring": {},
                    "familiar_share_sum": 0.0,
                    "familiar_share_n": 0,
                },
            )

        user_skills = self.profile.keys()

        for posting in active:
            job_skills = skills_by_job.get(posting["id"], {})
            company = posting.get("company_normalized") or "?"
            salary = posting.get("salary_annual_usd")

            for skill, in_title in job_skills.items():
                entry = record(skill)
                entry["raw_count"] += 1
                entry["companies"][company] = entry["companies"].get(company, 0) + 1
                if in_title:
                    entry["in_title"] += 1
                if salary:
                    entry["salaries"].append(salary)

                # Adjacency = when this skill appears, how much of the REST of that
                # posting's stack does the user already know? Averaged over postings, this
                # is the learnability signal: a skill surrounded by familiar technology is
                # easier to reach than one embedded in an unfamiliar stack.
                #
                # An earlier version counted DISTINCT co-occurring user skills divided by
                # the posting count, which exceeded 1.0 for almost every skill and clamped
                # to a flat 1.00 -- contributing an identical constant to every candidate
                # and letting the remaining terms decide the ranking. That put dbt
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
            for skill in skills_by_job.get(posting["id"], {}):
                if skill not in user_skills:
                    record(skill)["blocking_count"] += 1

        salaries = [s for p in active if (s := p.get("salary_annual_usd"))]
        baseline_salary = statistics.median(salaries) if salaries else None

        for entry in stats.values():
            entry["n_active"] = len(active)
            entry["n_good_fit"] = len(good_fit)
            entry["baseline_salary"] = baseline_salary
        return stats

    def _score_rows(self, stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        min_postings = self.analytics_config.get("min_postings_for_skill", 20)
        min_companies = self.analytics_config.get("min_companies_for_skill", 3)
        max_company_share = self.analytics_config.get("max_company_share", 0.40)
        min_salary_samples = self.analytics_config.get("min_salary_samples", 12)
        gap_weights = self.analytics_config.get("gap_weights") or {}

        rows = []
        for skill, entry in stats.items():
            n = entry["raw_count"]
            demand = n / (entry["n_active"] or 1)
            _, ci_low, ci_high = wilson_interval(n, entry["n_active"] or 1)

            company_total = sum(entry["companies"].values()) or 1
            top_company_share = (
                max(entry["companies"].values()) / company_total if entry["companies"] else 1.0
            )

            blocking_gap = (
                entry["blocking_count"] / entry["n_good_fit"] if entry["n_good_fit"] else 0.0
            )
            adjacency = (
                entry["familiar_share_sum"] / entry["familiar_share_n"]
                if entry["familiar_share_n"]
                else 0.0
            )

            salary_lift = None
            if len(entry["salaries"]) >= min_salary_samples and entry["baseline_salary"]:
                skill_median = statistics.median(entry["salaries"])
                if skill_median:
                    salary_lift = skill_median / entry["baseline_salary"]

            suppressed = None
            if n < min_postings:
                suppressed = SUPPRESS_SMALL_SAMPLE
            elif len(entry["companies"]) < min_companies:
                suppressed = SUPPRESS_TOO_FEW_COMPANIES
            elif top_company_share > max_company_share:
                suppressed = SUPPRESS_COMPANY_CONCENTRATION

            user_has = self.profile.has(skill)

            if self.profile.has(skill):
                label = self.profile.skills[skill]["label"]
                category = self.profile.skills[skill].get("category") or "other"
            else:
                label = skill.replace("_", " ").title()
                category = "other"

            rows.append(
                {
                    "skill": skill,
                    "label": label,
                    "category": category,
                    "user_has": user_has,
                    "user_level": self.profile.level(skill),
                    "demand": round(demand, 4),
                    "demand_ci_low": round(ci_low, 4),
                    "demand_ci_high": round(ci_high, 4),
                    "blocking_gap": round(blocking_gap, 4),
                    "adjacency": round(adjacency, 4),
                    "salary_lift": round(salary_lift, 3) if salary_lift else None,
                    "priority": None,
                    "n_raw": n,
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

    def _empty_result(self, window_days: int, reason: str) -> dict[str, Any]:
        return {
            "window_days": window_days,
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
                "cold_start": True,
                "suppressed_reason": reason,
            },
        }

    def skill_detail(self, skill: str, window_days: int = 90, limit: int = 40) -> dict[str, Any]:
        """Everything behind one skill's numbers, so a ranking can be audited."""
        window_start = datetime.now(timezone.utc) - timedelta(days=window_days)
        postings = market_repo.get_skill_drilldown_postings(
            self.db.conn,
            skill=skill,
            window_start=window_start.isoformat(),
            limit=limit,
        )
        cooccurring = market_repo.get_skill_cooccurring(
            self.db.conn,
            skill=skill,
            window_start=window_start.isoformat(),
            limit=15,
        )
        by_query = market_repo.get_skill_by_query(
            self.db.conn,
            skill=skill,
            window_start=window_start.isoformat(),
        )

        if self.profile.has(skill):
            label = self.profile.skills[skill]["label"]
            category = self.profile.skills[skill].get("category") or "other"
        else:
            label = skill.replace("_", " ").title()
            category = "other"

        cooccurring_items = []
        for r in cooccurring:
            c_skill = r["skill"]
            if self.profile.has(c_skill):
                c_label = self.profile.skills[c_skill]["label"]
            else:
                c_label = c_skill.replace("_", " ").title()
            cooccurring_items.append(
                {
                    "skill": c_skill,
                    "label": c_label,
                    "n": r["n"],
                    "user_has": self.profile.has(c_skill),
                }
            )

        return {
            "skill": skill,
            "label": label,
            "category": category,
            "user_has": self.profile.has(skill),
            "user_level": self.profile.level(skill),
            "evidence": self.profile.evidence(skill),
            "window_days": window_days,
            "postings": postings,
            "cooccurring": cooccurring_items,
            "by_query": [{"query": r["query"], "n": r["n"]} for r in by_query if r["query"]],
        }
