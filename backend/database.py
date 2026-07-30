import sqlite3
import os
import json
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs.db")

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
        from backend.migrations import migrate

        migrate(self.conn)

    def add_job(self, job_data):
        """
        Inserts a job. Returns True if inserted, False if it already exists.
        If it exists and the match score is higher, updates it.
        """
        cursor = self.conn.cursor()
        
        # Check if job exists by url or job_key
        job_key = job_data.get("job_key") or job_data.get("url")
        cursor.execute("SELECT id, match_score, status FROM jobs WHERE job_key = ? OR url = ?", (job_key, job_data.get("url")))
        row = cursor.fetchone()
        
        skills_json = json.dumps(job_data.get("matched_skills", []))
        now_str = datetime.now().isoformat()
        
        if row:
            # Job exists. If the match score is higher, or it was unread, update it.
            existing_id = row["id"]
            existing_score = row["match_score"]
            new_score = job_data.get("match_score", 0)
            
            if new_score > existing_score:
                cursor.execute("""
                    UPDATE jobs SET 
                        title = ?, company = ?, location = ?, country = ?, 
                        description = ?, match_score = ?, matched_skills = ?, 
                        resume_match = ?
                    WHERE id = ?
                """, (
                    job_data["title"], job_data.get("company"), job_data.get("location"),
                    job_data.get("country"), job_data.get("description"), new_score,
                    skills_json, job_data.get("resume_match"), existing_id
                ))
                self.conn.commit()
            return False
            
        # New job, insert it
        try:
            cursor.execute("""
                INSERT INTO jobs (
                    job_key, title, company, location, country, url, 
                    description, source, match_score, matched_skills, 
                    resume_match, status, date_found
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?)
            """, (
                job_key,
                job_data["title"],
                job_data.get("company"),
                job_data.get("location"),
                job_data.get("country"),
                job_data.get("url"),
                job_data.get("description"),
                job_data.get("source"),
                job_data.get("match_score", 0),
                skills_json,
                job_data.get("resume_match"),
                now_str
            ))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_jobs(self, status=None, country=None, resume_match=None, min_score=None):
        cursor = self.conn.cursor()
        query = "SELECT * FROM jobs WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if country:
            query += " AND country = ?"
            params.append(country)
        if resume_match:
            query += " AND resume_match = ?"
            params.append(resume_match)
        if min_score is not None:
            query += " AND match_score >= ?"
            params.append(min_score)

        query += " ORDER BY match_score DESC, date_found DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        jobs = []
        for r in rows:
            job = dict(r)
            job["matched_skills"] = json.loads(r["matched_skills"]) if r["matched_skills"] else []
            jobs.append(job)
        return jobs

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
        
        # Average match score
        cursor.execute("SELECT AVG(match_score) FROM jobs WHERE match_score > 0")
        avg = cursor.fetchone()[0]
        stats["avg_match_score"] = round(avg, 1) if avg else 0
        
        # Counts by country
        cursor.execute("SELECT country, COUNT(*) as count FROM jobs GROUP BY country")
        stats["country_counts"] = {r["country"]: r["count"] for r in cursor.fetchall() if r["country"]}
        
        # Counts by resume matching
        cursor.execute("SELECT resume_match, COUNT(*) as count FROM jobs GROUP BY resume_match")
        stats["resume_counts"] = {r["resume_match"]: r["count"] for r in cursor.fetchall() if r["resume_match"]}
        
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
        from backend.scheduler import CellState

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
        "company_num_employees", "company_industry", "scrape_cell_id", "sync_run_id",
        "taxonomy_hash", "match_score", "matched_skills", "resume_match",
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
            if updatable:
                cursor.execute(
                    f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in updatable)} "
                    "WHERE id = ?",
                    [record[c] for c in updatable] + [job_id],
                )
                self.conn.commit()
            return job_id, False

        columns = [c for c in self.POSTING_COLUMNS if c in record]
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
