import sqlite3
import os
import json
from datetime import datetime, timedelta, timezone

from careerradar.core.paths import DB_PATH  # noqa: F401


# The two requirement lists are stored as separate JSON columns because the model answers
# them as separate fields, but they are one table: `core_requirements` carries the
# importance, `requirement_assessments` carries the status, and the join key is the
# requirement text the model was told to repeat verbatim. `profile/models.py` validates
# that repetition and normalises with `strip().casefold()` -- the same normalisation is
# used here so the dashboard and the validator cannot disagree about which requirement is
# which.
def _requirement_summary(core, assessments):
    """Must-have counts for one verdict, or None when there is nothing to count.

    None rather than a zeroed dict: a verdict written before the ordinal schema has no
    requirement extraction at all, and "0 of 0 must-haves met" would read as a finding
    about the posting rather than about the row's age.
    """
    if not core:
        return None
    status = {a.get("requirement", "").strip().casefold(): a.get("status")
              for a in assessments or []}
    counts = {"met": 0, "partial": 0, "unmet": 0, "unassessed": 0}
    for requirement in core:
        if requirement.get("importance") != "must_have":
            continue
        counts[status.get(requirement.get("requirement", "").strip().casefold())
               or "unassessed"] += 1
    total = sum(counts.values())
    if not total:
        return None
    return {"must_total": total, **{f"must_{k}": v for k, v in counts.items()}}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()

class Database:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        """Bring the schema up to date via the versioned migration runner.

        Migrations are idempotent, so calling this on every connection is cheap: once
        user_version matches, it is a single PRAGMA read.
        """
        from careerradar.core.migrations import migrate

        migrate(self.conn)

    # The default ranks WITHOUT inventing an exchange rate between the dimensions.
    # Eligibility partitions -- nothing blocked outranks anything eligible, at any tier --
    # and `pareto_tier` orders within a partition by dominance, so two postings share a
    # tier only when neither is better than the other on every dimension. Ties inside a
    # tier are real; recency breaks them, and nothing should be read into the result.
    #
    # `fit_score` remains available as a coarse sort. It is a projection of the same
    # ordinals through an invented weighting (see scoring/scale.py), which is exactly why
    # it is not the default.
    _SORTS = {
        "fit": ("CASE v.eligibility WHEN 'eligible' THEN 0 WHEN 'conditional' THEN 1 "
                "WHEN 'blocked' THEN 2 ELSE 3 END, "
                "COALESCE(v.pareto_tier, 99), date_found DESC"),
        "fit_score": "fit_score DESC, match_score DESC, date_found DESC",
        "match_score": "match_score DESC, date_found DESC",
        "date_found": "date_found DESC",
    }

    def query_jobs(self, status=None, country=None, role_family=None,
                   seniority=None, source=None, is_remote=None, has_salary=None,
                   access=None, include_duplicates=False, min_score=None,
                   verdict=None, pipeline_state=None, min_fit_score=None,
                   eligibility=None, role_match=None, capability_match=None,
                   max_tier=None, liveness=None, job_id=None,
                   sort="fit", limit=200, offset=0):
        """Filtered, paginated posting list for the dashboard.

        Duplicates are hidden by default: the same requisition cross-posted to both boards
        would otherwise appear twice in the feed.

        The verdict fields live in `job_verdicts`, one row per (posting, profile version).
        Joining against the *active* profile rather than the latest verdict means a profile
        rebuild does not retroactively rewrite what the dashboard shows until the backlog
        has actually been re-scored under it.
        """
        query = """
            SELECT jobs.*,
                   v.verdict        AS verdict,
                   v.seniority_fit  AS seniority_fit,
                   v.hard_blockers  AS hard_blockers,
                   v.key_gaps       AS key_gaps,
                   v.strengths      AS strengths,
                   v.reasoning      AS reasoning,
                   v.role_summary   AS role_summary,
                   v.eligibility    AS eligibility,
                   v.role_match     AS role_match,
                   v.capability_match AS capability_match,
                   v.seniority_gap  AS seniority_gap,
                   v.evidence_quality AS evidence_quality,
                   v.pareto_tier    AS pareto_tier,
                   v.core_requirements AS core_requirements,
                   v.requirement_assessments AS requirement_assessments,
                   v.audit_flags    AS audit_flags,
                   v.scale_version  AS scale_version,
                   l.liveness       AS liveness
              FROM jobs
              LEFT JOIN job_verdicts v
                     ON v.job_id = jobs.id
                    AND v.profile_version = (
                        SELECT version FROM profiles WHERE is_active = 1
                    )
              LEFT JOIN v_job_liveness l ON l.job_id = jobs.id
             WHERE 1=1
        """
        params = []

        # Fetching one posting reuses this method so that the drawer sees exactly the row
        # shape the feed does -- the same verdict join, the same JSON decoding, the same
        # requirement summary. A second bespoke query is how the two drifted apart before.
        if job_id is not None:
            query += " AND jobs.id = ?"
            params.append(job_id)
        if not include_duplicates:
            query += " AND duplicate_of IS NULL"
        for column, value in (
            ("jobs.status", status), ("jobs.country", country),
            ("jobs.role_family", role_family), ("jobs.seniority", seniority),
            ("jobs.source", source), ("jobs.access", access),
            ("jobs.pipeline_state", pipeline_state), ("v.verdict", verdict),
            ("v.eligibility", eligibility), ("v.role_match", role_match),
            ("v.capability_match", capability_match), ("l.liveness", liveness),
        ):
            if value:
                query += f" AND {column} = ?"
                params.append(value)
        if is_remote is not None:
            query += " AND jobs.is_remote = ?"
            params.append(1 if is_remote else 0)
        if has_salary is not None:
            query += (" AND jobs.salary_annual_usd IS NOT NULL" if has_salary
                      else " AND jobs.salary_annual_usd IS NULL")
        if min_score is not None:
            query += " AND jobs.match_score >= ?"
            params.append(min_score)
        if min_fit_score is not None:
            query += " AND jobs.fit_score >= ?"
            params.append(min_fit_score)
        if max_tier is not None:
            query += " AND v.pareto_tier <= ?"
            params.append(max_tier)

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM ({query})", params
        ).fetchone()[0]

        query += f" ORDER BY {self._SORTS.get(sort, self._SORTS['fit_score'])}"
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        jobs = []
        for row in self.conn.execute(query, params):
            job = dict(row)
            job["matched_skills"] = (
                json.loads(row["matched_skills"]) if row["matched_skills"] else []
            )
            for field in ("hard_blockers", "key_gaps", "strengths",
                          "core_requirements", "requirement_assessments"):
                job[field] = json.loads(job[field]) if job.get(field) else []
            # Derived here, not in the frontend: the join is on normalised requirement text
            # and the normalisation has to match the validator in `profile/models.py`. One
            # implementation, server-side, where the rule already lives.
            job["requirement_summary"] = _requirement_summary(
                job["core_requirements"], job["requirement_assessments"]
            )
            jobs.append(job)

        return {
            "jobs": jobs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(jobs) < total,
        }

    def update_job_status(self, job_id, status):
        cursor = self.conn.cursor()
        now_str = datetime.now().isoformat() if status == "applied" else None
        
        if status == "applied":
            cursor.execute("UPDATE jobs SET status = ?, date_applied = ? WHERE id = ?", (status, now_str, job_id))
        else:
            cursor.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_stats(self):
        cursor = self.conn.cursor()
        stats = {}
        
        # Total counts by status
        cursor.execute("SELECT status, COUNT(*) as count FROM jobs GROUP BY status")
        stats["status_counts"] = {r["status"]: r["count"] for r in cursor.fetchall()}
        for s in ["unread", "saved", "applied", "rejected"]:
            if s not in stats["status_counts"]:
                stats["status_counts"][s] = 0
                
        # Total crawled
        cursor.execute("SELECT COUNT(*) FROM jobs")
        stats["total_jobs"] = cursor.fetchone()[0]
        
        # Coverage, reported with its denominator. `match_score` was never a percent --
        # the coverage prior, an unspecified seniority and an unmapped family put ~23
        # points on the floor, so its observed range was 15-80.
        cursor.execute(
            "SELECT SUM(matched_count), SUM(required_count) FROM jobs "
            "WHERE required_count > 0"
        )
        matched, required = cursor.fetchone()
        stats["skill_coverage"] = {
            "matched": matched or 0,
            "required": required or 0,
            "ratio": round(matched / required, 3) if required else None,
        }
        
        # Counts by country
        cursor.execute("SELECT country, COUNT(*) as count FROM jobs GROUP BY country")
        stats["country_counts"] = {r["country"]: r["count"] for r in cursor.fetchall() if r["country"]}
        
        # Where postings sit in the pipeline -- the dashboard's "is the backlog drained?"
        cursor.execute("SELECT pipeline_state, COUNT(*) as count FROM jobs GROUP BY pipeline_state")
        stats["pipeline_counts"] = {r["pipeline_state"]: r["count"] for r in cursor.fetchall()}

        # Ordinal marginals, not an average score. A mean over a projected scale says
        # very little; a collapsed marginal (90% `meets`) is the thing worth catching, and
        # it is the new "the model is not using its scale".
        active = "(SELECT version FROM profiles WHERE is_active = 1)"
        stats["ordinals"] = {}
        for dimension in ("eligibility", "role_match", "capability_match",
                          "evidence_quality"):
            cursor.execute(
                f"SELECT {dimension}, COUNT(*) c FROM job_verdicts "
                f"WHERE profile_version = {active} AND {dimension} IS NOT NULL "
                f"GROUP BY 1 ORDER BY 2 DESC"
            )
            stats["ordinals"][dimension] = {r[0]: r[1] for r in cursor.fetchall()}

        # Worth applying to, stated as a predicate rather than a threshold on a lumpy
        # scale. The old `fit_score >= 70` sat between two quantisation attractors (62 and
        # 72), so it was partly measuring where the model liked to round.
        cursor.execute(
            f"""
            SELECT COUNT(*) FROM jobs j
              JOIN job_verdicts v ON v.job_id = j.id AND v.profile_version = {active}
             WHERE j.duplicate_of IS NULL
               AND v.eligibility = 'eligible'
               AND v.role_match IN ('same_role', 'adjacent')
               AND v.capability_match IN ('exceeds', 'meets')
            """
        )
        stats["strong_matches"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT liveness, COUNT(*) FROM v_job_liveness GROUP BY 1"
        )
        stats["liveness_counts"] = {r[0]: r[1] for r in cursor.fetchall()}

        return stats

    # =====================================================================================
    # Scrape cells
    # =====================================================================================

    def seed_cells(self, specs):
        """Insert any missing cells, preserving the state of existing ones.

        Re-runnable after every roles.yaml edit: an existing cell keeps its scrape history
        so editing the taxonomy does not reset coverage.
        """
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        inserted = 0
        for spec in specs:
            cursor.execute(
                """
                INSERT INTO scrape_cells
                    (source, role_family, location_id, query, tier, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, role_family, location_id, query) DO NOTHING
                """,
                (spec["source"], spec["role_family"], spec["location_id"],
                 spec["query"], spec["tier"], now),
            )
            inserted += cursor.rowcount
        self.conn.commit()
        return inserted

    def prune_cells(self, specs):
        """Disable cells no longer present in the taxonomy.

        Disabled rather than deleted, so cell_observations keeps its foreign key and past
        analytics stay auditable.
        """
        wanted = {
            (s["source"], s["role_family"], s["location_id"], s["query"]) for s in specs
        }
        cursor = self.conn.cursor()
        disabled = 0
        for row in cursor.execute(
            "SELECT id, source, role_family, location_id, query FROM scrape_cells "
            "WHERE enabled = 1"
        ).fetchall():
            key = (row["source"], row["role_family"], row["location_id"], row["query"])
            if key not in wanted:
                self.conn.execute(
                    "UPDATE scrape_cells SET enabled = 0 WHERE id = ?", (row["id"],)
                )
                disabled += 1
        self.conn.commit()
        return disabled

    def get_cells(self, source=None, enabled_only=True):
        """Load cells as scheduler.CellState objects."""
        from careerradar.search.scheduler import CellState

        query = "SELECT * FROM scrape_cells WHERE 1=1"
        params = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if enabled_only:
            query += " AND enabled = 1"

        cells = []
        for row in self.conn.execute(query, params):
            cells.append(CellState(
                id=row["id"], source=row["source"], role_family=row["role_family"],
                location_id=row["location_id"], query=row["query"], tier=row["tier"],
                enabled=row["enabled"],
                last_scraped_at=row["last_scraped_at"],
                last_success_at=row["last_success_at"],
                last_result_count=row["last_result_count"] or 0,
                last_saturated=row["last_saturated"] or 0,
                last_hours_old=row["last_hours_old"],
                ewma_new_per_scrape=row["ewma_new_per_scrape"],
                consecutive_empty=row["consecutive_empty"] or 0,
                consecutive_error=row["consecutive_error"] or 0,
                total_scrapes=row["total_scrapes"] or 0,
                backoff_until=row["backoff_until"],
            ))
        return cells

    def record_cell_attempt(self, cell_id, observed_at, hours_old, requested,
                            returned=0, new_unique=0, saturated=0, status="ok",
                            error=None, backoff_until=None, ewma=None):
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
        params = [observed_at, requested, returned, new_unique, saturated, hours_old,
                  returned]

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
        cursor.execute(
            f"UPDATE scrape_cells SET {', '.join(fields)} WHERE id = ?", params
        )
        self.conn.commit()

    # =====================================================================================
    # Source circuit-breaker state
    # =====================================================================================

    def get_source_backoff(self, source):
        row = self.conn.execute(
            "SELECT backoff_until FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return row["backoff_until"] if row else None

    def get_source_trips(self, source):
        row = self.conn.execute(
            "SELECT consecutive_trips FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return (row["consecutive_trips"] if row else 0) or 0

    def set_source_backoff(self, source, until, reason=None, escalate=False):
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
            (source, until, 1 if escalate else 0, now, reason, 1 if escalate else 0,
             1 if escalate else 0, 1 if escalate else 0),
        )
        self.conn.commit()

    def reset_source_trips(self, source):
        self.conn.execute(
            "UPDATE source_state SET consecutive_trips = 0, backoff_until = NULL "
            "WHERE source = ?", (source,)
        )
        self.conn.commit()

    # =====================================================================================
    # Sync runs and cell observations
    # =====================================================================================

    def start_sync_run(self, mode, taxonomy_hash=None, plan_hash=None):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO sync_runs (started_at, mode, status, taxonomy_hash, plan_hash) "
            "VALUES (?, ?, 'running', ?, ?)",
            (datetime.now(timezone.utc).isoformat(), mode, taxonomy_hash, plan_hash),
        )
        self.conn.commit()
        return cursor.lastrowid

    def finish_sync_run(self, run_id, status, **counters):
        allowed = {
            "cells_planned", "cells_succeeded", "cells_skipped", "postings_fetched",
            "postings_new", "duplicates_merged", "llm_cost_usd", "error_summary",
        }
        fields = ["finished_at = ?", "status = ?"]
        params = [datetime.now(timezone.utc).isoformat(), status]
        for key, value in counters.items():
            if key in allowed:
                fields.append(f"{key} = ?")
                params.append(
                    json.dumps(value) if key == "error_summary"
                    and not isinstance(value, str) else value
                )
        params.append(run_id)
        self.conn.execute(
            f"UPDATE sync_runs SET {', '.join(fields)} WHERE id = ?", params
        )
        self.conn.commit()

    def record_observation(self, run_id, task, observed_at, returned=0,
                           returned_on_topic=0, new_unique=0, saturated=0,
                           descriptions_full=0, status="ok", error=None):
        """Write the sampling denominator for one cell visit.

        `returned` drives saturation (did the board truncate us?), `returned_on_topic`
        drives flow (postings whose title actually maps to a role family). Keeping them
        apart is what stops off-target results -- a "Platform Engineer" query returning a
        Maintenance Technician -- from inflating supply.
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
                 desc_selection, descriptions_full, status, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, task.get("cell_id"), task.get("source"),
             task.get("role_family") or "", task.get("location_id") or "",
             task.get("query") or "", observed_at, task.get("hours_old"),
             window_start, observed_at, task.get("results_wanted") or 0,
             returned, returned_on_topic, new_unique, saturated,
             task.get("desc_selection", "none"), descriptions_full, status,
             (error or None) and str(error)[:500]),
        )
        self.conn.commit()

    # =====================================================================================
    # Enriched posting upsert
    # =====================================================================================

    POSTING_COLUMNS = [
        "job_key", "title", "company", "company_normalized", "location", "city",
        "region", "country", "url", "url_direct", "description", "source",
        "site_job_id", "role_family", "role_family_hint", "seniority", "is_remote",
        "date_posted", "date_precision", "posted_window_start", "posted_window_end",
        "salary_min", "salary_max", "salary_currency", "salary_interval",
        "salary_annual_usd", "salary_currency_inferred", "salary_source",
        "description_quality", "desc_selection", "content_hash", "is_agency",
        "company_num_employees", "company_industry", "access",
        "scrape_cell_id", "sync_run_id",
        "taxonomy_hash", "match_score", "matched_skills", "matched_count",
        "required_count", "scorer_version", "pipeline_state",
    ]

    def upsert_posting(self, posting, run_id=None, taxonomy_hash=None):
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
            updatable = [c for c in self.POSTING_COLUMNS
                         if c in record and c not in ("job_key", "url")]
            # Re-finding a posting is evidence it is still open, and it was previously
            # thrown away: `sync_run_id` was overwritten and nothing recorded WHEN the
            # posting was last actually seen. Without that, a posting missing from a scrape
            # is indistinguishable from one whose cell has not been scraped since -- and
            # with a ~5-day cell rotation, most of the corpus is in the second case.
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
            f"INSERT INTO jobs ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        self.conn.commit()
        return cursor.lastrowid, True

    def find_duplicate(self, content_hash, exclude_id=None):
        """Earliest non-duplicate posting sharing a content hash."""
        if not content_hash:
            return None
        query = (
            "SELECT id FROM jobs WHERE content_hash = ? AND duplicate_of IS NULL"
        )
        params = [content_hash]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        query += " ORDER BY id LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        return row["id"] if row else None

    def mark_duplicate(self, job_id, canonical_id):
        self.conn.execute(
            "UPDATE jobs SET duplicate_of = ? WHERE id = ?", (canonical_id, job_id)
        )
        self.conn.commit()

    def replace_job_skills(self, job_id, skills):
        """Rewrite a posting's skill rows. `skills` is {key: {"in_title": bool}}."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM job_skills WHERE job_id = ?", (job_id,))
        if skills:
            cursor.executemany(
                "INSERT OR REPLACE INTO job_skills (job_id, skill, in_title) "
                "VALUES (?, ?, ?)",
                [(job_id, key, 1 if info.get("in_title") else 0)
                 for key, info in skills.items()],
            )
        self.conn.commit()

    def replace_job_blockers(self, job_id, blockers):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM job_blockers WHERE job_id = ?", (job_id,))
        if blockers:
            cursor.executemany(
                "INSERT OR REPLACE INTO job_blockers (job_id, blocker) VALUES (?, ?)",
                [(job_id, b) for b in blockers],
            )
        self.conn.commit()

    def record_skill_candidates(self, terms):
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

    def close(self):
        self.conn.close()


def _parse_iso(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
