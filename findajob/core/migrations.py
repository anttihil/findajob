"""Versioned SQLite schema migrations, tracked via PRAGMA user_version.

Each migration is a (version, description, callable) triple applied in order inside a
transaction. Adding a migration means appending to MIGRATIONS -- never editing an existing
one, since it may already have run against a live database.
"""

import contextlib
import sqlite3
from collections.abc import Callable

from findajob.core.logger import get_logger

logger = get_logger()

SCHEMA_VERSION = 30


def _v1_baseline(cursor: sqlite3.Cursor) -> None:
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
    ("role_family", "TEXT"),  # re-derived from the TITLE, never trusted from the query
    ("role_family_hint", "TEXT"),  # which cell's query surfaced it; provenance only
    ("seniority", "TEXT"),  # junior|mid|senior|staff|unspecified
    ("is_remote", "INTEGER"),  # 1|0|NULL (NULL = could not determine)
    ("city", "TEXT"),
    ("region", "TEXT"),
    ("date_posted", "TEXT"),  # real date when the board provides one
    ("date_precision", "TEXT DEFAULT 'unknown'"),  # exact|interval|unknown
    ("posted_window_start", "TEXT"),
    ("posted_window_end", "TEXT"),
    ("salary_min", "REAL"),
    ("salary_max", "REAL"),
    ("salary_currency", "TEXT"),
    ("salary_interval", "TEXT"),
    ("salary_annual_usd", "REAL"),
    ("salary_currency_inferred", "INTEGER DEFAULT 0"),
    ("salary_source", "TEXT"),
    ("description_quality", "TEXT DEFAULT 'unknown'"),  # full|snippet|missing
    ("desc_selection", "TEXT DEFAULT 'none'"),  # census|top_k|none -- see note below
    ("content_hash", "TEXT"),
    ("duplicate_of", "INTEGER"),
    ("is_agency", "INTEGER DEFAULT 0"),
    ("company_normalized", "TEXT"),
    ("company_num_employees", "TEXT"),
    ("company_industry", "TEXT"),
    ("site_job_id", "TEXT"),  # the board's own id, e.g. 'in-22cfed37fc0b9a12'
    ("url_direct", "TEXT"),
    ("sync_run_id", "INTEGER"),
    ("scrape_cell_id", "INTEGER"),
    # taxonomy_hash recorded which skills.yaml classified a posting. The keyword
    # taxonomy is gone and nothing has written the column since. Dropped in v27.
    ("taxonomy_hash", "TEXT"),
]

# `desc_selection` is load-bearing for statistical correctness, not just bookkeeping.
# Indeed returns descriptions in-response, so every posting in a cell gets one -> 'census'.
# LinkedIn charges an extra request per description, so only the top-scoring subset can be
# fetched -> 'top_k'. Pre-score correlates with the user's own skills, so pooling top_k rows
# into skill-demand denominators would inflate demand for skills the user already has. Skill
# analytics therefore accept 'census' only. See v_skill_eligible below.


def _v2_analytics(cursor: sqlite3.Cursor) -> None:
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
            taxonomy_hash TEXT,               -- keyword-taxonomy leftover; dropped in v27
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
            -- returned_on_topic held a title-based on-topic count. The classifier that
            -- produced it is gone; nothing writes the column now. Dropped in v24.
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


def _v3_llm_verdict(cursor: sqlite3.Cursor) -> None:
    """Store the optional Claude reranker's verdict alongside the deterministic score.

    Kept separate from match_score on purpose: the deterministic score stays reproducible,
    and a run without the LLM stage produces the same ranking as before.
    """
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    if "llm_verdict" not in existing:
        cursor.execute("ALTER TABLE jobs ADD COLUMN llm_verdict TEXT")


# Location ids were originally abbreviated. "la" reads as Louisiana rather than Los Angeles,
# and two-letter ids collide with US state codes generally, so they are spelled out. The
# mapping is applied to every table that stores a location_id.
_V4_LOCATION_RENAMES = {
    "la": "los_angeles",
    "fi": "helsinki",
    "se": "stockholm",
    "no": "oslo",
    "dk": "copenhagen",
}


def _v4_access_and_location_ids(cursor: sqlite3.Cursor) -> None:
    """Add posting-level `access` and spell out abbreviated location ids.

    `access` (commutable | remote | relocation) is the distinction that governs whether a
    posting is worth reading at all: the user lives in Los Angeles, so anything neither
    remote nor within commuting distance requires relocating. It is stored per posting
    rather than derived from the search, because a nationwide or remote-flagged search
    routinely returns roles that happen to sit in the LA basin.
    """
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    if "access" not in existing:
        cursor.execute("ALTER TABLE jobs ADD COLUMN access TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_access ON jobs(access)")

    for old, new in _V4_LOCATION_RENAMES.items():
        for table in ("scrape_cells", "cell_observations", "role_market_stats"):
            # Table may not exist on a partially-migrated database.
            with contextlib.suppress(Exception):
                cursor.execute(
                    f"UPDATE {table} SET location_id = ? WHERE location_id = ?",
                    (new, old),
                )
    # skill_market_stats stores scope strings like 'location:la'.
    for old, new in _V4_LOCATION_RENAMES.items():
        with contextlib.suppress(Exception):
            cursor.execute(
                "UPDATE skill_market_stats SET scope = ? WHERE scope = ?",
                (f"location:{new}", f"location:{old}"),
            )


def _v5_agentic(cursor: sqlite3.Cursor) -> None:
    """Replace the single-shot scoring columns with the four-stage agentic pipeline.

    The old shape assumed one pass: scrape, score, store, done. `sync.py` computed a
    keyword score inline and optionally appended an LLM verdict into a JSON blob on the
    same row. Stages now run on separate timers and hand off through `jobs.pipeline_state`,
    so a posting's progress has to be a queryable column rather than an implicit
    consequence of which script last touched it.

    Dropped columns:

      llm_verdict   a JSON blob holding one unversioned verdict. Superseded by
                    `job_verdicts`, which keys on `profile_version` so a profile rebuild
                    can invalidate and re-score without losing the audit trail, and which
                    records per-call token usage and cost.

      resume_match  a denormalized copy of `roles.resume_for(role_family)` -- a pure
                    function of a column already on the row. Callers derive it instead;
                    nothing about which resume to send has changed.

    `match_score` and `matched_skills` deliberately stay. They are keyword coverage, not
    judgment, and `gap_analysis` still measures its blocking gap against postings the user
    matches at GOOD_FIT_THRESHOLD or better. The LLM's `fit_score` is a second, separate
    signal that drives dashboard ranking; conflating the two would silently change what the
    skill-gap charts mean.
    """
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}

    for column, ddl in (
        # NOT NULL needs a default for ALTER TABLE ADD COLUMN, and 'new' is the right one:
        # every pre-existing posting is unscored under the new regime.
        ("pipeline_state", "TEXT NOT NULL DEFAULT 'new'"),
        ("fit_score", "INTEGER"),
        ("scored_at", "TEXT"),
        ("dossier_id", "INTEGER"),
    ):
        if column not in existing:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_state ON jobs(pipeline_state, fit_score DESC)"
    )

    for column in ("llm_verdict", "resume_match"):
        if column in existing:
            cursor.execute(f"ALTER TABLE jobs DROP COLUMN {column}")

    # The corpus predates the pipeline, so everything queues for a first scoring pass.
    cursor.execute("UPDATE jobs SET pipeline_state = 'new' WHERE pipeline_state IS NULL")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            model TEXT,
            -- The full structured profile: skills with evidence, strengths, weaknesses,
            -- constraints, preferences, non-negotiables.
            profile_json TEXT NOT NULL,
            -- The rendered prompt prefix, frozen at approval time. Rendering it per request
            -- would let dict ordering drift and silently break DeepSeek's prefix cache,
            -- which is a 50x input-cost difference. See docs/deepseek.md.
            summary_text TEXT NOT NULL,
            -- Combined hash of the source documents, so `profile build` can tell whether
            -- the corpus actually changed before proposing a rebuild.
            corpus_hash TEXT
        )
        """
    )
    # One active profile, enforced by the database rather than by convention.
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_one_active "
        "ON profiles(is_active) WHERE is_active = 1"
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_version INTEGER NOT NULL,
            path TEXT NOT NULL,
            kind TEXT,
            sha256 TEXT NOT NULL,
            chars INTEGER,
            ingested_at TEXT NOT NULL,
            UNIQUE(profile_version, path)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_version INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            topic TEXT,
            question TEXT NOT NULL,
            answer TEXT,
            asked_at TEXT,
            UNIQUE(profile_version, seq)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            profile_version INTEGER NOT NULL,
            model TEXT NOT NULL,
            fit_score INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            seniority_fit TEXT,
            -- JSON arrays. Blockers quote the phrase from the posting that makes them one,
            -- so a verdict can be checked against its evidence rather than trusted.
            hard_blockers TEXT,
            key_gaps TEXT,
            strengths TEXT,
            reasoning TEXT,
            research_worthy INTEGER DEFAULT 0,
            tokens_in INTEGER,
            tokens_cached INTEGER,
            tokens_out INTEGER,
            cost_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            -- Re-scoring under the same profile replaces the verdict; a new profile version
            -- adds one, so you can diff how a profile change moved the rankings.
            UNIQUE(job_id, profile_version)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_verdicts_score ON job_verdicts(fit_score DESC)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS company_dossiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Keyed on the normalized name, not the posting: twelve good hits at one
            -- company must cost one dossier, not twelve.
            company_normalized TEXT NOT NULL UNIQUE,
            company_display TEXT,
            generated_at TEXT NOT NULL,
            profile_version INTEGER,
            model TEXT,
            intel_json TEXT,
            contacts_json TEXT,
            nearby_jobs_json TEXT,
            -- Every claim in a dossier is web-sourced, so the citations travel with it.
            sources_json TEXT,
            cost_usd REAL DEFAULT 0
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS research_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            companies INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            status TEXT DEFAULT 'running',
            error TEXT
        )
        """
    )


def _v6_ordinal_verdicts(cursor: sqlite3.Cursor) -> None:
    """Store the scoring agent's answers, not just the number derived from them.

    The previous schema kept `fit_score INTEGER` and a band label. Measured across 5,511
    verdicts the model used 53 distinct values, 99.84% of which agreed with the band the
    prompt's own table assigned -- inside `worth_applying`, the single value 62 accounted
    for 45% of the band. The number was a re-encoding of the label, and storing only it
    threw away the reasoning that produced it.

    Now the model answers five named scales and `scoring/scale.py` projects them onto a
    score. Keeping the ordinals in columns is what makes that projection cheap to change:
    `findajob score rescale` recomputes every row without an API call, and the UI can
    sort, filter and cross-tab on any dimension instead of on one collapsed number.

    Existing verdicts are KEPT, at `scale_version = 0`. They are the only cross-version
    evidence in the database -- the profile v3-vs-v4 comparison that measured 16.7 points
    of mean drift came from them -- and they cost real money to produce. `scale_version`
    makes them unmistakable, and `scoring/stats.py` refuses to render a mixed histogram
    rather than averaging two incomparable scales together.

    On `jobs`, three of the new columns are about staleness rather than scoring. Posting
    liveness was previously unknowable: `sync_run_id` is overwritten on every re-find and
    nothing recorded when a posting was last actually seen, so "closed" and "that cell has
    not been scraped since" were indistinguishable. Measured on the three cells scraped in
    both of the first two windows without saturating, 36 of 71 postings from the earlier
    cohort reappeared six days later -- a ~6-day half-life. Scoring a corpus without
    knowing that means paying to rank dead postings at the top of the dashboard.
    """
    verdict_columns = {row[1] for row in cursor.execute("PRAGMA table_info(job_verdicts)")}

    for column, ddl in (
        ("role_summary", "TEXT"),
        ("eligibility", "TEXT"),
        ("role_match", "TEXT"),
        ("capability_match", "TEXT"),
        ("seniority_gap", "TEXT"),
        ("evidence_quality", "TEXT"),
        # JSON. The extraction the ordinals were derived from, kept so a verdict can be
        # checked against its own working rather than trusted.
        ("core_requirements", "TEXT"),
        ("requirement_assessments", "TEXT"),
        ("audit_flags", "TEXT"),
        # 0 means "the model emitted the number itself" -- the pre-v6 regime.
        ("scale_version", "INTEGER NOT NULL DEFAULT 0"),
        ("verdict_schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ("pareto_tier", "INTEGER"),
        # Identifies the rules a verdict was produced under, so a distribution shift can be
        # attributed to a prompt edit instead of argued about.
        ("prompt_hash", "TEXT"),
    ):
        if column not in verdict_columns:
            cursor.execute(f"ALTER TABLE job_verdicts ADD COLUMN {column} {ddl}")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdicts_ordinals "
        "ON job_verdicts(profile_version, eligibility, pareto_tier)"
    )

    job_columns = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    for column, ddl in (
        # Coverage as a fraction with its denominator, rather than a percent that was never
        # one: `match_score` has ~23 points of unearned floor and its observed range across
        # 5,932 postings was 15-80, so "15%" meant "nothing matched", not "a poor match".
        ("matched_count", "INTEGER"),
        ("required_count", "INTEGER"),
        # Which scorer wrote match_score. 121 rows still carry display labels in
        # `matched_skills` instead of canonical keys because an older scorer wrote them and
        # nothing recorded the fact; staleness becomes queryable instead of inferred from
        # the shape of a JSON blob.
        ("scorer_version", "INTEGER NOT NULL DEFAULT 0"),
        ("last_seen_at", "TEXT"),
        ("times_seen", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if column not in job_columns:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")

    # Backfill last_seen_at from the run that last touched each row. Approximate -- the run
    # timestamp, not the observation -- but it is strictly better than NULL, and every row
    # scraped from here on records the real thing.
    cursor.execute(
        """
        UPDATE jobs SET last_seen_at = COALESCE(
            (SELECT r.started_at FROM sync_runs r WHERE r.id = jobs.sync_run_id),
            date_found)
        WHERE last_seen_at IS NULL
        """
    )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at)")

    # Liveness cannot be a stored column: it depends on when the posting's CELL was last
    # scraped, which changes without the posting row changing.
    #
    # Three states, and the third one is the point. Absence from a scrape only means
    # "closed" if we actually looked; with ~280 of 410 cells rotating per day, most postings
    # are simply unobserved, and calling those closed would delete the corpus. The grace
    # window absorbs the ordering skew between when a run starts (which is what
    # `last_seen_at` records for backfilled rows) and when a cell reports success.
    cursor.execute("DROP VIEW IF EXISTS v_job_liveness")
    cursor.execute(
        """
        CREATE VIEW v_job_liveness AS
        SELECT j.id AS job_id,
               j.last_seen_at,
               c.last_success_at AS cell_last_success_at,
               CASE
                 WHEN j.last_seen_at IS NULL OR c.last_success_at IS NULL THEN 'unknown'
                 -- The cell was scraped well after we last saw this posting, and it did
                 -- not come back. That is the only case where absence is evidence.
                 WHEN unixepoch(c.last_success_at) - unixepoch(j.last_seen_at) > 43200
                      THEN 'likely_closed'
                 -- Nobody has looked at this cell in over a week, so we know nothing
                 -- current about it either way.
                 WHEN unixepoch('now') - unixepoch(c.last_success_at) > 604800 THEN 'stale'
                 ELSE 'live'
               END AS liveness
        FROM jobs j
        LEFT JOIN scrape_cells c ON c.id = j.scrape_cell_id
        """
    )


def _v7_structured_blockers(cursor: sqlite3.Cursor) -> None:
    """Give a stored hard blocker the same shape the model now emits: quote plus why.

    `hard_blockers` was a JSON array of strings that each had to be a verbatim posting
    quote and an explanation at once. The auditor checks the quote, so a blocker whose
    text was mostly explanation failed the check and its verdict was discarded -- 19 of
    the 33 distinct blockers that lost a verdict in one run were correct judgements
    written as prose about the candidate's own constraints. `profile/models.py` splits the
    field; this brings the rows already on disk along.

    Each old string becomes `{"quote": <the string>, "why": ""}`. Nothing is lost: the
    string is preserved exactly, and it lands in the field whose contract it was already
    written against. `findajob score audit` therefore reports the same quote-status
    rates over migrated rows as it did before, which is what keeps the trend line readable
    across the change.

    VERDICT_SCHEMA_VERSION is deliberately NOT bumped. That constant re-drains the backlog
    through the scoring worker, and re-scoring ~5,500 verdicts costs real money to buy
    ordinals that are already correct -- only the blocker representation changed, and it
    changed here rather than by asking the model again.
    """
    import json

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(job_verdicts)")}
    if "hard_blockers" not in columns:
        return

    converted = 0
    updates = []
    for row_id, raw in cursor.execute(
        "SELECT id, hard_blockers FROM job_verdicts "
        "WHERE hard_blockers IS NOT NULL AND hard_blockers NOT IN ('', '[]')"
    ).fetchall():
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            # Not JSON at all -- an early row that stored one blocker as a bare sentence.
            # It is still a blocker, so carry it over rather than dropping it.
            decoded = [raw]
        if not isinstance(decoded, list):
            decoded = [str(decoded)]
        if all(isinstance(item, dict) for item in decoded):
            continue
        rebuilt = [
            item if isinstance(item, dict) else {"quote": str(item), "why": ""} for item in decoded
        ]
        updates.append((json.dumps(rebuilt), row_id))
        converted += 1

    cursor.executemany("UPDATE job_verdicts SET hard_blockers = ? WHERE id = ?", updates)
    if converted:
        logger.info("v7: converted hard_blockers on %d verdict rows", converted)


def _v8_scoring_failure_counter(cursor: sqlite3.Cursor) -> None:
    """Give a posting a memory of its own scoring failures.

    The retry design assumes failure is transient -- a rate limit, or the model answering
    in prose where a tool call was required. Both get better on another draw, so the graph
    retries three times and the queue re-offers the posting on the next run.

    Some failures are not draws. Job 5802 is a Czech news item about a research project's
    advisory board convening; it is not a job ad, so `core_requirements` extracts empty,
    so the validator in `profile/models.py` rejects it -- correctly, and identically, on
    every attempt of every run. Three calls a run, forever, with no record anywhere: the
    posting keeps whatever `pipeline_state` it had, no verdict row appears, and the only
    evidence is a warning in app.log.

    These columns are that record. The worker increments on failure and clears on success,
    and `_select` stops offering a posting once the count says the next attempt will fail
    the same way. That turns an unbounded cost into a bounded, reportable category -- the
    same treatment duplicates and stub descriptions already get.

    Not a verdict row, deliberately. A failure is the absence of a verdict; writing a
    placeholder into `job_verdicts` would put rows with no ordinals into every query that
    reads the table, and `score stats` and `score audit` would both have to learn to skip
    them.
    """
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    if "scoring_failures" not in existing:
        cursor.execute("ALTER TABLE jobs ADD COLUMN scoring_failures INTEGER NOT NULL DEFAULT 0")
    if "last_scoring_error" not in existing:
        cursor.execute("ALTER TABLE jobs ADD COLUMN last_scoring_error TEXT")
    if "last_scoring_failure_at" not in existing:
        cursor.execute("ALTER TABLE jobs ADD COLUMN last_scoring_failure_at TEXT")


def _v9_feed_indexes(cursor: sqlite3.Cursor) -> None:
    """Indexes for the two lookups the dashboard does on every render.

    The feed's WHERE is `status = ? AND duplicate_of IS NULL` and its tie-breaker is
    `date_found DESC`, and nothing indexed any of the three: 5,932 rows were scanned and
    5,643 survivors sorted in a temp B-tree to return a page of 50. The composite covers
    the filter and lets the sort read in order.

    `company_display` is indexed because the dossier lookup matches either name. The
    normalized column already had a unique index and the display column had none, so an
    `OR` across the pair could not use either and scanned the table; with both indexed the
    lookup splits into two indexed probes (see `web/app.py:dossier_for`).
    """
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_feed ON jobs(status, duplicate_of, date_found DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dossiers_display ON company_dossiers(company_display)"
    )


def _v10_cell_quality_ewma(cursor: sqlite3.Cursor) -> None:
    """Give scrape cells a memory of the LLM score their postings actually earned.

    `cell_priority()` already weights cells by `ewma_new_per_scrape` -- how many postings
    a cell yields -- but nothing fed back how GOOD those postings turned out to be, so a
    cell producing lots of poorly-matched postings ranked identically to one producing
    fewer, consistently strong ones. `ewma_fit_score` closes that gap, updated from
    `job_verdicts.fit_score` at scoring time (scoring/worker.py) via `jobs.scrape_cell_id`
    rather than at scrape time, since verdicts land on their own faster cadence.

    `quality_samples` gates when the new signal is trusted: `cell_priority()` treats a
    cell as quality-neutral until it clears a small sample threshold, so a cell doesn't
    get deprioritized off one or two verdicts before it's had a fair look -- the same
    exploration-vs-exploitation concern `novelty`/`DEAD_PENALTY_FLOOR` already guard for
    the yield signal.
    """
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(scrape_cells)")}
    if "ewma_fit_score" not in existing:
        cursor.execute("ALTER TABLE scrape_cells ADD COLUMN ewma_fit_score REAL")
    if "quality_samples" not in existing:
        cursor.execute(
            "ALTER TABLE scrape_cells ADD COLUMN quality_samples INTEGER NOT NULL DEFAULT 0"
        )


def _v11_cell_cost(cursor: sqlite3.Cursor) -> None:
    """Record what a cell visit actually cost: wall time and HTTP requests.

    Both were previously only estimable after the fact -- duration from the gap between
    consecutive observed_at values (which includes the pacing sleep and the scoring loop),
    and requests from `returned / page_size + descriptions_full`, which is the planner's
    cost model rather than a measurement. Estimating the cost from the model and then
    tuning the model against those numbers is circular: `est_request_units` decides the
    per-run budget (scheduler.py), so it has to be checkable against something it did not
    produce itself.

    Both columns stay NULL for rows written before this migration, because 0 requests in
    0 ms is a claim, and an unmeasured cell has not made one.

    The `cell_cost` view exists so ad-hoc analysis is one SELECT rather than a rediscovery
    of which columns are counts and which are estimates.
    """
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(cell_observations)")}
    if "duration_ms" not in existing:
        cursor.execute("ALTER TABLE cell_observations ADD COLUMN duration_ms INTEGER")
    if "requests_made" not in existing:
        cursor.execute("ALTER TABLE cell_observations ADD COLUMN requests_made INTEGER")

    cursor.execute("DROP VIEW IF EXISTS cell_cost")
    cursor.execute(
        """
        CREATE VIEW cell_cost AS
        SELECT
            o.id,
            o.sync_run_id,
            o.cell_id,
            o.source,
            o.role_family,
            o.location_id,
            o.query,
            o.observed_at,
            o.status,
            o.desc_selection,
            o.requested,
            o.returned,
            o.returned_on_topic,
            o.new_unique,
            o.descriptions_full,
            o.duration_ms,
            o.requests_made,
            o.duration_ms / 1000.0                              AS seconds,
            1.0 * o.duration_ms / NULLIF(o.requests_made, 0)    AS ms_per_request,
            o.duration_ms / 1000.0 / NULLIF(o.returned, 0)      AS seconds_per_posting,
            1.0 * o.requests_made / NULLIF(o.returned, 0)       AS requests_per_posting
        -- Measurements and ratios of measurements only. The planner's estimate of the same
        -- cost lives in scheduler.estimate_units and reads page_size from config.yaml;
        -- restating it here in SQL would let the two drift, and the whole point of these
        -- columns is to be checkable against the model rather than derived from it.
        FROM cell_observations o
        """
    )


def _v12_liveness_window(cursor: sqlite3.Cursor) -> None:
    """Call a posting closed only when the re-scrape could have returned it.

    v6's rule was "the cell was scraped after we last saw this posting, and it did not
    come back". That misses half the question. A cell's search carries `hours_old`, which
    filters on `date_posted`, so a posting older than that window CANNOT come back whether
    it is open or closed. The rule was measuring age, not absence.

    Measured on the production corpus (18,698 postings, 2026-08-15), splitting on whether
    `date_posted` fell inside the cell's most recent `hours_old` window:

        inside window    7,508 postings    8.6% called likely_closed
        outside window  11,069 postings   89.5% called likely_closed

    Inside the window, where the test is valid, 8.6% of postings disappear -- ordinary
    turnover. Outside it the flag fires on nearly everything, which is what a filter
    measuring "older than the recency window" looks like. Of the 8,948 rows the scoring
    queue was dropping as closed, 8,435 were outside the window and only 513 had actual
    evidence of closure.

    Lowering `hours_old_floor` to 24/72/168 on 2026-08-15 made this worse rather than
    better: a narrower window ages more postings out of it. The two changes interact, so
    the bug got louder exactly when scrape freshness improved.

    Unprovable absence now reads 'unknown' -- the state that already means "we have not
    established anything" -- rather than 'live', which would be an equally unearned claim
    in the other direction. Every reader gates on `!= 'likely_closed'`, so these postings
    return to the queue.

    `last_hours_old` advances on every ATTEMPT while `last_success_at` advances only on
    success, so after a failed attempt the two describe different visits. The skew is
    small -- `adaptive_hours_old` derives both from the same `last_success_at` -- and the
    alternative is a correlated subquery into `cell_observations` for a view that is
    already scanned whole. NULL is treated as a zero-width window, so nothing is provable
    from a cell that never recorded one.

    No backfill: liveness is a view, and the postings this releases are still sitting in
    `pipeline_state = 'new'`. They rejoin the queue on the next scoring run.
    """
    cursor.execute("DROP VIEW IF EXISTS v_job_liveness")
    cursor.execute(
        """
        CREATE VIEW v_job_liveness AS
        SELECT j.id AS job_id,
               j.last_seen_at,
               c.last_success_at AS cell_last_success_at,
               CASE
                 WHEN j.last_seen_at IS NULL OR c.last_success_at IS NULL THEN 'unknown'
                 -- The cell was scraped well after we last saw this posting. Absence is
                 -- evidence only if that scrape's recency window reached back far enough
                 -- to have returned the posting at all.
                 WHEN unixepoch(c.last_success_at) - unixepoch(j.last_seen_at) > 43200 THEN
                      CASE
                        WHEN j.date_posted IS NOT NULL
                             AND (unixepoch(c.last_success_at)
                                  - unixepoch(j.date_posted))
                                 <= COALESCE(c.last_hours_old, 0) * 3600
                             THEN 'likely_closed'
                        ELSE 'unknown'
                      END
                 -- Nobody has looked at this cell in over a week, so we know nothing
                 -- current about it either way.
                 WHEN unixepoch('now') - unixepoch(c.last_success_at) > 604800 THEN 'stale'
                 ELSE 'live'
               END AS liveness
        FROM jobs j
        LEFT JOIN scrape_cells c ON c.id = j.scrape_cell_id
        """
    )


def _v13_drop_denormalized_fit_score(cursor: sqlite3.Cursor) -> None:
    """Delete `jobs.fit_score`. The verdict row is the only score now.

    v5 created `job_verdicts` and added this column in the same breath: the table keys on
    `(job_id, profile_version)` and is the record, while the column was a copy kept on
    `jobs` so a caller could rank by score without the join.

    The copy carries no profile version. Every reader of it joined -- or should have
    joined -- `job_verdicts` on the ACTIVE profile, and between a `profile build` and the
    re-score that follows it the two say different things: the join returns NULL while the
    copy still holds whatever the previous profile decided. Measured on this corpus, 306
    of the 320 postings scored under both profile v3 and v4 changed score across the
    rebuild, by as much as 57 points. That is the size of the lie the copy could tell.

    It was believed, too. The dashboard drawer printed the copy beside a tier badge that
    read `unscored` from the join; `min_fit_score` thresholded it; and the research stage
    fed it into a synthesis prompt and cached the result in
    `company_dossiers.nearby_jobs_json`.

    `idx_jobs_pipeline_state` is rebuilt without it. SQLite refuses to drop an indexed
    column, and the second key had no user left in any case -- the scoring backlog selects
    on `pipeline_state = 'new'` with no score ordering (`scoring/worker.py`).
    """
    cursor.execute("DROP INDEX IF EXISTS idx_jobs_pipeline_state")
    cursor.execute("CREATE INDEX idx_jobs_pipeline_state ON jobs(pipeline_state)")

    existing = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    if "fit_score" in existing:
        cursor.execute("ALTER TABLE jobs DROP COLUMN fit_score")


def _v14_simplified_scoring(cursor: sqlite3.Cursor) -> None:
    """Migrate job_verdicts to simplified schema: fit, reason_type, reason_description."""
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(job_verdicts)")}
    if not existing:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS job_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                profile_version INTEGER NOT NULL,
                model TEXT NOT NULL,
                fit INTEGER,
                reason_type TEXT,
                reason_description TEXT,
                fit_score INTEGER,
                verdict TEXT,
                seniority_fit TEXT,
                hard_blockers TEXT,
                key_gaps TEXT,
                strengths TEXT,
                reasoning TEXT,
                research_worthy INTEGER DEFAULT 0,
                role_summary TEXT,
                eligibility TEXT,
                role_match TEXT,
                capability_match TEXT,
                seniority_gap TEXT,
                evidence_quality TEXT,
                core_requirements TEXT,
                requirement_assessments TEXT,
                audit_flags TEXT,
                scale_version INTEGER,
                verdict_schema_version INTEGER,
                pareto_tier INTEGER,
                prompt_hash TEXT,
                tokens_in INTEGER,
                tokens_cached INTEGER,
                tokens_out INTEGER,
                cost_usd REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, profile_version)
            )
            """
        )
    else:
        cursor.execute(
            """
            CREATE TABLE job_verdicts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                profile_version INTEGER NOT NULL,
                model TEXT NOT NULL,
                fit INTEGER,
                reason_type TEXT,
                reason_description TEXT,
                fit_score INTEGER,
                verdict TEXT,
                seniority_fit TEXT,
                hard_blockers TEXT,
                key_gaps TEXT,
                strengths TEXT,
                reasoning TEXT,
                research_worthy INTEGER DEFAULT 0,
                role_summary TEXT,
                eligibility TEXT,
                role_match TEXT,
                capability_match TEXT,
                seniority_gap TEXT,
                evidence_quality TEXT,
                core_requirements TEXT,
                requirement_assessments TEXT,
                audit_flags TEXT,
                scale_version INTEGER,
                verdict_schema_version INTEGER,
                pareto_tier INTEGER,
                prompt_hash TEXT,
                tokens_in INTEGER,
                tokens_cached INTEGER,
                tokens_out INTEGER,
                cost_usd REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, profile_version)
            )
            """
        )
        common_cols = [
            "id",
            "job_id",
            "profile_version",
            "model",
            "fit_score",
            "verdict",
            "seniority_fit",
            "hard_blockers",
            "key_gaps",
            "strengths",
            "reasoning",
            "research_worthy",
            "tokens_in",
            "tokens_cached",
            "tokens_out",
            "cost_usd",
            "created_at",
        ]
        for opt_col in [
            "role_summary",
            "eligibility",
            "role_match",
            "capability_match",
            "seniority_gap",
            "evidence_quality",
            "core_requirements",
            "requirement_assessments",
            "audit_flags",
            "scale_version",
            "verdict_schema_version",
            "pareto_tier",
            "prompt_hash",
            "fit",
            "reason_type",
            "reason_description",
        ]:
            if opt_col in existing:
                common_cols.append(opt_col)

        cols_str = ", ".join(common_cols)
        cursor.execute(
            f"INSERT INTO job_verdicts_new ({cols_str}) SELECT {cols_str} FROM job_verdicts"
        )
        cursor.execute("DROP TABLE job_verdicts")
        cursor.execute("ALTER TABLE job_verdicts_new RENAME TO job_verdicts")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdicts_fit ON job_verdicts(profile_version, fit)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdicts_reason "
        "ON job_verdicts(profile_version, reason_type)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_verdicts_score ON job_verdicts(fit_score DESC)")


def _v15_drop_legacy_verdict_columns(cursor: sqlite3.Cursor) -> None:
    """Drop legacy 5-ordinal scoring columns from job_verdicts, keeping only simplified schema."""
    cursor.execute("DROP INDEX IF EXISTS idx_verdicts_score")

    existing = {row[1] for row in cursor.execute("PRAGMA table_info(job_verdicts)")}
    if not existing:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS job_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                profile_version INTEGER NOT NULL,
                model TEXT NOT NULL,
                fit INTEGER,
                reason_type TEXT,
                reason_description TEXT,
                verdict_schema_version INTEGER,
                prompt_hash TEXT,
                tokens_in INTEGER,
                tokens_cached INTEGER,
                tokens_out INTEGER,
                cost_usd REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, profile_version)
            )
            """
        )
    else:
        cursor.execute(
            """
            CREATE TABLE job_verdicts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                profile_version INTEGER NOT NULL,
                model TEXT NOT NULL,
                fit INTEGER,
                reason_type TEXT,
                reason_description TEXT,
                verdict_schema_version INTEGER,
                prompt_hash TEXT,
                tokens_in INTEGER,
                tokens_cached INTEGER,
                tokens_out INTEGER,
                cost_usd REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, profile_version)
            )
            """
        )
        desired_cols = [
            "id",
            "job_id",
            "profile_version",
            "model",
            "fit",
            "reason_type",
            "reason_description",
            "verdict_schema_version",
            "prompt_hash",
            "tokens_in",
            "tokens_cached",
            "tokens_out",
            "cost_usd",
            "created_at",
        ]
        copy_cols = [col for col in desired_cols if col in existing]
        cols_str = ", ".join(copy_cols)
        cursor.execute(
            f"INSERT INTO job_verdicts_new ({cols_str}) SELECT {cols_str} FROM job_verdicts"
        )
        cursor.execute("DROP TABLE job_verdicts")
        cursor.execute("ALTER TABLE job_verdicts_new RENAME TO job_verdicts")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdicts_fit ON job_verdicts(profile_version, fit)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdicts_reason "
        "ON job_verdicts(profile_version, reason_type)"
    )


def _v16_timestamp_normalization_and_liveness(cursor: sqlite3.Cursor) -> None:
    """Normalize date-only date_posted values to 12:00:00Z and use unixepoch in v_job_liveness.

    Date-only strings ('YYYY-MM-DD') lack time granularity and cause rollover issues in
    windowed queries. Backfilling them to 12:00:00Z sets a deterministic midpoint.
    v_job_liveness is rewritten to use unixepoch() rather than julianday().
    """
    cursor.execute(
        """
        UPDATE jobs
        SET date_posted = date_posted || 'T12:00:00Z'
        WHERE date_posted IS NOT NULL AND length(date_posted) = 10
        """
    )
    cursor.execute("DROP VIEW IF EXISTS v_job_liveness")
    cursor.execute(
        """
        CREATE VIEW v_job_liveness AS
        SELECT j.id AS job_id,
               j.last_seen_at,
               c.last_success_at AS cell_last_success_at,
               CASE
                 WHEN j.last_seen_at IS NULL OR c.last_success_at IS NULL THEN 'unknown'
                 -- The cell was scraped well after we last saw this posting (12h grace).
                 WHEN unixepoch(c.last_success_at) - unixepoch(j.last_seen_at) > 43200 THEN
                      CASE
                        WHEN j.date_posted IS NOT NULL
                             AND (unixepoch(c.last_success_at)
                                  - unixepoch(j.date_posted))
                                 <= COALESCE(c.last_hours_old, 0) * 3600
                             THEN 'likely_closed'
                        ELSE 'unknown'
                      END
                 -- Stale if no scrape in over 7 days (7 * 86400s).
                 WHEN unixepoch('now') - unixepoch(c.last_success_at) > 604800 THEN 'stale'
                 ELSE 'live'
               END AS liveness
        FROM jobs j
        LEFT JOIN scrape_cells c ON c.id = j.scrape_cell_id
        """
    )


def _v17_resume_builder(cursor: sqlite3.Cursor) -> None:
    """Add tables for dynamic Master Resume Profile and Tailored Generated Resumes."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS resume_master_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            location TEXT,
            github TEXT,
            linkedin TEXT,
            website TEXT,
            summary_guidance TEXT,
            education_json TEXT NOT NULL DEFAULT '[]',
            skills_json TEXT NOT NULL DEFAULT '[]',
            experience_json TEXT NOT NULL DEFAULT '[]',
            raw_achievements_md TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_resumes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            profile_version INTEGER REFERENCES profiles(version),
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            docx_path TEXT NOT NULL,
            pdf_path TEXT,
            resume_json TEXT NOT NULL,
            summary TEXT,
            ats_score INTEGER,
            ats_verdict TEXT,
            ats_feedback TEXT,
            status TEXT DEFAULT 'generated'
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_resumes_job_id ON generated_resumes(job_id)")

    existing = cursor.execute("SELECT COUNT(*) FROM resume_master_profile").fetchone()[0]
    if existing == 0:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """
            INSERT INTO resume_master_profile
                (updated_at, name, email, phone, location, github, linkedin, website,
                 summary_guidance, education_json, skills_json, experience_json,
                 raw_achievements_md)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "[]",
                "[]",
                "[]",
                "",
            ),
        )


def _v18_unified_master_profile(cursor: sqlite3.Cursor) -> None:
    """Consolidate the candidate Master Profile for resume generation and scoring."""
    import json

    existing = {row[1] for row in cursor.execute("PRAGMA table_info(resume_master_profile)")}

    default_roles = json.dumps(
        [
            "Software Engineer",
            "Platform Engineer",
            "Full-Stack Engineer",
        ]
    )
    default_industries = json.dumps(
        [
            "Cloud Infrastructure",
            "Developer Tools",
            "AI / ML Applications",
        ]
    )
    default_dealbreakers = json.dumps(
        [
            "24-hour on-call site reliability rotations",
            "No remote flexibility",
        ]
    )

    columns_to_add = [
        ("seniority", "TEXT DEFAULT 'Mid / Senior'"),
        ("years_experience", "REAL DEFAULT 4.0"),
        ("citizenship_json", "TEXT DEFAULT '[\"Authorized to work in US\"]'"),
        ("locations_json", "TEXT DEFAULT '[\"Remote\"]'"),
        ("willing_to_relocate", "INTEGER DEFAULT 0"),
        ("comp_floor_usd", "INTEGER DEFAULT 90000"),
        ("target_roles_json", f"TEXT DEFAULT '{default_roles}'"),
        ("work_modes_json", 'TEXT DEFAULT \'["remote", "hybrid", "onsite"]\''),
        ("target_industries_json", f"TEXT DEFAULT '{default_industries}'"),
        ("dealbreakers_json", f"TEXT DEFAULT '{default_dealbreakers}'"),
        ("strengths_json", "TEXT DEFAULT '[]'"),
        ("weaknesses_json", "TEXT DEFAULT '[]'"),
        ("skill_ratings_json", "TEXT DEFAULT '[]'"),
    ]

    for col_name, ddl in columns_to_add:
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE resume_master_profile ADD COLUMN {col_name} {ddl}")

    try:
        prof_row = cursor.execute(
            "SELECT profile_json FROM profiles WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if prof_row and prof_row[0]:
            p_data = json.loads(prof_row[0])
            strengths = json.dumps(p_data.get("strengths") or [])
            weaknesses = json.dumps(p_data.get("weaknesses") or [])
            dealbreakers = json.dumps(
                (p_data.get("non_negotiables") or []) + (p_data.get("red_flags") or [])
            )
            constraints = p_data.get("constraints") or {}
            auth = constraints.get("work_authorization") or ["Authorized to work in US"]
            citizenship = json.dumps(auth)
            locs = constraints.get("locations") or ["Remote"]
            locations = json.dumps(locs)
            comp_floor = constraints.get("comp_floor_usd") or 90000
            willing_relocate = 1 if constraints.get("willing_to_relocate", False) else 0

            prefs = p_data.get("preferences") or {}
            target_roles = json.dumps(prefs.get("role_families") or [])
            target_industries = json.dumps(prefs.get("industries") or [])
            modes = (
                [prefs.get("work_mode")]
                if prefs.get("work_mode")
                else ["remote", "hybrid", "onsite"]
            )
            work_modes = json.dumps(modes)
            seniority = p_data.get("seniority") or "Mid / Senior"
            years_exp = p_data.get("years_experience") or 4.0
            skill_ratings = json.dumps(p_data.get("skills") or [])

            cursor.execute(
                """
                UPDATE resume_master_profile
                   SET seniority = ?,
                       years_experience = ?,
                       citizenship_json = ?,
                       locations_json = ?,
                       willing_to_relocate = ?,
                       comp_floor_usd = ?,
                       target_roles_json = ?,
                       work_modes_json = ?,
                       target_industries_json = ?,
                       dealbreakers_json = ?,
                       strengths_json = ?,
                       weaknesses_json = ?,
                       skill_ratings_json = ?
                """,
                (
                    seniority,
                    years_exp,
                    citizenship,
                    locations,
                    willing_relocate,
                    comp_floor,
                    target_roles,
                    work_modes,
                    target_industries,
                    dealbreakers,
                    strengths,
                    weaknesses,
                    skill_ratings,
                ),
            )
    except (sqlite3.Error, ValueError, KeyError) as exc:
        logger.warning("Failed to backfill master profile from active profile: %s", exc)


def _v19_single_profile_table(cursor: sqlite3.Cursor) -> None:
    """Create the unified singleton `profile` table as the sole source of truth."""
    import json

    default_eligibility = json.dumps(
        {
            "citizenship": ["Authorized to work in US"],
            "locations": ["Remote"],
            "willing_to_relocate": False,
            "comp_floor_usd": 90000,
        }
    )
    default_targeting = json.dumps(
        {
            "target_roles": ["Software Engineer", "Platform Engineer"],
            "work_modes": ["remote", "hybrid", "onsite"],
            "target_industries": ["Cloud Infrastructure", "Developer Tools"],
            "dealbreakers": ["24-hour on-call site reliability rotations"],
        }
    )

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            github TEXT NOT NULL DEFAULT '',
            linkedin TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT '',
            summary_guidance TEXT NOT NULL DEFAULT '',
            seniority TEXT NOT NULL DEFAULT 'Mid / Senior',
            years_experience REAL NOT NULL DEFAULT 4.0,
            eligibility_json TEXT NOT NULL DEFAULT '{default_eligibility}',
            targeting_json TEXT NOT NULL DEFAULT '{default_targeting}',
            education_json TEXT NOT NULL DEFAULT '[]',
            skills_json TEXT NOT NULL DEFAULT '[]',
            experience_json TEXT NOT NULL DEFAULT '[]',
            summary_text TEXT NOT NULL DEFAULT ''
        )
        """
    )

    tables = {
        r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "resume_master_profile" in tables:
        try:
            rmp = cursor.execute(
                "SELECT * FROM resume_master_profile ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if rmp:
                col_names = [d[0] for d in cursor.description]
                r_dict = dict(zip(col_names, rmp, strict=False))

                citizenship = json.loads(
                    r_dict.get("citizenship_json") or '["Authorized to work in US"]'
                )
                locations = json.loads(r_dict.get("locations_json") or '["Remote"]')
                willing = bool(r_dict.get("willing_to_relocate", 0))
                comp = r_dict.get("comp_floor_usd") or 90000
                eligibility = json.dumps(
                    {
                        "citizenship": citizenship,
                        "locations": locations,
                        "willing_to_relocate": willing,
                        "comp_floor_usd": comp,
                    }
                )

                target_roles = json.loads(r_dict.get("target_roles_json") or "[]")
                work_modes = json.loads(
                    r_dict.get("work_modes_json") or '["remote", "hybrid", "onsite"]'
                )
                target_industries = json.loads(r_dict.get("target_industries_json") or "[]")
                dealbreakers = json.loads(r_dict.get("dealbreakers_json") or "[]")
                targeting = json.dumps(
                    {
                        "target_roles": target_roles,
                        "work_modes": work_modes,
                        "target_industries": target_industries,
                        "dealbreakers": dealbreakers,
                    }
                )

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO profile (
                        id, updated_at, name, email, phone, location, github, linkedin, website,
                        summary_guidance, seniority, years_experience, eligibility_json,
                        targeting_json, education_json, skills_json, experience_json, summary_text
                    ) VALUES (
                        1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ''
                    )
                    """,
                    (
                        r_dict.get("name") or "",
                        r_dict.get("email") or "",
                        r_dict.get("phone") or "",
                        r_dict.get("location") or "",
                        r_dict.get("github") or "",
                        r_dict.get("linkedin") or "",
                        r_dict.get("website") or "",
                        r_dict.get("summary_guidance") or "",
                        r_dict.get("seniority") or "Mid / Senior",
                        r_dict.get("years_experience") or 4.0,
                        eligibility,
                        targeting,
                        r_dict.get("education_json") or "[]",
                        r_dict.get("skills_json") or "[]",
                        r_dict.get("experience_json") or "[]",
                    ),
                )
        except (sqlite3.Error, ValueError, KeyError) as exc:
            logger.warning("Failed to migrate resume_master_profile to profile: %s", exc)

    cursor.execute(
        f"""
        INSERT OR IGNORE INTO profile (
            id, updated_at, name, email, phone, location, github, linkedin, website,
            summary_guidance, seniority, years_experience, eligibility_json,
            targeting_json, education_json, skills_json, experience_json, summary_text
        ) VALUES (
            1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), '', '', '', '', '', '', '',
            '', 'Mid / Senior', 4.0, '{default_eligibility}',
            '{default_targeting}', '[]', '[]', '[]', ''
        )
        """
    )


def _v20_target_roles_and_queries(cursor: sqlite3.Cursor) -> None:
    """Create target_roles, target_queries, and target_locations tables."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS target_roles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            key          TEXT UNIQUE NOT NULL,
            label        TEXT NOT NULL,
            resume       TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            enabled      INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL
        );
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS target_queries (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            role_key TEXT NOT NULL REFERENCES target_roles(key) ON DELETE CASCADE,
            query    TEXT NOT NULL,
            enabled  INTEGER NOT NULL DEFAULT 1,
            UNIQUE(role_key, query)
        );
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS target_locations (
            id             TEXT PRIMARY KEY,
            label          TEXT NOT NULL,
            search_label   TEXT NOT NULL,
            country        TEXT NOT NULL,
            indeed_country TEXT NOT NULL DEFAULT 'usa',
            is_remote      INTEGER NOT NULL DEFAULT 0,
            access         TEXT NOT NULL DEFAULT 'relocation',
            weight         REAL NOT NULL DEFAULT 1.0,
            distance       INTEGER NOT NULL DEFAULT 50,
            enabled        INTEGER NOT NULL DEFAULT 1
        );
        """
    )


def _v21_profile_executive_summary_and_projects(cursor: sqlite3.Cursor) -> None:
    """Add executive_summary, model_guidance, dealbreakers_json, projects_json to profile table."""
    import json

    cols = {row[1] for row in cursor.execute("PRAGMA table_info(profile)").fetchall()}
    if "executive_summary" not in cols:
        cursor.execute("ALTER TABLE profile ADD COLUMN executive_summary TEXT NOT NULL DEFAULT ''")
    if "model_guidance" not in cols:
        cursor.execute("ALTER TABLE profile ADD COLUMN model_guidance TEXT NOT NULL DEFAULT ''")
    if "dealbreakers_json" not in cols:
        cursor.execute(
            "ALTER TABLE profile ADD COLUMN dealbreakers_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "projects_json" not in cols:
        cursor.execute("ALTER TABLE profile ADD COLUMN projects_json TEXT NOT NULL DEFAULT '[]'")

    cursor.execute(
        """
        UPDATE profile
           SET executive_summary = summary_guidance
         WHERE (executive_summary IS NULL OR executive_summary = '')
           AND summary_guidance IS NOT NULL AND summary_guidance != ''
        """
    )
    row = cursor.execute(
        "SELECT id, targeting_json, dealbreakers_json FROM profile WHERE id = 1"
    ).fetchone()
    if row:
        pid, targ_raw, d_raw = row
        if (not d_raw or d_raw == "[]") and targ_raw:
            try:
                targ = json.loads(targ_raw)
                dealbreakers = targ.get("dealbreakers") or []
                if dealbreakers:
                    cursor.execute(
                        "UPDATE profile SET dealbreakers_json = ? WHERE id = ?",
                        (json.dumps(dealbreakers), pid),
                    )
            except (json.JSONDecodeError, ValueError, TypeError):
                pass


def _v22_drop_legacy_tables_and_columns(cursor: sqlite3.Cursor) -> None:
    """Drop legacy tables and unused columns from early architecture iterations.

    Dropped tables:
      - role_market_stats, skill_market_stats, skill_candidates: abandoned v2 tables.
      - profile_documents, interview_turns: obsolete v5 profile ingest/interview tables.
      - resume_master_profile, profiles: superseded by singleton `profile` table (v19).
      - job_blockers: write-only table with no reader queries (reasons now in `job_verdicts`).

    Dropped / recreated columns:
      - scrape_cells.ewma_yield_per_day: unused 100% NULL column.
      - generated_resumes.profile_version: obsolete foreign key to legacy `profiles` table.
    """
    cursor.execute("DROP TABLE IF EXISTS role_market_stats")
    cursor.execute("DROP TABLE IF EXISTS skill_market_stats")
    cursor.execute("DROP TABLE IF EXISTS skill_candidates")
    cursor.execute("DROP INDEX IF EXISTS idx_rms_lookup")
    cursor.execute("DROP INDEX IF EXISTS idx_sms_lookup")

    cursor.execute("DROP TABLE IF EXISTS profile_documents")
    cursor.execute("DROP TABLE IF EXISTS interview_turns")
    cursor.execute("DROP TABLE IF EXISTS resume_master_profile")

    cursor.execute("DROP TABLE IF EXISTS job_blockers")

    cells_cols = {row[1] for row in cursor.execute("PRAGMA table_info(scrape_cells)")}
    if "ewma_yield_per_day" in cells_cols:
        cursor.execute("ALTER TABLE scrape_cells DROP COLUMN ewma_yield_per_day")

    existing_gr = {row[1] for row in cursor.execute("PRAGMA table_info(generated_resumes)")}
    if existing_gr:
        cursor.execute(
            """
            CREATE TABLE generated_resumes_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                docx_path TEXT NOT NULL,
                pdf_path TEXT,
                resume_json TEXT NOT NULL,
                summary TEXT,
                ats_score INTEGER,
                ats_verdict TEXT,
                ats_feedback TEXT,
                status TEXT DEFAULT 'generated'
            )
            """
        )
        cols_to_copy = [
            c
            for c in [
                "id",
                "job_id",
                "model",
                "created_at",
                "docx_path",
                "pdf_path",
                "resume_json",
                "summary",
                "ats_score",
                "ats_verdict",
                "ats_feedback",
                "status",
            ]
            if c in existing_gr
        ]
        cols_str = ", ".join(cols_to_copy)
        cursor.execute(
            f"INSERT INTO generated_resumes_new ({cols_str}) "
            f"SELECT {cols_str} FROM generated_resumes"
        )
        cursor.execute("DROP TABLE generated_resumes")
        cursor.execute("ALTER TABLE generated_resumes_new RENAME TO generated_resumes")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_resumes_job_id ON generated_resumes(job_id)")

    cursor.execute("DROP TABLE IF EXISTS profiles")
    cursor.execute("DROP INDEX IF EXISTS idx_profiles_one_active")


def _v23_add_typst_path_to_generated_resumes(cursor: sqlite3.Cursor) -> None:
    """Add typst_path column to generated_resumes table."""
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(generated_resumes)")}
    if "typst_path" not in existing_cols:
        cursor.execute("ALTER TABLE generated_resumes ADD COLUMN typst_path TEXT")


def _v24_cell_centric_queries(cursor: sqlite3.Cursor) -> None:
    """Cell-centric query model: a scrape cell carries every parameter of its own search.

    `role_family` began as a semantic grouping for the regex classifier removed in
    42179cd. With the classifier, the research feature and post-stratification weighting
    all gone, nothing does semantic work with the grouping any more, so the query string
    becomes the unit of identity and the column leaves every table.

    The JobSpy parameters -- search_label, country, indeed_country, is_remote, distance --
    plus the scheduler's location weight move onto `scrape_cells`, so `build_task` reads
    one row instead of joining the locations table at task-build time. `seed_cells`
    refreshes them whenever a location definition changes: cells are not immutable
    snapshots, and per-run provenance already lives in `cell_observations`.

    Merge rule for cells that collide once `role_family` leaves the unique key: the row
    with the most `total_scrapes` keeps its history and adopts the others' postings and
    observations (ties go to the lowest id). Measured before writing this migration --
    zero colliding pairs in either the development or the production database -- so the
    branch is a safety net, not a data change.

    The eligibility views widen. `role_family IS NOT NULL` used to exclude the postings
    the regex left unclassified; cell-derived provenance is never null, so those postings
    now count. Rows written after 42179cd already behaved this way.
    """
    # The documented procedure for rebuilding a table other tables reference.
    # `migrate()` restores the pragma after the migration commits.
    cursor.execute("PRAGMA foreign_keys=OFF")

    # Views are re-parsed by ALTER TABLE ... RENAME, so they cannot outlive the tables
    # they name. Dropped here, recreated at the end.
    for view in ("v_supply_eligible", "v_skill_eligible", "cell_cost", "v_job_liveness"):
        cursor.execute(f"DROP VIEW IF EXISTS {view}")

    cell_columns = {row[1] for row in cursor.execute("PRAGMA table_info(scrape_cells)")}
    for column, ddl in (
        ("search_label", "TEXT NOT NULL DEFAULT ''"),
        ("country", "TEXT NOT NULL DEFAULT ''"),
        ("indeed_country", "TEXT NOT NULL DEFAULT 'usa'"),
        ("is_remote", "INTEGER NOT NULL DEFAULT 0"),
        ("distance", "INTEGER NOT NULL DEFAULT 50"),
        ("weight", "REAL NOT NULL DEFAULT 1.0"),
    ):
        if column not in cell_columns:
            cursor.execute(f"ALTER TABLE scrape_cells ADD COLUMN {column} {ddl}")

    cursor.execute(
        """
        UPDATE scrape_cells SET
            search_label = COALESCE(
                (SELECT l.search_label FROM target_locations l WHERE l.id = location_id),
                location_id),
            country = COALESCE(
                (SELECT l.country FROM target_locations l WHERE l.id = location_id), ''),
            indeed_country = COALESCE(
                (SELECT l.indeed_country FROM target_locations l WHERE l.id = location_id),
                'usa'),
            is_remote = COALESCE(
                (SELECT l.is_remote FROM target_locations l WHERE l.id = location_id), 0),
            distance = COALESCE(
                (SELECT l.distance FROM target_locations l WHERE l.id = location_id), 50),
            weight = COALESCE(
                (SELECT l.weight FROM target_locations l WHERE l.id = location_id), 1.0)
        """
    )

    cursor.execute(
        """
        CREATE TEMP TABLE cell_merge AS
        SELECT c.id AS old_id,
               (SELECT s.id FROM scrape_cells s
                 WHERE s.source = c.source
                   AND s.location_id = c.location_id
                   AND s.query = c.query
                 ORDER BY s.total_scrapes DESC, s.id ASC
                 LIMIT 1) AS new_id
          FROM scrape_cells c
        """
    )
    cursor.execute(
        "UPDATE jobs SET scrape_cell_id = (SELECT new_id FROM cell_merge WHERE old_id = "
        "scrape_cell_id) WHERE scrape_cell_id IN (SELECT old_id FROM cell_merge)"
    )
    cursor.execute(
        "UPDATE cell_observations SET cell_id = (SELECT new_id FROM cell_merge WHERE old_id = "
        "cell_id) WHERE cell_id IN (SELECT old_id FROM cell_merge)"
    )
    cursor.execute("DELETE FROM scrape_cells WHERE id NOT IN (SELECT new_id FROM cell_merge)")
    cursor.execute("DROP TABLE cell_merge")

    cursor.execute(
        """
        CREATE TABLE scrape_cells_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            location_id TEXT NOT NULL,
            query       TEXT NOT NULL,

            -- The JobSpy call, embedded. Refreshed by seed_cells from search_locations.
            search_label   TEXT NOT NULL,
            country        TEXT NOT NULL,
            indeed_country TEXT NOT NULL DEFAULT 'usa',
            is_remote      INTEGER NOT NULL DEFAULT 0,
            distance       INTEGER NOT NULL DEFAULT 50,
            -- Dropped again in v28: the priority product that read it is gone.
            weight         REAL NOT NULL DEFAULT 1.0,

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
            ewma_fit_score      REAL,
            quality_samples     INTEGER NOT NULL DEFAULT 0,
            consecutive_empty INTEGER NOT NULL DEFAULT 0,
            consecutive_error INTEGER NOT NULL DEFAULT 0,
            total_scrapes     INTEGER NOT NULL DEFAULT 0,
            total_postings    INTEGER NOT NULL DEFAULT 0,
            backoff_until     TEXT,
            last_error        TEXT,

            created_at TEXT NOT NULL,
            UNIQUE (source, location_id, query)
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO scrape_cells_new
            (id, source, location_id, query, search_label, country, indeed_country,
             is_remote, distance, weight, enabled, last_scraped_at, last_success_at,
             last_requested, last_result_count, last_new_count, last_saturated,
             last_hours_old, ewma_new_per_scrape, ewma_fit_score, quality_samples,
             consecutive_empty, consecutive_error, total_scrapes, total_postings,
             backoff_until, last_error, created_at)
        SELECT id, source, location_id, query, search_label, country, indeed_country,
               is_remote, distance, weight, enabled, last_scraped_at, last_success_at,
               last_requested, last_result_count, last_new_count, last_saturated,
               last_hours_old, ewma_new_per_scrape, ewma_fit_score, quality_samples,
               consecutive_empty, consecutive_error, total_scrapes, total_postings,
               backoff_until, last_error, created_at
          FROM scrape_cells
        """
    )
    cursor.execute("DROP TABLE scrape_cells")
    cursor.execute("ALTER TABLE scrape_cells_new RENAME TO scrape_cells")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cells_sched "
        "ON scrape_cells(source, enabled, backoff_until, last_scraped_at)"
    )

    cursor.execute(
        """
        CREATE TABLE cell_observations_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
            cell_id     INTEGER REFERENCES scrape_cells(id),
            source      TEXT NOT NULL,
            location_id TEXT NOT NULL,
            query       TEXT NOT NULL,
            observed_at   TEXT NOT NULL,
            hours_old     INTEGER,
            window_start  TEXT,                  -- observed_at - hours_old
            window_end    TEXT,                  -- observed_at
            requested         INTEGER NOT NULL DEFAULT 0,
            returned          INTEGER NOT NULL DEFAULT 0,
            new_unique        INTEGER NOT NULL DEFAULT 0,
            saturated         INTEGER NOT NULL DEFAULT 0,
            desc_selection    TEXT NOT NULL DEFAULT 'none',
            descriptions_full INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,                -- ok|empty|error|skipped
            error  TEXT,
            duration_ms   INTEGER,
            requests_made INTEGER
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO cell_observations_new
            (id, sync_run_id, cell_id, source, location_id, query, observed_at, hours_old,
             window_start, window_end, requested, returned, new_unique, saturated,
             desc_selection, descriptions_full, status, error, duration_ms, requests_made)
        SELECT id, sync_run_id, cell_id, source, location_id, query, observed_at, hours_old,
               window_start, window_end, requested, returned, new_unique, saturated,
               desc_selection, descriptions_full, status, error, duration_ms, requests_made
          FROM cell_observations
        """
    )
    cursor.execute("DROP TABLE cell_observations")
    cursor.execute("ALTER TABLE cell_observations_new RENAME TO cell_observations")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_cellobs_scope "
        "ON cell_observations(query, location_id, source, observed_at)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cellobs_run ON cell_observations(sync_run_id)")

    cursor.execute("DROP INDEX IF EXISTS idx_jobs_family_found")
    cursor.execute("DROP INDEX IF EXISTS idx_jobs_access")
    job_columns = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)")}
    for column in ("role_family", "role_family_hint", "access", "dossier_id"):
        if column in job_columns:
            cursor.execute(f"ALTER TABLE jobs DROP COLUMN {column}")

    cursor.execute("DROP INDEX IF EXISTS idx_dossiers_display")
    cursor.execute("DROP TABLE IF EXISTS company_dossiers")
    cursor.execute("DROP TABLE IF EXISTS research_runs")

    cursor.execute(
        """
        CREATE TABLE search_queries (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            query   TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO search_queries (id, query, enabled)
        SELECT MIN(id), query, MAX(enabled) FROM target_queries GROUP BY query
        """
    )
    cursor.execute("DROP TABLE target_queries")
    cursor.execute("DROP TABLE IF EXISTS target_roles")

    cursor.execute(
        """
        CREATE TABLE search_locations (
            id             TEXT PRIMARY KEY,
            label          TEXT NOT NULL,
            search_label   TEXT NOT NULL,
            country        TEXT NOT NULL,
            indeed_country TEXT NOT NULL DEFAULT 'usa',
            is_remote      INTEGER NOT NULL DEFAULT 0,
            weight         REAL NOT NULL DEFAULT 1.0,
            distance       INTEGER NOT NULL DEFAULT 50,
            enabled        INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO search_locations
            (id, label, search_label, country, indeed_country, is_remote, weight,
             distance, enabled)
        SELECT id, label, search_label, country, indeed_country, is_remote, weight,
               distance, enabled
          FROM target_locations
        """
    )
    cursor.execute("DROP TABLE target_locations")

    cursor.executescript(
        """
        CREATE VIEW v_supply_eligible AS
        SELECT * FROM jobs
        WHERE sync_run_id IS NOT NULL
          AND duplicate_of IS NULL;

        CREATE VIEW v_skill_eligible AS
        SELECT * FROM jobs
        WHERE sync_run_id IS NOT NULL
          AND duplicate_of IS NULL
          AND description_quality = 'full'
          AND desc_selection = 'census';

        CREATE VIEW cell_cost AS
        SELECT
            o.id,
            o.sync_run_id,
            o.cell_id,
            o.source,
            o.location_id,
            o.query,
            o.observed_at,
            o.status,
            o.desc_selection,
            o.requested,
            o.returned,
            o.new_unique,
            o.descriptions_full,
            o.duration_ms,
            o.requests_made,
            o.duration_ms / 1000.0                              AS seconds,
            1.0 * o.duration_ms / NULLIF(o.requests_made, 0)    AS ms_per_request,
            o.duration_ms / 1000.0 / NULLIF(o.returned, 0)      AS seconds_per_posting,
            1.0 * o.requests_made / NULLIF(o.returned, 0)       AS requests_per_posting
        -- Measurements and ratios of measurements only. The planner's estimate of the same
        -- cost lives in scheduler.estimate_units and reads page_size from config.yaml;
        -- restating it here in SQL would let the two drift, and the whole point of these
        -- columns is to be checkable against the model rather than derived from it.
        FROM cell_observations o;

        CREATE VIEW v_job_liveness AS
        SELECT j.id AS job_id,
               j.last_seen_at,
               c.last_success_at AS cell_last_success_at,
               CASE
                 WHEN j.last_seen_at IS NULL OR c.last_success_at IS NULL THEN 'unknown'
                 -- The cell was scraped well after we last saw this posting (12h grace).
                 WHEN unixepoch(c.last_success_at) - unixepoch(j.last_seen_at) > 43200 THEN
                      CASE
                        WHEN j.date_posted IS NOT NULL
                             AND (unixepoch(c.last_success_at)
                                  - unixepoch(j.date_posted))
                                 <= COALESCE(c.last_hours_old, 0) * 3600
                             THEN 'likely_closed'
                        ELSE 'unknown'
                      END
                 -- Stale if no scrape in over 7 days (7 * 86400s).
                 WHEN unixepoch('now') - unixepoch(c.last_success_at) > 604800 THEN 'stale'
                 ELSE 'live'
               END AS liveness
        FROM jobs j
        LEFT JOIN scrape_cells c ON c.id = j.scrape_cell_id;
        """
    )


def _v25_cell_quality_counters(cursor: sqlite3.Cursor) -> None:
    """Replace `scrape_cells.ewma_fit_score` with two plain counters.

    The v10 EWMA was fed one posting at a time at alpha=0.4, giving it a 1.36-sample
    half-life against cells that hold 1,400+ verdicts. It therefore measured whether the
    last two or three postings happened to fit, not the cell. In production the stored
    values had collapsed onto the reachable points of that recurrence -- 321 of 537 cells
    sat below 1.0 and 186 sat at 0.4*100, regardless of a true fit rate spanning 0 to 0.48.

    `quality_fits / quality_samples` is the same signal without the decay, and because
    counting is order-independent the existing per-posting call site becomes correct.

    Backfilled from job_verdicts at the newest profile version: earlier versions scored the
    same corpus roughly 3x looser, so folding them in would understate every live cell.
    """
    cursor.execute("PRAGMA table_info(scrape_cells)")
    existing = {row[1] for row in cursor.fetchall()}

    if "quality_fits" not in existing:
        cursor.execute(
            "ALTER TABLE scrape_cells ADD COLUMN quality_fits INTEGER NOT NULL DEFAULT 0"
        )

    row = cursor.execute("SELECT MAX(profile_version) FROM job_verdicts").fetchone()
    if row and row[0] is not None:
        # Aggregated once into a keyed temp table rather than as correlated subqueries per
        # cell: jobs.scrape_cell_id carries no index, so the per-cell form rescanned the
        # whole join 500+ times and did not finish in minutes on a 750MB database.
        cursor.execute("DROP TABLE IF EXISTS temp._v25_rates")
        cursor.execute(
            """
            CREATE TEMP TABLE _v25_rates AS
            SELECT j.scrape_cell_id AS cell_id,
                   SUM(v.fit = 1)   AS fits,
                   COUNT(*)         AS samples
              FROM jobs j
              JOIN job_verdicts v ON v.job_id = j.id
             WHERE j.scrape_cell_id IS NOT NULL AND v.profile_version = ?
             GROUP BY j.scrape_cell_id
            """,
            (row[0],),
        )
        cursor.execute("CREATE INDEX temp.idx_v25_rates ON _v25_rates(cell_id)")
        cursor.execute(
            """
            UPDATE scrape_cells SET
                quality_fits = COALESCE(
                    (SELECT fits FROM _v25_rates WHERE cell_id = scrape_cells.id), 0),
                quality_samples = COALESCE(
                    (SELECT samples FROM _v25_rates WHERE cell_id = scrape_cells.id), 0)
            """
        )
        cursor.execute("DROP TABLE temp._v25_rates")

    if "ewma_fit_score" in existing:
        cursor.execute("ALTER TABLE scrape_cells DROP COLUMN ewma_fit_score")


def _v26_drop_new_per_scrape_ewma(cursor: sqlite3.Cursor) -> None:
    """Delete `scrape_cells.ewma_new_per_scrape`, a duplicate of `last_new_count`.

    `_make_task` never populated `ScrapeTask.ewma_new_per_scrape`, so the writer always
    called `update_ewma(None, new_count)`, which returns the observation unsmoothed. Every
    one of the 88 enabled cells held a whole number and 82 of them equalled
    `last_new_count` outright -- no cell ever carried a fractional value, which a real
    average would produce constantly.

    Nothing reads it now: the priority function that consumed it is gone, and the coverage
    report reads `last_new_count` instead.
    """
    cursor.execute("PRAGMA table_info(scrape_cells)")
    if "ewma_new_per_scrape" in {row[1] for row in cursor.fetchall()}:
        cursor.execute("ALTER TABLE scrape_cells DROP COLUMN ewma_new_per_scrape")


def _v27_drop_taxonomy_hash(cursor: sqlite3.Cursor) -> None:
    """Delete `jobs.taxonomy_hash` and `sync_runs.taxonomy_hash`.

    Both recorded which build of the static keyword taxonomy (`data/skills.yaml`) had
    classified a row, so a trend line could refuse to compare numbers produced under two
    different vocabularies. That taxonomy is gone -- skills now come from the candidate
    profile, and scoring provenance is `job_verdicts.profile_version` plus `prompt_hash`.

    No caller had passed a hash to `upsert_posting` or `start_sync_run` for some time, so
    every insert wrote NULL and every refresh of an existing posting overwrote the legacy
    value with NULL. The status report still compared the surviving values against a
    profile-derived hash they could never equal, and reported the whole corpus as stale.
    """
    for table in ("jobs", "sync_runs"):
        cursor.execute(f"PRAGMA table_info({table})")
        if "taxonomy_hash" in {row[1] for row in cursor.fetchall()}:
            cursor.execute(f"ALTER TABLE {table} DROP COLUMN taxonomy_hash")


def _v28_drop_location_weight(cursor: sqlite3.Cursor) -> None:
    """Delete `search_locations.weight` and `scrape_cells.weight`.

    The weight was one factor of the old six-term `cell_priority()` product, so a location
    set to 2.0 really did compete harder for the scrape budget. That product is gone: cells
    are visited oldest-attempt-first, and the only reader left tested `weight >= 1.0` to
    decide who counted for the staleness coverage warning. As a gate that is not a weight,
    and it silently dropped the low-weight cells out of the one report that would have said
    they were never being visited.

    The dashboard still offered a "Weight Multiplier (0.1 - 2.0)" field for a number that
    could no longer multiply anything.
    """
    for table in ("search_locations", "scrape_cells"):
        cursor.execute(f"PRAGMA table_info({table})")
        if "weight" in {row[1] for row in cursor.fetchall()}:
            cursor.execute(f"ALTER TABLE {table} DROP COLUMN weight")


def _v29_job_search_fts(cursor: sqlite3.Cursor) -> None:
    """Index the dashboard's searchable job fields with FTS5.

    The external-content table keeps the index compact while triggers keep it in
    lockstep with jobs, including postings updated on subsequent scrapes.
    """
    cursor.executescript(
        """
        CREATE VIRTUAL TABLE jobs_fts USING fts5(
            title, company, matched_skills, location, seniority,
            content='jobs', content_rowid='id'
        );

        INSERT INTO jobs_fts(rowid, title, company, matched_skills, location, seniority)
        SELECT id, title, company, matched_skills, location, seniority FROM jobs;

        CREATE TRIGGER jobs_fts_after_insert AFTER INSERT ON jobs BEGIN
            INSERT INTO jobs_fts(rowid, title, company, matched_skills, location, seniority)
            VALUES (
                new.id, new.title, new.company, new.matched_skills, new.location, new.seniority
            );
        END;

        CREATE TRIGGER jobs_fts_after_delete AFTER DELETE ON jobs BEGIN
            INSERT INTO jobs_fts(
                jobs_fts, rowid, title, company, matched_skills, location, seniority
            )
            VALUES (
                'delete', old.id, old.title, old.company, old.matched_skills,
                old.location, old.seniority
            );
        END;

        CREATE TRIGGER jobs_fts_after_update
        AFTER UPDATE OF title, company, matched_skills, location, seniority ON jobs BEGIN
            INSERT INTO jobs_fts(
                jobs_fts, rowid, title, company, matched_skills, location, seniority
            )
            VALUES (
                'delete', old.id, old.title, old.company, old.matched_skills,
                old.location, old.seniority
            );
            INSERT INTO jobs_fts(rowid, title, company, matched_skills, location, seniority)
            VALUES (
                new.id, new.title, new.company, new.matched_skills, new.location, new.seniority
            );
        END;
        """
    )


def _v30_scheduler_preferences(cursor: sqlite3.Cursor) -> None:
    """Persist user-controlled scheduler activation separately from deployment config."""
    cursor.execute(
        """
        CREATE TABLE scheduler_preferences (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Cursor], None]]] = [
    (1, "baseline jobs table", _v1_baseline),
    (2, "market analytics: cells, observations, skills, stats", _v2_analytics),
    (3, "optional LLM verdict column", _v3_llm_verdict),
    (4, "posting access level and unabbreviated location ids", _v4_access_and_location_ids),
    (5, "agentic pipeline: profiles, verdicts, dossiers, pipeline_state", _v5_agentic),
    (6, "ordinal verdicts, derived score, posting liveness", _v6_ordinal_verdicts),
    (7, "hard blockers carry quote and reasoning separately", _v7_structured_blockers),
    (8, "per-posting scoring failure counter", _v8_scoring_failure_counter),
    (9, "feed and dossier lookup indexes", _v9_feed_indexes),
    (10, "scrape cell quality EWMA fed from LLM verdicts", _v10_cell_quality_ewma),
    (11, "measured per-cell scrape cost: duration, requests, cell_cost view", _v11_cell_cost),
    (12, "liveness requires the re-scrape's window to reach the posting", _v12_liveness_window),
    (13, "drop the unversioned jobs.fit_score copy", _v13_drop_denormalized_fit_score),
    (
        14,
        "simplified scoring: fit boolean, reason_type, reason_description",
        _v14_simplified_scoring,
    ),
    (
        15,
        "remove legacy job_verdict columns",
        _v15_drop_legacy_verdict_columns,
    ),
    (
        16,
        "timestamp normalization and unixepoch liveness view",
        _v16_timestamp_normalization_and_liveness,
    ),
    (
        17,
        "resume builder: master profile and generated tailored resumes",
        _v17_resume_builder,
    ),
    (
        18,
        "unified master profile: consolidated 5 sections for resumes and scoring",
        _v18_unified_master_profile,
    ),
    (
        19,
        "single profile table: canonical singleton profile for scoring and resumes",
        _v19_single_profile_table,
    ),
    (
        20,
        "database-driven target roles, queries, and locations",
        _v20_target_roles_and_queries,
    ),
    (
        21,
        "profile executive summary, model guidance, dealbreakers, and standalone projects",
        _v21_profile_executive_summary_and_projects,
    ),
    (
        22,
        "drop legacy tables (profiles, role/skill market stats, job_blockers) and columns",
        _v22_drop_legacy_tables_and_columns,
    ),
    (
        23,
        "add typst_path to generated_resumes table",
        _v23_add_typst_path_to_generated_resumes,
    ),
    (
        24,
        "cell-centric queries: embed search params, drop role_family and access",
        _v24_cell_centric_queries,
    ),
    (
        25,
        "replace cell quality EWMA with plain fit/sample counters",
        _v25_cell_quality_counters,
    ),
    (
        26,
        "drop the unsmoothed ewma_new_per_scrape duplicate of last_new_count",
        _v26_drop_new_per_scrape_ewma,
    ),
    (
        27,
        "drop the taxonomy_hash columns left by the retired keyword taxonomy",
        _v27_drop_taxonomy_hash,
    ),
    (
        28,
        "drop the location weight left by the retired priority ranking",
        _v28_drop_location_weight,
    ),
    (29, "FTS5 index for dashboard job search", _v29_job_search_fts),
    (30, "persisted scheduler preferences", _v30_scheduler_preferences),
]


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """WAL so a 20-minute scrape does not block dashboard reads."""
    conn.execute("PRAGMA journal_mode=WAL")
    # 30s, not 5s. WAL gives concurrent readers but still one writer, and the pipeline now
    # has three writing stages on independent timers -- a scoring pass every 30 minutes can
    # land on top of the nightly research run. 5s was tuned for one writer and surfaced as
    # `database is locked` the first time two stages overlapped.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
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
            logger.exception(f"Migration v{target} failed and was rolled back")
            raise

    if applied:
        # A migration may have turned foreign_keys off to rebuild a referenced table.
        apply_pragmas(conn)
        logger.info(f"Schema now at v{current_version(conn)} ({applied} migration(s) applied)")
    return applied


if __name__ == "__main__":
    from findajob.core.paths import DB_PATH

    connection = sqlite3.connect(DB_PATH)
    try:
        migrate(connection)
        print(f"schema version: {current_version(connection)}")
    finally:
        connection.close()
