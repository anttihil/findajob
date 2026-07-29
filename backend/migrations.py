"""Versioned SQLite schema migrations, tracked via PRAGMA user_version.

Each migration is a (version, description, callable) triple applied in order inside a
transaction. Adding a migration means appending to MIGRATIONS -- never editing an existing
one, since it may already have run against a live database.
"""

import sqlite3

from backend.logger import get_logger

logger = get_logger()

SCHEMA_VERSION = 2


def _v1_baseline(cursor):
    """Create the original jobs table if this is a fresh database.

    Existing databases already have this table (created by Database.create_tables), so the
    IF NOT EXISTS makes v1 a no-op for them and they jump straight to v2.
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT UNIQUE,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            country TEXT,
            url TEXT UNIQUE,
            description TEXT,
            source TEXT,
            match_score INTEGER DEFAULT 0,
            matched_skills TEXT,
            resume_match TEXT,
            status TEXT DEFAULT 'unread',
            date_found TEXT,
            date_applied TEXT
        )
        """
    )


# Columns added to `jobs` in v2. Kept as data so the migration is idempotent against
# databases that may have been partially migrated by an interrupted run.
_V2_JOB_COLUMNS = [
    # --- normalization ---
    ("role_family", "TEXT"),  # re-derived from the TITLE, never trusted from the query
    ("role_family_hint", "TEXT"),  # which cell's query surfaced it; provenance only
    ("seniority", "TEXT"),  # junior|mid|senior|staff|unspecified
    ("is_remote", "INTEGER"),  # 1|0|NULL (NULL = could not determine)
    ("city", "TEXT"),
    ("region", "TEXT"),
    # --- recency ---
    ("date_posted", "TEXT"),  # real date when the board provides one
    ("date_precision", "TEXT DEFAULT 'unknown'"),  # exact|interval|unknown
    ("posted_window_start", "TEXT"),
    ("posted_window_end", "TEXT"),
    # --- salary, normalized to annual USD for comparison ---
    ("salary_min", "REAL"),
    ("salary_max", "REAL"),
    ("salary_currency", "TEXT"),
    ("salary_interval", "TEXT"),
    ("salary_annual_usd", "REAL"),
    ("salary_currency_inferred", "INTEGER DEFAULT 0"),
    ("salary_source", "TEXT"),
    # --- corpus hygiene / eligibility ---
    ("description_quality", "TEXT DEFAULT 'unknown'"),  # full|snippet|missing
    ("desc_selection", "TEXT DEFAULT 'none'"),  # census|top_k|none -- see note below
    ("content_hash", "TEXT"),
    ("duplicate_of", "INTEGER"),
    ("is_agency", "INTEGER DEFAULT 0"),
    ("company_normalized", "TEXT"),
    ("company_num_employees", "TEXT"),
    ("company_industry", "TEXT"),
    # --- provenance ---
    ("site_job_id", "TEXT"),  # the board's own id, e.g. 'in-22cfed37fc0b9a12'
    ("url_direct", "TEXT"),
    ("sync_run_id", "INTEGER"),
    ("scrape_cell_id", "INTEGER"),
    ("taxonomy_hash", "TEXT"),
]

# `desc_selection` is load-bearing for statistical correctness, not just bookkeeping.
# Indeed returns descriptions in-response, so every posting in a cell gets one -> 'census'.
# LinkedIn charges an extra request per description, so only the top-scoring subset can be
# fetched -> 'top_k'. Pre-score correlates with the user's own skills, so pooling top_k rows
# into skill-demand denominators would inflate demand for skills the user already has. Skill
# analytics therefore accept 'census' only. See v_skill_eligible below.


def _v2_analytics(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    for name, decl in _V2_JOB_COLUMNS:
        if name not in existing:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")

    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_family_found
            ON jobs(role_family, date_found);
        CREATE INDEX IF NOT EXISTS idx_jobs_content_hash
            ON jobs(content_hash);
        CREATE INDEX IF NOT EXISTS idx_jobs_site_job_id
            ON jobs(site_job_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_run
            ON jobs(sync_run_id);

        -- Canonical skill mentions, one row per (job, skill). Presence is the signal;
        -- within-posting frequency is noise, so there is no count column.
        CREATE TABLE IF NOT EXISTS job_skills (
            job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            skill    TEXT NOT NULL,
            in_title INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (job_id, skill)
        );
        CREATE INDEX IF NOT EXISTS idx_job_skills_skill ON job_skills(skill);

        -- Informational tags (e.g. "requires EU work authorization"). Weight 0 -- these
        -- describe a posting, they do not score it.
        CREATE TABLE IF NOT EXISTS job_blockers (
            job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            blocker TEXT NOT NULL,
            PRIMARY KEY (job_id, blocker)
        );

        -- One row per (source, role_family, location, query) search cell. The scheduler
        -- rotates through these rather than sweeping the whole matrix every run.
        CREATE TABLE IF NOT EXISTS scrape_cells (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            role_family TEXT NOT NULL,
            location_id TEXT NOT NULL,
            query       TEXT NOT NULL,
            tier        TEXT NOT NULL,          -- core|adjacent|breadth
            enabled     INTEGER NOT NULL DEFAULT 1,

            -- INVARIANT: last_scraped_at advances on EVERY attempt (so priority backs off
            -- a broken cell), last_success_at only on success (so the staleness floor and
            -- adaptive hours_old are not fooled into reporting coverage that never happened).
            last_scraped_at   TEXT,
            last_success_at   TEXT,
            last_requested    INTEGER,
            last_result_count INTEGER,
            last_new_count    INTEGER,
            last_saturated    INTEGER,
            last_hours_old    INTEGER,

            ewma_new_per_scrape REAL,
            ewma_yield_per_day  REAL,

            consecutive_empty INTEGER NOT NULL DEFAULT 0,
            consecutive_error INTEGER NOT NULL DEFAULT 0,
            total_scrapes     INTEGER NOT NULL DEFAULT 0,
            total_postings    INTEGER NOT NULL DEFAULT 0,
            backoff_until     TEXT,
            last_error        TEXT,

            created_at TEXT NOT NULL,
            UNIQUE (source, role_family, location_id, query)
        );
        CREATE INDEX IF NOT EXISTS idx_cells_sched
            ON scrape_cells(source, enabled, backoff_until, last_scraped_at);

        -- Per-source circuit breaker state, persisted so a 429 survives process restarts.
        CREATE TABLE IF NOT EXISTS source_state (
            source            TEXT PRIMARY KEY,
            backoff_until     TEXT,
            consecutive_trips INTEGER NOT NULL DEFAULT 0,
            last_trip_at      TEXT,
            last_trip_reason  TEXT,
            total_429         INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            mode   TEXT,                          -- backfill|incremental|dry_run
            status TEXT DEFAULT 'running',        -- running|ok|partial|failed
            cells_planned    INTEGER DEFAULT 0,
            cells_succeeded  INTEGER DEFAULT 0,
            cells_skipped    INTEGER DEFAULT 0,
            postings_fetched INTEGER DEFAULT 0,
            postings_new     INTEGER DEFAULT 0,
            duplicates_merged INTEGER DEFAULT 0,
            taxonomy_hash TEXT,
            plan_hash     TEXT,
            llm_cost_usd  REAL DEFAULT 0,
            error_summary TEXT
        );

        -- The sampling denominator. Without this table every supply and demand figure is
        -- an unfalsifiable assertion; with it, each one is auditable.
        CREATE TABLE IF NOT EXISTS cell_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
            cell_id     INTEGER REFERENCES scrape_cells(id),
            source      TEXT NOT NULL,
            role_family TEXT NOT NULL,
            location_id TEXT NOT NULL,
            query       TEXT NOT NULL,
            observed_at   TEXT NOT NULL,
            hours_old     INTEGER,
            window_start  TEXT,                  -- observed_at - hours_old
            window_end    TEXT,                  -- observed_at
            requested         INTEGER NOT NULL DEFAULT 0,
            returned          INTEGER NOT NULL DEFAULT 0,
            -- returned counts what the board gave us, and drives saturation.
            -- returned_on_topic counts rows whose TITLE maps to a role family, and drives
            -- flow. Indeed returns substantial off-target results (a 'Platform Engineer'
            -- query returned a Maintenance Technician), so conflating these two inflates
            -- every supply number.
            returned_on_topic INTEGER NOT NULL DEFAULT 0,
            new_unique        INTEGER NOT NULL DEFAULT 0,
            saturated         INTEGER NOT NULL DEFAULT 0,
            desc_selection    TEXT NOT NULL DEFAULT 'none',
            descriptions_full INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,                -- ok|empty|error|skipped
            error  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cellobs_scope
            ON cell_observations(role_family, location_id, source, observed_at);
        CREATE INDEX IF NOT EXISTS idx_cellobs_run
            ON cell_observations(sync_run_id);

        -- Materialized per-window skill statistics. taxonomy_hash and reference_mix_hash
        -- are what stop a trend line from silently comparing numbers produced under two
        -- different methodologies.
        CREATE TABLE IF NOT EXISTS skill_market_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            scope       TEXT NOT NULL,           -- 'all' or 'family:x' / 'location:y'
            skill       TEXT NOT NULL,
            demand            REAL,              -- post-stratified estimate
            demand_unweighted REAL,              -- diagnostic: shows the rotation bias
            demand_ci_low     REAL,
            demand_ci_high    REAL,
            n_eff             REAL,              -- Kish effective sample size; CIs use this
            n_raw             INTEGER,
            n_companies       INTEGER,
            max_company_share REAL,
            missing_weight    REAL,
            saturated_share   REAL,
            blocking_gap REAL,
            adjacency    REAL,
            salary_lift  REAL,
            priority     REAL,
            user_has     INTEGER,
            suppressed_reason TEXT,              -- suppress WITH a reason, never omit
            weighting_mode     TEXT,
            reference_mix_hash TEXT,
            taxonomy_hash      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sms_lookup
            ON skill_market_stats(window_days, scope, skill, computed_at);

        CREATE TABLE IF NOT EXISTS role_market_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            role_family TEXT NOT NULL,
            location_id TEXT NOT NULL,
            source      TEXT NOT NULL,
            flow REAL,                           -- postings/day; the headline metric
            censored INTEGER DEFAULT 0,          -- saturated => flow is a LOWER BOUND
            covered_days      REAL,              -- interval UNION, never a sum of hours_old
            coverage_fraction REAL,
            saturated_share   REAL,
            n_observations    INTEGER,
            n_postings        INTEGER,           -- diagnostic only, never a headline
            n_companies       INTEGER,
            remote_share      REAL,
            median_salary_usd REAL,
            comparability_class TEXT,
            suppressed_reason   TEXT,
            plan_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rms_lookup
            ON role_market_stats(window_days, location_id, source, role_family);

        -- Unmatched capitalized n-grams, so the taxonomy grows from observed data rather
        -- than guesswork. Reviewing this is a manual step; nothing is auto-promoted.
        CREATE TABLE IF NOT EXISTS skill_candidates (
            term        TEXT PRIMARY KEY,
            n_postings  INTEGER NOT NULL DEFAULT 0,
            first_seen  TEXT,
            last_seen   TEXT,
            status      TEXT NOT NULL DEFAULT 'new'  -- new|accepted|rejected
        );

        DROP VIEW IF EXISTS v_supply_eligible;
        DROP VIEW IF EXISTS v_skill_eligible;

        -- Role supply: title/company/location are complete for every row a board returns,
        -- so description selection is irrelevant. LinkedIn counts here.
        CREATE VIEW v_supply_eligible AS
        SELECT * FROM jobs
        WHERE sync_run_id IS NOT NULL
          AND duplicate_of IS NULL
          AND role_family IS NOT NULL;

        -- Skill demand: requires a census of descriptions within the cell, so Indeed only.
        CREATE VIEW v_skill_eligible AS
        SELECT * FROM jobs
        WHERE sync_run_id IS NOT NULL
          AND duplicate_of IS NULL
          AND role_family IS NOT NULL
          AND description_quality = 'full'
          AND desc_selection = 'census';
        """
    )

    # The 121 pre-existing rows came from JobTech Sweden / HackerNews / WeWorkRemotely API
    # scrapers and do carry full descriptions (measured: min 484, avg 2942, max 6540 chars).
    # They are legitimately 'full'/'census', but they have no cell_observations provenance,
    # so sync_run_id IS NULL keeps them out of both eligibility views without special-casing.
    cursor.execute(
        """
        UPDATE jobs
           SET description_quality = CASE
                   WHEN description IS NULL OR LENGTH(description) = 0 THEN 'missing'
                   WHEN LENGTH(description) < 400 THEN 'snippet'
                   ELSE 'full'
               END,
               desc_selection = 'census'
         WHERE description_quality IS NULL OR description_quality = 'unknown'
        """
    )


MIGRATIONS = [
    (1, "baseline jobs table", _v1_baseline),
    (2, "market analytics: cells, observations, skills, stats", _v2_analytics),
]


def apply_pragmas(conn):
    """WAL so a 20-minute scrape does not block dashboard reads."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def current_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn):
    """Apply pending migrations. Returns the number applied."""
    apply_pragmas(conn)
    version = current_version(conn)
    applied = 0

    for target, description, fn in MIGRATIONS:
        if target <= version:
            continue
        logger.info(f"Applying migration v{target}: {description}")
        cursor = conn.cursor()
        try:
            fn(cursor)
            # PRAGMA user_version does not accept a bound parameter.
            cursor.execute(f"PRAGMA user_version = {int(target)}")
            conn.commit()
            applied += 1
        except Exception:
            conn.rollback()
            logger.error(f"Migration v{target} failed and was rolled back", exc_info=True)
            raise

    if applied:
        logger.info(f"Schema now at v{current_version(conn)} ({applied} migration(s) applied)")
    return applied


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.database import DB_PATH

    connection = sqlite3.connect(DB_PATH)
    try:
        migrate(connection)
        print(f"schema version: {current_version(connection)}")
    finally:
        connection.close()
