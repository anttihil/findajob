import copy
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, ClassVar

from careerradar.core.paths import DB_PATH

if TYPE_CHECKING:
    from careerradar.search.scheduler import CellState

# Memoized `get_stats()` results, keyed by database path: (data_version, write_generation) -> stats.
_StatsCacheEntry = tuple[tuple[int, int], dict[str, Any]]

# Memoized `get_stats()` results, keyed by database path. See `Database.get_stats`.
_STATS_CACHE: dict[str, _StatsCacheEntry] = {}

# Memoized `job_ids_for()` results. See that method for why one entry is enough.
_FEED_IDS_CACHE: dict[tuple[Any, ...], list[int]] = {}

# Bumped by writes made on the same connection that later reads stats, which is the one
# case `PRAGMA data_version` cannot see.
_WRITE_GENERATION = 0


def _invalidate_stats() -> None:
    global _WRITE_GENERATION
    _WRITE_GENERATION += 1


def _fuzzy_job_search(
    q_str: str | None,
    title: str | None,
    company: str | None,
    skills: str | None,
    location: str | None,
    role_family: str | None,
    seniority: str | None,
) -> int:
    """Multi-token typo-tolerant fuzzy matching across primary job posting fields."""
    if not q_str or not q_str.strip():
        return 1
    combined = (
        f"{title or ''} {company or ''} {skills or ''} "
        f"{location or ''} {role_family or ''} {seniority or ''}"
    ).lower()
    tokens = [t for t in re.split(r"\s+", q_str.strip().lower()) if t]
    if not tokens:
        return 1
    words = None
    for token in tokens:
        if token in combined:
            continue
        if words is None:
            words = re.findall(r"[a-zA-Z0-9+#.-]+", combined)
        token_len = len(token)
        if token_len < 3:
            return 0
        max_dist = 1 if token_len <= 5 else 2
        matched = False
        for w in words:
            if abs(len(w) - token_len) <= max_dist and len(set(token) - set(w)) <= max_dist:
                ratio = SequenceMatcher(None, token, w).ratio()
                if ratio >= (0.75 if token_len <= 5 else 0.8):
                    matched = True
                    break
        if not matched:
            return 0
    return 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DB_PATH
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("fuzzy_search", 7, _fuzzy_job_search)
        # Filled on first use by `_select_columns`, which reads it from PRAGMA table_info
        # rather than repeating the `jobs` column list in Python.
        self._jobs_columns: list[str] | None = None
        self.create_tables()

    def create_tables(self) -> None:
        """Bring the schema up to date via the versioned migration runner.

        Migrations are idempotent, so calling this on every connection is cheap: once
        user_version matches, it is a single PRAGMA read.
        """
        from careerradar.core.migrations import migrate

        migrate(self.conn)

    # The liveness rule, inlined rather than joined from `v_job_liveness`.
    #
    # The view is defined over the whole of `jobs`, and SQLite materializes it before it
    # can be joined -- a 5,932-row scan to attach one string to one row already found by
    # primary key. Because the view also appeared inside the `SELECT COUNT(*) FROM (...)`
    # wrapper below, a single drawer page built it five times. Measured on the live
    # database, the by-id lookup went from 18.10ms to 0.02ms once this became a plain join
    # against `scrape_cells`.
    #
    # The view itself stays in the schema for other readers; this is the same CASE, kept
    # deliberately identical to `migrations._v12_liveness_window`. If one changes, so must
    # the other.
    #
    # The nested CASE is the window check: `hours_old` filters the board's search on
    # date_posted, so a posting older than the cell's window cannot come back whether it is
    # open or closed, and its absence proves nothing. See _v12_liveness_window for the
    # measurement that motivated it.
    _LIVENESS_CASE = """
        CASE
          WHEN jobs.last_seen_at IS NULL OR cell.last_success_at IS NULL THEN 'unknown'
          WHEN unixepoch(cell.last_success_at) - unixepoch(jobs.last_seen_at) > 43200 THEN
               CASE
                 WHEN jobs.date_posted IS NOT NULL
                      AND (unixepoch(cell.last_success_at)
                           - unixepoch(jobs.date_posted))
                          <= COALESCE(cell.last_hours_old, 0) * 3600
                      THEN 'likely_closed'
                 ELSE 'unknown'
               END
          WHEN unixepoch('now') - unixepoch(cell.last_success_at) > 604800 THEN 'stale'
          ELSE 'live'
        END"""

    _FROM = """
          FROM jobs
          LEFT JOIN job_verdicts v
                 ON v.job_id = jobs.id
                AND v.profile_version = (
                    SELECT version FROM profiles WHERE is_active = 1
                )
          LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id
    """

    # What a job card and drawer read from job_verdicts.
    _VERDICT_LIST_COLUMNS = (
        "fit",
        "reason_type",
        "reason_description",
    )
    _VERDICT_DETAIL_COLUMNS = _VERDICT_LIST_COLUMNS

    # Only the drawer renders the posting text, and at ~6KB a row it was 300KB of every
    # 50-card page. Named as an exclusion and applied against `PRAGMA table_info` so that a
    # column added to `jobs` tomorrow still reaches the feed without editing a list here.
    _LIST_OMITTED_JOB_COLUMNS = frozenset({"description"})

    _JSON_COLUMNS = ("matched_skills",)

    DATE_POSTED_WINDOWS: ClassVar[dict[str, float]] = {
        "24h": 1.0,
        "3d": 3.0,
        "7d": 7.0,
        "14d": 14.0,
        "30d": 30.0,
    }

    _FEED_ORDER_BY = (
        "v.fit DESC NULLS LAST, "
        "COALESCE(jobs.date_posted, jobs.date_found) DESC, "
        "jobs.date_found DESC"
    )

    def _select_columns(self, detail: bool) -> str:
        """The SELECT list, minus what the caller will not read."""
        if self._jobs_columns is None:
            self._jobs_columns = [row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")]
        omitted = frozenset() if detail else self._LIST_OMITTED_JOB_COLUMNS
        job_columns = [f"jobs.{name}" for name in self._jobs_columns if name not in omitted]
        verdict_columns = [
            f"v.{name} AS {name}"
            for name in (self._VERDICT_DETAIL_COLUMNS if detail else self._VERDICT_LIST_COLUMNS)
        ]
        return ",\n                   ".join(
            job_columns + verdict_columns + [f"{self._LIVENESS_CASE} AS liveness"]
        )

    def _feed_filters(
        self,
        *,
        status: str | None,
        country: str | None,
        role_family: str | None,
        seniority: str | None,
        source: str | None,
        is_remote: bool | None,
        has_salary: bool | None,
        access: str | None,
        include_duplicates: bool,
        min_score: int | None,
        pipeline_state: str | None,
        liveness: str | None,
        job_id: int | None,
        fit: bool | None = None,
        reason_type: str | None = None,
        date_posted: str | None = None,
        q: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The shared WHERE clause, as (sql, params).

        Extracted so that `query_jobs` and `job_ids_for` cannot disagree about what the
        current feed contains -- the drawer's "next posting" has to be the next one the
        feed would actually show.
        """
        sql = ""
        params: list[Any] = []

        # Fetching one posting reuses this method so that the drawer sees exactly the row
        # shape the feed does -- the same verdict join, the same JSON decoding, the same
        # requirement summary. A second bespoke query is how the two drifted apart before.
        if job_id is not None:
            sql += " AND jobs.id = ?"
            params.append(job_id)
        if not include_duplicates:
            sql += " AND duplicate_of IS NULL"
        for column, value in (
            ("jobs.status", status),
            ("jobs.country", country),
            ("jobs.role_family", role_family),
            ("jobs.seniority", seniority),
            ("jobs.source", source),
            ("jobs.access", access),
            ("jobs.pipeline_state", pipeline_state),
            (self._LIVENESS_CASE, liveness),
        ):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        if fit is not None:
            sql += " AND v.fit = ?"
            params.append(1 if fit else 0)
        if reason_type:
            sql += " AND v.reason_type = ?"
            params.append(reason_type.strip().lower())
        if date_posted and date_posted in self.DATE_POSTED_WINDOWS:
            hours = self.DATE_POSTED_WINDOWS[date_posted] * 24
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            sql += " AND COALESCE(jobs.date_posted, jobs.date_found) >= ?"
            params.append(cutoff)
        if is_remote is not None:
            sql += " AND jobs.is_remote = ?"
            params.append(1 if is_remote else 0)
        if has_salary is not None:
            sql += (
                " AND jobs.salary_annual_usd IS NOT NULL"
                if has_salary
                else " AND jobs.salary_annual_usd IS NULL"
            )
        if min_score is not None:
            sql += " AND jobs.match_score >= ?"
            params.append(min_score)
        if q and q.strip():
            sql += (
                " AND fuzzy_search(?, jobs.title, jobs.company, jobs.matched_skills,"
                " jobs.location, jobs.role_family, jobs.seniority) = 1"
            )
            params.append(q.strip())
        return sql, params

    def query_jobs(
        self,
        status: str | None = None,
        country: str | None = None,
        role_family: str | None = None,
        seniority: str | None = None,
        source: str | None = None,
        is_remote: bool | None = None,
        has_salary: bool | None = None,
        access: str | None = None,
        include_duplicates: bool = False,
        min_score: int | None = None,
        pipeline_state: str | None = None,
        liveness: str | None = None,
        job_id: int | None = None,
        fit: bool | None = None,
        reason_type: str | None = None,
        date_posted: str | None = None,
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Filtered, paginated posting list for the dashboard.

        Duplicates are hidden by default: the same requisition cross-posted to both boards
        would otherwise appear twice in the feed.

        The verdict fields live in `job_verdicts`, one row per (posting, profile version).
        Joining against the *active* profile rather than the latest verdict means a profile
        rebuild does not retroactively rewrite what the dashboard shows until the backlog
        has actually been re-scored under it.

        `detail` selects the columns only the drawer renders -- the posting text above all.
        Fetching by `job_id` implies it, so no caller has to remember the pairing.
        """
        if job_id is not None:
            detail = True

        where, params = self._feed_filters(
            status=status,
            country=country,
            role_family=role_family,
            seniority=seniority,
            source=source,
            is_remote=is_remote,
            has_salary=has_salary,
            access=access,
            include_duplicates=include_duplicates,
            min_score=min_score,
            pipeline_state=pipeline_state,
            liveness=liveness,
            job_id=job_id,
            fit=fit,
            reason_type=reason_type,
            date_posted=date_posted,
            q=q,
        )
        query = f"SELECT {self._select_columns(detail)}{self._FROM} WHERE 1=1{where}"

        # A by-id lookup returns at most one row, so counting it is a second full execution
        # of the query to learn something `len()` already knows.
        total = None
        if job_id is None:
            total = self.conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()[0]

        query += f" ORDER BY {self._FEED_ORDER_BY}"
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        jobs: list[dict[str, Any]] = []
        for row in self.conn.execute(query, params):
            job = dict(row)
            for field in self._JSON_COLUMNS:
                if field in job:
                    job[field] = json.loads(job[field]) if job[field] else []
            jobs.append(job)

        if total is None:
            total = len(jobs)

        return {
            "jobs": jobs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(jobs) < total,
        }

    def job_ids_for(
        self,
        status: str | None = None,
        country: str | None = None,
        role_family: str | None = None,
        seniority: str | None = None,
        source: str | None = None,
        is_remote: bool | None = None,
        has_salary: bool | None = None,
        access: str | None = None,
        include_duplicates: bool = False,
        min_score: int | None = None,
        pipeline_state: str | None = None,
        liveness: str | None = None,
        job_id: int | None = None,
        fit: bool | None = None,
        reason_type: str | None = None,
        date_posted: str | None = None,
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
        detail: bool | None = None,  # noqa: ARG002 - signature parity with the sibling feed query
    ) -> list[int]:
        """Just the ids on this page of the feed, in feed order.

        What the drawer needs to answer "which posting comes after this one" without
        fetching a page of rows to find out. Takes the same keyword arguments as
        `query_jobs` -- `FilterQuery.as_db_kwargs()` is passed to both -- and ignores
        `detail`, which means nothing when only the id is selected.
        """
        where, params = self._feed_filters(
            status=status,
            country=country,
            role_family=role_family,
            seniority=seniority,
            source=source,
            is_remote=is_remote,
            has_salary=has_salary,
            access=access,
            include_duplicates=include_duplicates,
            min_score=min_score,
            pipeline_state=pipeline_state,
            liveness=liveness,
            job_id=job_id,
            fit=fit,
            reason_type=reason_type,
            date_posted=date_posted,
            q=q,
        )
        query = (
            f"SELECT jobs.id{self._FROM} WHERE 1=1{where}"
            f" ORDER BY {self._FEED_ORDER_BY}"
            f" LIMIT ? OFFSET ?"
        )
        args: list[Any] = [*params, limit, offset]

        # Ranking the whole eligible corpus to slice 50 ids off the top costs ~12ms, and
        # every drawer opened out of the same feed asks for the same list -- the filter
        # only changes on a navigation. Cached on the query itself, so a different filter
        # is a different entry rather than a stale hit, and invalidated by the same
        # data_version/write-generation pair as `get_stats`.
        key = (
            self.db_path,
            query,
            tuple(args),
            self.conn.execute("PRAGMA data_version").fetchone()[0],
            _WRITE_GENERATION,
        )
        cached = _FEED_IDS_CACHE.get(key)
        if cached is not None:
            return list(cached)

        ids = [row[0] for row in self.conn.execute(query, args)]
        # One filter at a time is the realistic case; the bound just stops a script that
        # sweeps filters from growing this without limit.
        if len(_FEED_IDS_CACHE) > 32:
            _FEED_IDS_CACHE.clear()
        _FEED_IDS_CACHE[key] = ids
        return list(ids)

    def update_job_status(self, job_id: int, status: str) -> bool:
        cursor = self.conn.cursor()
        now_str = datetime.now().isoformat() if status == "applied" else None

        if status == "applied":
            cursor.execute(
                "UPDATE jobs SET status = ?, date_applied = ? WHERE id = ?",
                (status, now_str, job_id),
            )
        else:
            cursor.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.conn.commit()
        # A status change moves a posting between the counters `get_stats` reports, and it
        # is the one write the web process makes on the connection that also reads them --
        # the only case `PRAGMA data_version` will not flag.
        _invalidate_stats()
        return cursor.rowcount > 0

    def get_stats(self) -> dict[str, Any]:
        """Dashboard counters, memoized between writes.

        Ten unindexed GROUP BY scans of `jobs` and `job_verdicts`, ~87ms, recomputed on
        every dashboard render even though nothing it reports changes except when a
        posting is written. Cached per database file and keyed on two things:

        `PRAGMA data_version`, which SQLite bumps whenever *another* connection commits --
        so the scoring worker and the sync runner invalidate this without knowing it
        exists. It deliberately does not change for commits on the connection doing the
        reading, which is why `_WRITE_GENERATION` covers the one write the web process
        makes on its own connection (`update_job_status`).

        The result is deep-copied out so a caller that mutates what it renders cannot
        poison the next request.
        """
        key = (
            self.conn.execute("PRAGMA data_version").fetchone()[0],
            _WRITE_GENERATION,
        )
        cached = _STATS_CACHE.get(self.db_path)
        if cached is not None and cached[0] == key:
            return copy.deepcopy(cached[1])
        stats = self._compute_stats()
        _STATS_CACHE[self.db_path] = (key, stats)
        return copy.deepcopy(stats)

    def status_counts(self) -> dict[str, int]:
        """Postings per status, zero-filled.

        Split out of `get_stats` because the drawer's status action needs exactly this and
        nothing else. Going through `get_stats` for it meant nine other rollups -- ~70ms --
        to put two numbers on screen, on an action taken once per posting triaged.
        """
        counts = {
            row["status"]: row["count"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
            )
        }
        for status in ("unread", "saved", "applied", "rejected"):
            counts.setdefault(status, 0)
        return counts

    def _compute_stats(self) -> dict[str, Any]:
        cursor = self.conn.cursor()
        stats: dict[str, Any] = {}

        # Total counts by status
        stats["status_counts"] = self.status_counts()

        # Total crawled
        cursor.execute("SELECT COUNT(*) FROM jobs")
        stats["total_jobs"] = cursor.fetchone()[0]

        # Coverage, reported with its denominator. `match_score` was never a percent --
        # the coverage prior, an unspecified seniority and an unmapped family put ~23
        # points on the floor, so its observed range was 15-80.
        cursor.execute(
            "SELECT SUM(matched_count), SUM(required_count) FROM jobs WHERE required_count > 0"
        )
        matched, required = cursor.fetchone()
        stats["skill_coverage"] = {
            "matched": matched or 0,
            "required": required or 0,
            "ratio": round(matched / required, 3) if required else None,
        }

        # Counts by country
        cursor.execute("SELECT country, COUNT(*) as count FROM jobs GROUP BY country")
        stats["country_counts"] = {
            r["country"]: r["count"] for r in cursor.fetchall() if r["country"]
        }

        # Where postings sit in the pipeline -- the dashboard's "is the backlog drained?"
        cursor.execute("SELECT pipeline_state, COUNT(*) as count FROM jobs GROUP BY pipeline_state")
        stats["pipeline_counts"] = {r["pipeline_state"]: r["count"] for r in cursor.fetchall()}

        # Ordinal marginals, not an average score. A mean over a projected scale says
        # very little; a collapsed marginal (90% `meets`) is the thing worth catching, and
        # Worth applying to, determined by simplified boolean fit.
        active = "(SELECT version FROM profiles WHERE is_active = 1)"
        cursor.execute(
            f"""
            SELECT COUNT(*) FROM jobs j
              JOIN job_verdicts v ON v.job_id = j.id AND v.profile_version = {active}
             WHERE j.duplicate_of IS NULL
               AND v.fit = 1
            """
        )
        stats["strong_matches"] = cursor.fetchone()[0]

        # Same inlined rule as `_LIVENESS_CASE`, for the same reason: joining the view
        # here materialized it a sixth time per dashboard render.
        cursor.execute(
            f"SELECT {Database._LIVENESS_CASE} AS liveness, COUNT(*)"
            f"  FROM jobs LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id"
            f" GROUP BY 1"
        )
        stats["liveness_counts"] = {r[0]: r[1] for r in cursor.fetchall()}

        return stats

    def get_observability_stats(self) -> dict[str, Any]:
        """Aggregate token counts, costs, and reason distributions across all scored verdicts."""
        cursor = self.conn.cursor()
        has_table = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_verdicts'"
        ).fetchone()
        if not has_table:
            return {
                "total_verdicts": 0,
                "total_tokens_in": 0,
                "total_tokens_cached": 0,
                "total_tokens_out": 0,
                "cache_hit_rate": 0.0,
                "avg_tokens_out": 0.0,
                "min_tokens_out": 0,
                "max_tokens_out": 0,
                "avg_tokens_in": 0.0,
                "total_cost_usd": 0.0,
                "avg_cost_usd": 0.0,
                "fit_count": 0,
                "no_fit_count": 0,
                "fit_rate": 0.0,
                "reasons": [],
                "models": [],
            }

        stats_sql = """
            SELECT
                COUNT(*) AS total_verdicts,
                COALESCE(SUM(v.tokens_in), 0) AS total_tokens_in,
                COALESCE(SUM(v.tokens_cached), 0) AS total_tokens_cached,
                COALESCE(SUM(v.tokens_out), 0) AS total_tokens_out,
                COALESCE(ROUND(AVG(v.tokens_out), 1), 0.0) AS avg_tokens_out,
                COALESCE(MIN(v.tokens_out), 0) AS min_tokens_out,
                COALESCE(MAX(v.tokens_out), 0) AS max_tokens_out,
                COALESCE(ROUND(AVG(v.tokens_in), 1), 0.0) AS avg_tokens_in,
                COALESCE(SUM(v.cost_usd), 0.0) AS total_cost_usd,
                COALESCE(ROUND(AVG(v.cost_usd), 6), 0.0) AS avg_cost_usd,
                SUM(CASE WHEN v.fit = 1 THEN 1 ELSE 0 END) AS fit_count,
                SUM(CASE WHEN v.fit = 0 THEN 1 ELSE 0 END) AS no_fit_count
            FROM job_verdicts v
        """
        row = cursor.execute(stats_sql).fetchone()
        total_verdicts = row["total_verdicts"] or 0
        total_in = row["total_tokens_in"] or 0
        total_cached = row["total_tokens_cached"] or 0
        cache_hit_rate = round(total_cached / total_in, 4) if total_in > 0 else 0.0
        fit_count = row["fit_count"] or 0
        no_fit_count = row["no_fit_count"] or 0
        fit_rate = round(fit_count / total_verdicts, 4) if total_verdicts > 0 else 0.0

        reasons_sql = """
            SELECT
                COALESCE(v.reason_type, 'unspecified') AS reason_type,
                COUNT(*) AS count,
                COALESCE(ROUND(AVG(v.tokens_out), 1), 0.0) AS avg_tokens_out
            FROM job_verdicts v
            GROUP BY COALESCE(v.reason_type, 'unspecified')
            ORDER BY count DESC
        """
        reasons = []
        for r in cursor.execute(reasons_sql).fetchall():
            rtype = r["reason_type"] or "unspecified"
            rcnt = r["count"]
            reasons.append(
                {
                    "reason_type": rtype,
                    "count": rcnt,
                    "avg_tokens_out": r["avg_tokens_out"],
                    "percentage": round(rcnt / total_verdicts, 4) if total_verdicts > 0 else 0.0,
                }
            )

        models_sql = """
            SELECT
                COALESCE(v.model, 'unknown') AS model,
                COUNT(*) AS count,
                COALESCE(SUM(v.tokens_out), 0) AS total_tokens_out,
                COALESCE(ROUND(AVG(v.tokens_out), 1), 0.0) AS avg_tokens_out,
                COALESCE(SUM(v.cost_usd), 0.0) AS total_cost_usd
            FROM job_verdicts v
            GROUP BY COALESCE(v.model, 'unknown')
            ORDER BY count DESC
        """
        models = [
            {
                "model": m["model"],
                "count": m["count"],
                "total_tokens_out": m["total_tokens_out"],
                "avg_tokens_out": m["avg_tokens_out"],
                "total_cost_usd": round(m["total_cost_usd"], 6),
            }
            for m in cursor.execute(models_sql).fetchall()
        ]

        return {
            "total_verdicts": total_verdicts,
            "total_tokens_in": total_in,
            "total_tokens_cached": total_cached,
            "total_tokens_out": row["total_tokens_out"] or 0,
            "cache_hit_rate": cache_hit_rate,
            "avg_tokens_out": row["avg_tokens_out"] or 0.0,
            "min_tokens_out": row["min_tokens_out"] or 0,
            "max_tokens_out": row["max_tokens_out"] or 0,
            "avg_tokens_in": row["avg_tokens_in"] or 0.0,
            "total_cost_usd": round(row["total_cost_usd"] or 0.0, 4),
            "avg_cost_usd": row["avg_cost_usd"] or 0.0,
            "fit_count": fit_count,
            "no_fit_count": no_fit_count,
            "fit_rate": fit_rate,
            "reasons": reasons,
            "models": models,
        }

    def query_observability_verdicts(
        self,
        q: str | None = None,
        fit: str | None = None,
        reason_type: str | None = None,
        model: str | None = None,
        sort: str = "tokens_out_desc",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Query verdicts with token usage details and search filters."""
        cursor = self.conn.cursor()
        has_table = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_verdicts'"
        ).fetchone()
        if not has_table:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}

        cols = {row[1] for row in cursor.execute("PRAGMA table_info(job_verdicts)").fetchall()}
        has_fit = "fit" in cols
        has_reason_type = "reason_type" in cols
        has_reason_desc = "reason_description" in cols
        has_verdict = "verdict" in cols
        has_reasoning = "reasoning" in cols

        fit_expr = (
            "v.fit"
            if has_fit
            else (
                "(CASE WHEN v.verdict IN ('strong', 'worth_applying') "
                "OR (v.fit_score IS NOT NULL AND v.fit_score >= 60) THEN 1 ELSE 0 END)"
                if has_verdict
                else "0"
            )
        )

        reason_expr = (
            "COALESCE(v.reason_type, v.verdict, 'unspecified')"
            if (has_reason_type and has_verdict)
            else (
                "COALESCE(v.reason_type, 'unspecified')"
                if has_reason_type
                else ("COALESCE(v.verdict, 'unspecified')" if has_verdict else "'unspecified'")
            )
        )

        desc_expr = (
            "COALESCE(v.reason_description, v.reasoning, '')"
            if (has_reason_desc and has_reasoning)
            else (
                "COALESCE(v.reason_description, '')"
                if has_reason_desc
                else ("COALESCE(v.reasoning, '')" if has_reasoning else "''")
            )
        )

        where_clauses: list[str] = ["1=1"]
        params: list[Any] = []

        if q and q.strip():
            term = f"%{q.strip()}%"
            where_clauses.append(f"(j.title LIKE ? OR j.company LIKE ? OR {desc_expr} LIKE ?)")
            params.extend([term, term, term])

        if fit in ("true", "1", "fit"):
            where_clauses.append(f"({fit_expr} = 1)")
        elif fit in ("false", "0", "no_fit"):
            where_clauses.append(f"({fit_expr} = 0)")

        if reason_type and reason_type.lower() not in ("all", ""):
            where_clauses.append(f"LOWER({reason_expr}) = LOWER(?)")
            params.append(reason_type.strip())

        if model and model.lower() not in ("all", ""):
            where_clauses.append("COALESCE(v.model, 'unknown') = ?")
            params.append(model.strip())

        where_sql = " AND ".join(where_clauses)

        # Count total
        count_sql = f"""
            SELECT COUNT(*)
            FROM job_verdicts v
            JOIN jobs j ON j.id = v.job_id
            WHERE {where_sql}
        """
        total = cursor.execute(count_sql, params).fetchone()[0]

        # Sorting
        if sort == "tokens_out_desc":
            order_by = "v.tokens_out DESC NULLS LAST, v.created_at DESC"
        elif sort == "tokens_out_asc":
            order_by = "v.tokens_out ASC NULLS LAST, v.created_at DESC"
        elif sort == "cost_desc":
            order_by = "v.cost_usd DESC NULLS LAST, v.created_at DESC"
        elif sort == "date_asc":
            order_by = "v.created_at ASC"
        else:  # date_desc
            order_by = "v.created_at DESC"

        query_sql = f"""
            SELECT
                v.id,
                v.job_id,
                j.title,
                j.company,
                j.location,
                j.url,
                {fit_expr} AS fit,
                {reason_expr} AS reason_type,
                {desc_expr} AS reason_description,
                COALESCE(v.tokens_in, 0) AS tokens_in,
                COALESCE(v.tokens_cached, 0) AS tokens_cached,
                COALESCE(v.tokens_out, 0) AS tokens_out,
                COALESCE(v.cost_usd, 0.0) AS cost_usd,
                COALESCE(v.model, 'unknown') AS model,
                COALESCE(v.created_at, '') AS created_at
            FROM job_verdicts v
            JOIN jobs j ON j.id = v.job_id
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """
        query_params = [*list(params), limit, offset]
        rows = cursor.execute(query_sql, query_params).fetchall()

        items = [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "title": r["title"] or "Untitled Role",
                "company": r["company"] or "Unknown Company",
                "location": r["location"],
                "url": r["url"] or "",
                "fit": bool(r["fit"]) if r["fit"] is not None else False,
                "reason_type": r["reason_type"] or "unknown",
                "reason_description": r["reason_description"] or "",
                "tokens_in": r["tokens_in"],
                "tokens_cached": r["tokens_cached"],
                "tokens_out": r["tokens_out"],
                "cost_usd": round(r["cost_usd"], 6),
                "model": r["model"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # =====================================================================================
    # Scrape cells
    # =====================================================================================

    def seed_cells(self, specs: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert any missing cells, preserving the state of existing ones.

        Re-runnable after every roles.yaml edit: an existing cell keeps its scrape history
        so editing the taxonomy does not reset coverage.

        `tier` and `enabled` are the two exceptions, and both are declarative rather than
        accumulated -- they say what the taxonomy currently wants, not what the cell has
        done. DO NOTHING carried them from whenever the row was first inserted, so a tier
        change in roles.yaml applied only to families that had never been seeded: demoting
        seven families on 2026-08-15 would have moved 0 of the 150 cells that had history
        and all of the 228 that had none, which is precisely backwards. `prune_cells` is
        the only writer of enabled = 0, so re-enabling here just says a family that left
        the taxonomy and came back is wanted again.

        Returns (inserted, updated). The two are counted separately because `rowcount`
        cannot tell them apart once the conflict arm writes, and "12 cells inserted" when
        nothing new was added is the kind of report that hides a no-op re-seed.
        """
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        before = cursor.execute("SELECT COUNT(*) FROM scrape_cells").fetchone()[0]
        touched = 0
        for spec in specs:
            cursor.execute(
                """
                INSERT INTO scrape_cells
                    (source, role_family, location_id, query, tier, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, role_family, location_id, query) DO UPDATE
                    SET tier = excluded.tier, enabled = 1
                  WHERE tier != excluded.tier OR enabled != 1
                """,
                (
                    spec["source"],
                    spec["role_family"],
                    spec["location_id"],
                    spec["query"],
                    spec["tier"],
                    now,
                ),
            )
            touched += cursor.rowcount
        after = cursor.execute("SELECT COUNT(*) FROM scrape_cells").fetchone()[0]
        self.conn.commit()
        inserted = after - before
        return inserted, touched - inserted

    def prune_cells(self, specs: list[dict[str, Any]]) -> int:
        """Disable cells no longer present in the taxonomy.

        Disabled rather than deleted, so cell_observations keeps its foreign key and past
        analytics stay auditable.
        """
        wanted = {(s["source"], s["role_family"], s["location_id"], s["query"]) for s in specs}
        cursor = self.conn.cursor()
        disabled = 0
        for row in cursor.execute(
            "SELECT id, source, role_family, location_id, query FROM scrape_cells WHERE enabled = 1"
        ).fetchall():
            key = (row["source"], row["role_family"], row["location_id"], row["query"])
            if key not in wanted:
                self.conn.execute("UPDATE scrape_cells SET enabled = 0 WHERE id = ?", (row["id"],))
                disabled += 1
        self.conn.commit()
        return disabled

    def get_cells(self, source: str | None = None, enabled_only: bool = True) -> list["CellState"]:
        """Load cells as scheduler.CellState objects."""
        from careerradar.search.scheduler import CellState

        query = "SELECT * FROM scrape_cells WHERE 1=1"
        params: list[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if enabled_only:
            query += " AND enabled = 1"

        cells: list[CellState] = []
        for row in self.conn.execute(query, params):
            cells.append(
                CellState(
                    id=row["id"],
                    source=row["source"],
                    role_family=row["role_family"],
                    location_id=row["location_id"],
                    query=row["query"],
                    tier=row["tier"],
                    enabled=row["enabled"],
                    last_scraped_at=row["last_scraped_at"],
                    last_success_at=row["last_success_at"],
                    last_result_count=row["last_result_count"] or 0,
                    last_saturated=row["last_saturated"] or 0,
                    last_hours_old=row["last_hours_old"],
                    ewma_new_per_scrape=row["ewma_new_per_scrape"],
                    ewma_fit_score=row["ewma_fit_score"],
                    quality_samples=row["quality_samples"] or 0,
                    consecutive_empty=row["consecutive_empty"] or 0,
                    consecutive_error=row["consecutive_error"] or 0,
                    total_scrapes=row["total_scrapes"] or 0,
                    backoff_until=row["backoff_until"],
                )
            )
        return cells

    def record_cell_attempt(
        self,
        cell_id: int,
        observed_at: str,
        hours_old: int | None,
        requested: int,
        returned: int = 0,
        new_unique: int = 0,
        saturated: int = 0,
        status: str = "ok",
        error: str | None = None,
        backoff_until: str | None = None,
        ewma: float | None = None,
    ) -> None:
        """Update a cell after an attempt.

        INVARIANT: last_scraped_at advances on every attempt, last_success_at only on
        success. Advancing both together would let a persistently failing cell look fresh,
        so the staleness floor would stop firing and the dashboard would report coverage of
        data never collected.
        """
        cursor = self.conn.cursor()
        succeeded = status in ("ok", "empty")

        fields = [
            "last_scraped_at = ?",
            "last_requested = ?",
            "last_result_count = ?",
            "last_new_count = ?",
            "last_saturated = ?",
            "last_hours_old = ?",
            "total_scrapes = total_scrapes + 1",
            "total_postings = total_postings + ?",
        ]
        params: list[Any] = [
            observed_at,
            requested,
            returned,
            new_unique,
            saturated,
            hours_old,
            returned,
        ]

        if succeeded:
            fields.append("last_success_at = ?")
            params.append(observed_at)
            fields.append("consecutive_error = 0")
            if returned == 0:
                fields.append("consecutive_empty = consecutive_empty + 1")
            else:
                fields.append("consecutive_empty = 0")
        else:
            fields.append("consecutive_error = consecutive_error + 1")

        if ewma is not None:
            fields.append("ewma_new_per_scrape = ?")
            params.append(ewma)
        if backoff_until is not None:
            fields.append("backoff_until = ?")
            params.append(backoff_until)
        if error is not None:
            fields.append("last_error = ?")
            params.append(error[:500])

        params.append(cell_id)
        cursor.execute(f"UPDATE scrape_cells SET {', '.join(fields)} WHERE id = ?", params)
        self.conn.commit()

    def update_cell_quality(self, cell_id: int, fit_score: float) -> None:
        """Fold one posting's LLM fit_score into its scrape cell's quality EWMA.

        Called from scoring, not scraping -- a cell's realized quality is only known once
        `job_verdicts` lands, which happens on its own faster cadence. See
        scheduler.quality_multiplier() for how this feeds cell_priority(). Does not commit,
        matching `_persist()`'s pattern in scoring/worker.py -- the caller batches commits
        across many scored postings.
        """
        from careerradar.search.scheduler import update_ewma

        row = self.conn.execute(
            "SELECT ewma_fit_score FROM scrape_cells WHERE id = ?", (cell_id,)
        ).fetchone()
        if row is None:
            return
        new_ewma = update_ewma(row["ewma_fit_score"], fit_score)
        self.conn.execute(
            "UPDATE scrape_cells SET ewma_fit_score = ?, quality_samples = quality_samples + 1 "
            "WHERE id = ?",
            (new_ewma, cell_id),
        )

    # =====================================================================================
    # Source circuit-breaker state
    # =====================================================================================

    def get_source_backoff(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT backoff_until FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return row["backoff_until"] if row else None

    def get_source_trips(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT consecutive_trips FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return (row["consecutive_trips"] if row else 0) or 0

    def set_source_backoff(
        self, source: str, until: str, reason: str | None = None, escalate: bool = False
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO source_state
                (source, backoff_until, consecutive_trips, last_trip_at,
                 last_trip_reason, total_429)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source) DO UPDATE SET
                backoff_until = excluded.backoff_until,
                consecutive_trips = source_state.consecutive_trips + ?,
                last_trip_at = excluded.last_trip_at,
                last_trip_reason = excluded.last_trip_reason,
                total_429 = source_state.total_429 + ?
            """,
            (
                source,
                until,
                1 if escalate else 0,
                now,
                reason,
                1 if escalate else 0,
                1 if escalate else 0,
                1 if escalate else 0,
            ),
        )
        self.conn.commit()

    def reset_source_trips(self, source: str) -> None:
        self.conn.execute(
            "UPDATE source_state SET consecutive_trips = 0, backoff_until = NULL WHERE source = ?",
            (source,),
        )
        self.conn.commit()

    # =====================================================================================
    # Sync runs and cell observations
    # =====================================================================================

    def start_sync_run(
        self, mode: str, taxonomy_hash: str | None = None, plan_hash: str | None = None
    ) -> int | None:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO sync_runs (started_at, mode, status, taxonomy_hash, plan_hash) "
            "VALUES (?, ?, 'running', ?, ?)",
            (datetime.now(timezone.utc).isoformat(), mode, taxonomy_hash, plan_hash),
        )
        self.conn.commit()
        return cursor.lastrowid

    def finish_sync_run(self, run_id: int, status: str, **counters: Any) -> None:
        allowed = {
            "cells_planned",
            "cells_succeeded",
            "cells_skipped",
            "postings_fetched",
            "postings_new",
            "duplicates_merged",
            "llm_cost_usd",
            "error_summary",
        }
        fields = ["finished_at = ?", "status = ?"]
        params: list[Any] = [datetime.now(timezone.utc).isoformat(), status]
        for key, value in counters.items():
            if key in allowed:
                fields.append(f"{key} = ?")
                params.append(
                    json.dumps(value)
                    if key == "error_summary" and not isinstance(value, str)
                    else value
                )
        params.append(run_id)
        self.conn.execute(f"UPDATE sync_runs SET {', '.join(fields)} WHERE id = ?", params)
        self.conn.commit()

    def record_observation(
        self,
        run_id: int | None,
        task: dict[str, Any],
        observed_at: str,
        returned: int = 0,
        returned_on_topic: int = 0,
        new_unique: int = 0,
        saturated: int = 0,
        descriptions_full: int = 0,
        status: str = "ok",
        error: str | BaseException | None = None,
        duration_ms: int | None = None,
        requests_made: int | None = None,
    ) -> None:
        """Write the sampling denominator for one cell visit.

        `returned` drives saturation (did the board truncate us?), `returned_on_topic`
        drives flow (postings whose title actually maps to a role family). Keeping them
        apart is what stops off-target results -- a "Platform Engineer" query returning a
        Maintenance Technician -- from inflating supply.

        `duration_ms` and `requests_made` are measurements, so they stay NULL when the
        caller did not measure. Writing 0 would make an unmeasured cell read as a free one
        and quietly bias every average taken over the `cell_cost` view.
        """
        window_start = None
        if task.get("hours_old"):
            window_start = (
                _parse_iso(observed_at) - timedelta(hours=task["hours_old"])
            ).isoformat()

        self.conn.execute(
            """
            INSERT INTO cell_observations
                (sync_run_id, cell_id, source, role_family, location_id, query,
                 observed_at, hours_old, window_start, window_end,
                 requested, returned, returned_on_topic, new_unique, saturated,
                 desc_selection, descriptions_full, status, error,
                 duration_ms, requests_made)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                task.get("cell_id"),
                task.get("source"),
                task.get("role_family") or "",
                task.get("location_id") or "",
                task.get("query") or "",
                observed_at,
                task.get("hours_old"),
                window_start,
                observed_at,
                task.get("results_wanted") or 0,
                returned,
                returned_on_topic,
                new_unique,
                saturated,
                task.get("desc_selection", "none"),
                descriptions_full,
                status,
                (error or None) and str(error)[:500],
                duration_ms,
                requests_made,
            ),
        )
        self.conn.commit()

    # =====================================================================================
    # Enriched posting upsert
    # =====================================================================================

    POSTING_COLUMNS: ClassVar[list[str]] = [
        "job_key",
        "title",
        "company",
        "company_normalized",
        "location",
        "city",
        "region",
        "country",
        "url",
        "url_direct",
        "description",
        "source",
        "site_job_id",
        "role_family",
        "role_family_hint",
        "seniority",
        "is_remote",
        "date_posted",
        "date_precision",
        "posted_window_start",
        "posted_window_end",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_interval",
        "salary_annual_usd",
        "salary_currency_inferred",
        "salary_source",
        "description_quality",
        "desc_selection",
        "content_hash",
        "is_agency",
        "company_num_employees",
        "company_industry",
        "access",
        "scrape_cell_id",
        "sync_run_id",
        "taxonomy_hash",
        "match_score",
        "matched_skills",
        "matched_count",
        "required_count",
        "scorer_version",
        "pipeline_state",
    ]

    def upsert_posting(
        self,
        posting: dict[str, Any],
        run_id: int | None = None,
        taxonomy_hash: str | None = None,
    ) -> tuple[int | None, bool]:
        """Insert or refresh one posting. Returns (job_id, is_new).

        Dedup order matters: the board's own id is the strongest key, then the URL, then
        the content hash for the same requisition cross-posted to both boards.
        """
        cursor = self.conn.cursor()
        record = dict(posting)
        record["sync_run_id"] = run_id
        record["taxonomy_hash"] = taxonomy_hash
        if isinstance(record.get("matched_skills"), (list, dict)):
            record["matched_skills"] = json.dumps(record["matched_skills"])

        existing = None
        if record.get("job_key"):
            existing = cursor.execute(
                "SELECT id FROM jobs WHERE job_key = ?", (record["job_key"],)
            ).fetchone()
        if existing is None and record.get("url"):
            existing = cursor.execute(
                "SELECT id FROM jobs WHERE url = ?", (record["url"],)
            ).fetchone()

        if existing is not None:
            job_id = existing["id"]
            updatable = [
                c for c in self.POSTING_COLUMNS if c in record and c not in ("job_key", "url")
            ]
            # Re-finding a posting is evidence it is still open, and it was previously
            # thrown away: `sync_run_id` was overwritten and nothing recorded WHEN the
            # posting was last actually seen. Without that, a posting missing from a scrape
            # is indistinguishable from one whose cell has not been scraped since -- and
            # with a ~1.5-day cell rotation, most of the corpus is in the second case.
            cursor.execute(
                f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in updatable)}"
                f"{', ' if updatable else ' '}"
                "last_seen_at = ?, times_seen = COALESCE(times_seen, 1) + 1 "
                "WHERE id = ?",
                [record[c] for c in updatable] + [_utcnow(), job_id],
            )
            self.conn.commit()
            return job_id, False

        columns = [c for c in self.POSTING_COLUMNS if c in record]
        record["last_seen_at"] = _utcnow()
        columns.append("last_seen_at")
        columns.append("date_found")
        values = [record[c] for c in columns[:-1]]
        values.append(datetime.now(timezone.utc).isoformat())

        cursor.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        self.conn.commit()
        return cursor.lastrowid, True

    def find_duplicate(self, content_hash: str | None, exclude_id: int | None = None) -> int | None:
        """Earliest non-duplicate posting sharing a content hash."""
        if not content_hash:
            return None
        query = "SELECT id FROM jobs WHERE content_hash = ? AND duplicate_of IS NULL"
        params: list[Any] = [content_hash]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        query += " ORDER BY id LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        return row["id"] if row else None

    def mark_duplicate(self, job_id: int, canonical_id: int) -> None:
        self.conn.execute("UPDATE jobs SET duplicate_of = ? WHERE id = ?", (canonical_id, job_id))
        self.conn.commit()

    def replace_job_skills(self, job_id: int, skills: dict[str, dict[str, Any]]) -> None:
        """Rewrite a posting's skill rows. `skills` is {key: {"in_title": bool}}."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM job_skills WHERE job_id = ?", (job_id,))
        if skills:
            cursor.executemany(
                "INSERT OR REPLACE INTO job_skills (job_id, skill, in_title) VALUES (?, ?, ?)",
                [(job_id, key, 1 if info.get("in_title") else 0) for key, info in skills.items()],
            )
        self.conn.commit()

    def replace_job_blockers(self, job_id: int, blockers: list[str]) -> None:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM job_blockers WHERE job_id = ?", (job_id,))
        if blockers:
            cursor.executemany(
                "INSERT OR REPLACE INTO job_blockers (job_id, blocker) VALUES (?, ?)",
                [(job_id, b) for b in blockers],
            )
        self.conn.commit()

    def record_skill_candidates(self, terms: dict[str, int]) -> None:
        """Accumulate unmatched capitalized n-grams for taxonomy review.

        Nothing is auto-promoted; reviewing this table is a manual step.
        """
        if not terms:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            """
            INSERT INTO skill_candidates (term, n_postings, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (term) DO UPDATE SET
                n_postings = skill_candidates.n_postings + excluded.n_postings,
                last_seen = excluded.last_seen
            """,
            [(term, count, now, now) for term, count in terms.items()],
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _parse_iso(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
