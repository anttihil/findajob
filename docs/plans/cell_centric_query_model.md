# Architectural Plan: Cell-Centric Query Model

## 1. Problem Statement

The app has three core features: **search, scoring, and resume generation**. Everything else
is secondary and is either removed here or left running in a simpler form pending a later
decision.

`scrape_cells` holds only `(source, role_family, location_id, query, tier)`. The parameters
that JobSpy actually receives -- `search_label`, `country`, `indeed_country`, `is_remote`,
`distance` -- live in `target_locations` and are joined in at task-build time
(`search/scheduler.py:269-291`). Query provenance is therefore split across four tables, and
recovering it needs brittle joins.

Three consequences:

1. **Text joins to recover identity.** `market/repository.py:80` re-joins `target_queries`
   to `scrape_cells` on `(role_key = sc.role_family AND query = sc.query)` -- two text
   columns -- purely to get a `target_query_id`. Any query edit breaks it silently.

2. **Dead mappings from the removed regex NLP** (commit `42179cd`). The former classifier,
   `classify_all`, `classify_access`, `is_excluded` and `seniority` have zero callers outside
   `taxonomy/roles.py`. `target_roles.aliases_json` is still written by `cli.py:183` and
   `web/app.py:702` but read by nothing. `runner.py:351` sets
   `stats["on_topic"] = len(postings)`, so `cell_observations.returned_on_topic` no longer
   measures the thing its comment describes. About 300 of `roles.py`'s 546 lines are inert.

3. **Two columns caught mid-migration.** Every row in `jobs.db` was written by the old regex
   path (last scrape 2026-09-04 18:39, same day as the removal), so:
   * 11073 of 15538 joined rows disagree with their cell's `role_family`; new rows copy the
     cell verbatim (`runner.py:329`). The column now holds two meanings.
   * `jobs.access` has 2673 `commutable` rows from the dead `classify_access`, while the
     runner now writes only `remote` / `unspecified` (`runner.py:331`).
   * `jobs.role_family_hint` (`normalizer.py:408`) is an exact copy of `role_family`.

### Design decisions taken

* **The research feature is removed.** It is non-functional (evidence in section 3) and it is
  one of the two consumers of the `role_family` grouping that would otherwise need a
  replacement.
* **`reference_mix` and post-stratification weighting are removed.** This is the other such
  consumer. Skill demand and market analysis are deferred; the gap report keeps running on
  plain unweighted counts until that decision is made.
* **`role_family` is dropped, not repaired.** It began as a semantic grouping for regexes and
  search terms. With the regexes gone, the query string is the unit. "Full Stack Engineer"
  and "Full Stack Developer" are separate entities, and that is acceptable.
* **`access` / commutability is dropped.** JobSpy's `distance` radius already covers it. The
  dashboard filters by location instead. A preferred-location user setting may be added
  later; it must affect display only, never search or scoring.

With research and weighting both gone, **no site does semantic work with `role_family`.** It
becomes a pure rename to `query`.

---

## 2. Current Footprint

| Concept | Files | Count |
| :--- | :--- | :--- |
| `role_family` | `market/repository.py` (16), `core/migrations.py` (13), `market/gap_analysis.py` (11), `search/repository.py` (10), `core/job_repository.py` (9), `market/analytics.py` (7), `web/app.py` (6), `research/graph.py` (6), `search/scheduler.py` (5), `research/repository.py` (5), `core/database.py` (4), `taxonomy/roles.py` (3), `search/runner.py` (3), `research/tools.py` (2), `search/importer.py` (1), `research/worker.py` (1) | 102 refs |
| `access` | `web/app.py` (8), `taxonomy/roles.py` (8), `core/migrations.py` (8), `core/job_repository.py` (6), `taxonomy/repository.py` (5), `core/database.py` (4), `scoring/prompts.py` (2), `web/rendering.py` (1), `search/runner.py` (1), `search/repository.py` (1) | 44 refs |
| research | `careerradar/research/` (896 lines) + `cli.py`, `web/app.py`, `core/scheduler.py`, `core/status.py`, `core/status_repository.py`, `core/config.py`, `core/pipeline_lock.py` | ~40 external refs |
| weighting | `market/gap_analysis.py` (61 `weight` refs of 585 lines), `market/analytics.py:49-109`, `config.yaml:250,271-291`, `core/config.py:93`, `web/app.py:787`, `SkillsPage.tsx`, `api/types.ts:382-392` | ~90 refs |
| Frontend | `SearchTargetsWidget.tsx` (32), `api/types.ts` (8), `lib/filterQuery.ts` (5), `market/MarketPage.tsx` (4), `dashboard/FilterSidebar.tsx` (3), `badges/AccessBadge.tsx` (3), `skills/SkillDrawer.tsx` (1), `JobCard.tsx` (1), `dashboard/DossierPanel.tsx` | 57 refs + dossier UI |

Scoring does **not** read `role_family`, and resume generation touches none of this. Both core
features are unaffected by every phase except the migration's column drops.

### Remaining decision sites after phases 1 and 4

`scoring/prompts.py:79-80` injects `access: relocation` into the LLM prompt from the dead
classifier -- it is currently feeding the scorer a wrong fact. Phase 3 removes it.

That is the only one left. `gap_analysis.py:163-172` (stratum key), `config.yaml:272-291`
(`reference_mix`), and `research/repository.py:213-232` (`nearby_company_openings`) all
disappear rather than needing a decision.

---

## 3. Measurements Taken Before Planning

### The research feature is dead

| evidence | value |
| :--- | :--- |
| `company_dossiers` rows | 1 |
| `research_runs` rows | 3 |
| `jobs.dossier_id` populated | 5 of 15659 |
| `jobs.pipeline_state = 'researched'` | **0** (only `new` 9279, `scored` 6380) |

The auto-chain has never fired. `core/scheduler.py:394-398` selects `d.company` and
`d.researched_at` from `company_dossiers`, which has neither column -- it has
`company_normalized` and `generated_at`. The query raises
`sqlite3.OperationalError: no such column: d.company`, which the bare `except` at
`scheduler.py:412` swallows into a `logger.debug`. So `score_triggers_research` has been a
no-op since v5.

Removal also drops the `langchain-tavily` dependency (`pyproject.toml:18`) and the
`TAVILY_API_KEY` requirement (`.env.example:10`) -- one fewer API key for a new user to
obtain, which matters for the generalisation goal.

### The weighting layer collapses to identity

`reference_mix` (`config.yaml:272-291`) is 20 hand-written `role_family|location_id` weights,
three of whose keys (`academic_technology`, `tpm`, `product_manager`) no longer exist in
`target_queries`. With it gone, every posting weight is 1.0, so:

| function | with uniform weights |
| :--- | :--- |
| `kish_n_eff([1.0] * n)` (`analytics.py:49`) | returns `n` exactly |
| `weighted_median` (`analytics.py:60`) | a plain median |
| `weighted_count` / `total_weight` / `blocking_weight` | plain counts |

So the 61 `weight` references in `gap_analysis.py` become no-ops rather than arithmetic.
Leaving them in place would be dead weight, so phase 4 collapses them.

`skill_market_stats` (the materialised table carrying `reference_mix_hash` and
`weighting_mode`, `migrations.py:233-259`) is **already gone** -- it is absent from `.tables`
and no code reads it. Nothing to do there.

### Job-to-cell cardinality (the reason phase 7 exists)

| measure | count |
| :--- | ---: |
| jobs with `times_seen > 1` | 4728 / 15659 (30%) |
| content-hash groups spanning >1 cell | 415 |
| ...spanning >1 query | 374 |
| ...spanning >1 location | 77 |

`upsert_posting` (`search/repository.py:443-450`) has `scrape_cell_id` in its updatable set,
so a re-found posting's cell is last-writer-wins.

### Cell matrix

588 enabled cells = 42 queries x 7 locations x 2 sources; 252 disabled remnants.
`scrape_cells.tier` holds a mix of `1` (700), `breadth` (64), `core` (60), `adjacent` (16) and
is read by nothing -- `seed_cells` only compares it to itself (`search/repository.py:43-44`).

---

## 4. Target Schema

```
search_queries(id, query, enabled)            -- flat; role_key FK removed
search_locations(id, label, search_label, country, indeed_country,
                 is_remote, distance, weight, enabled)   -- access removed

scrape_cells(id, source, location_id, query,
             search_label, country, indeed_country,      -- embedded JobSpy params
             is_remote, distance,
             weight,                                     -- embedded scheduler priority
             enabled, last_scraped_at, last_success_at, ...)
             UNIQUE(source, location_id, query)          -- role_family, tier removed

jobs(..., scrape_cell_id)                                -- role_family,
                                                         -- role_family_hint,
                                                         -- access,
                                                         -- dossier_id removed
```

Deleted tables: `target_roles`, `company_dossiers`, `research_runs`.

`search_locations.weight` survives as the definition. After phase 4 its only consumer is
scheduler priority, which is what it was for, and phase 6 copies it onto the cell alongside
the JobSpy parameters so `cell_priority` needs no lookup either. `with_location_weights`
and the `scraper.locations` config key both disappear.

**Snapshot semantics:** `seed_cells` *updates* the embedded columns when a target definition
changes. Cells are not immutable snapshots. Per-run provenance already exists --
`cell_observations` copies the search parameters on every attempt -- so reuse that mechanism
rather than adding a second one.

---

## 5. Phased Roadmap

Phases 1-5 are deletion and rename; none touches search, scoring, or resume generation.
Phase 6 is the schema change. Phase 7 is optional.

### Phase 1: Remove the research feature

* Delete `careerradar/research/` (6 files, 896 lines) and `tests/test_research_module.py`.
* `cli.py`: remove `_cmd_research` (`:47-52`), the `research` subparser (`:383-389`), and the
  stage line in the module docstring (`:7`).
* `core/scheduler.py`: remove the `research` lock (`:45`), the daily loop registration
  (`:86`), the `timeout_minutes` special case (`:269`), `_trigger_research_if_needed`
  (`:384-413`), and the `score_triggers_research` branch (`:366-367`).
* `core/config.py`: remove the `research` defaults block (`:108-113`) and the
  `score_triggers_research` key (`:115`).
* `core/status_repository.py:97-104` and `core/status.py:79-85`: remove the research section.
* `core/pipeline_lock.py:3`: drop `research` from the stage list in the docstring.
* `web/app.py`: remove `dossier_for` (`:1172-1184`), the `/api/companies/{company}/dossier`
  endpoint (`:1187-1194`), `research` from the stage allowlist (`:1252`), `researched` from
  the `pipeline_state` pattern (`:298`), and the `dossier` key from the job-drawer payload
  (`:1340,1348,1364`).
* Frontend: delete `routes/dashboard/DossierPanel.tsx` and its styles in `style.css`; remove
  the dossier fetch from `api/client.ts`, the types from `api/types.ts`, the panel mount in
  `JobDrawer.tsx`, and the dossier helper in `lib/format.ts`.
* `config.yaml:370-379`: delete the `research:` block.
* `pyproject.toml:18`: drop `langchain-tavily`. `.env.example:10`: drop `TAVILY_API_KEY`.
  Re-lock with `uv lock`.
* Leave the tables and `jobs.dossier_id` in place; phase 6 drops them.

**Exit check:** `find-a-job --help` lists no research stage; the scheduler runs search and
score; the job drawer opens without a dossier panel; `find-a-job status` prints without a
research section.

### Phase 2: Delete dead taxonomy code (no migration)

* `taxonomy/roles.py`: remove `classify`, `classify_all`, `classify_access`, `is_commutable`,
  `is_excluded`, `seniority`, `_earliest_match`, `compile_aliases`, `RoleFamily.find`,
  `DEFAULT_SENIORITY_PATTERNS`, `DEFAULT_WEAK_PATTERNS`, `DEFAULT_EXCLUSIONS`,
  `META_REGEX_TOKENS`, `_init_commutable_area`. Drop `aliases` from `RoleFamily`.
* `validate()`: drop the "no patterns or aliases" check (it now gates seeding on nothing).
  Keep the `search_label` parenthetical check.
* Remove `--aliases` from `cli.py:431`, `aliases` from `web/app.py:274,702`, and
  `aliases_json` from `taxonomy/repository.py`.
* `runner.py:351` + `search/repository.py:316,335,353` + `core/database.py:291,306`: remove
  `returned_on_topic` plumbing; keep the column for now (dropped in phase 6).
* Remove `tier` from `roles.cell_specs()`, `seed_cells`, and `seed.py`'s "cells re-synced"
  line.

**Exit check:** `find-a-job search seed-cells` reports 588 cells; a dry-run scrape produces
the same task list as before.

### Phase 3: Delete `access` end to end

* Drop `ACCESS_COMMUTABLE` / `ACCESS_REMOTE` / `ACCESS_RELOCATION` / `ACCESS_LEVELS` and
  `Location.access` from `roles.py`.
* `runner.py:331`: stop writing `posting["access"]`.
* `scoring/prompts.py:79-80`: remove the `access:` fact. **This is a bug fix** -- the value
  comes from the dead classifier.
* `core/job_repository.py`: remove the `access` parameter from the three signatures
  (`:161,237,317`) and the filter tuple entry (`:187`).
* `taxonomy/repository.py`: remove `access` from `save_target_location` / `get_target_locations`.
* `web/app.py:650,1392`, `web/rendering.py:22`: remove.
* Frontend: delete `badges/AccessBadge.tsx` and its use in `JobCard.tsx`; remove the access
  block from `FilterSidebar.tsx:70-78`; remove `access` from `lib/filterQuery.ts:13,26,50,77`
  and `api/types.ts`; strip `access` from the 25 `LOCATION_PRESETS` entries and the
  `locAccess` state in `SearchTargetsWidget.tsx`.

**Exit check:** the dashboard renders and filters; a scoring run's prompt contains no
`access:` line.

### Phase 4: Remove post-stratification weighting

The gap report keeps working; it reports plain counts instead of raked estimates.

* `config.yaml`: delete `reference_mix` (`:272-291`), `weighting_mode` (`:271`),
  `min_stratum_n`, `max_missing_weight`, and `min_n_eff_for_scope` (`:250`).
  `core/config.py:93`: delete the `weighting_mode` default.
* `market/analytics.py`: delete `build_stratum_weights` (`:75-109`), `kish_n_eff` (`:49-58`)
  and `weighted_median` (`:60-73`). Replace the median call with `statistics.median`.
* `market/gap_analysis.py:161-212`: delete the stratum block, the fallback branch, and
  `weight_of` / `total_weight` / `good_fit_weight`. In `_per_skill_stats` (`:258-330`) drop
  `weighted_count`, `weights`, and `blocking_weight`; keep `raw_count` and add a plain
  `blocking_count`. `n_eff` becomes `n`.
* Payload (`gap_analysis.py:220-254`): drop `weighting_mode`, `weighting_effective`,
  `weighting_fallback_reason`, `missing_weight`, `strata_used`, `strata_available`. Keep
  `cold_start` keyed on raw `n`.
* `web/app.py:787`: drop the `weighting` query parameter.
* Frontend: `SkillsPage.tsx:141,143,153` remove the weighting and strata chips and the
  fallback warning; `api/types.ts:382-392` remove the fields.

**Exit check:** the skills page renders with no weighting chips and no fallback banner; the
gap report returns a number for every skill that passes `min_postings_for_skill`.

### Phase 5: Rename `role_family` to `query`

No decisions remain -- phases 1 and 4 removed both semantic consumers.

* `market/analytics.py:187-240,313,404` and `market/repository.py`: group by `sc.query`.
  The `target_queries` text join at `market/repository.py:80` is **kept**, reduced to the
  single column: the market page's "disable this query" button needs the row id, and once
  `search_queries.query` is UNIQUE (phase 6) the join is on a real key rather than on a
  `(role_key, query)` pair that any edit could break. The payload field is renamed
  `target_query_id` -> `search_query_id`.
* Delete `roles.label()` and `target_roles.label`. The 42 query strings are already
  display-quality title case ("Cloud Infrastructure Engineer", "Site Reliability Engineer"),
  so no mapping is needed.
* `gap_analysis.py:576-583`: `by_role_family` -> `by_query`.
* `market/repository.py:126`: keep the `location_id` filter -- it already routes through
  `scrape_cell_id IN (SELECT id FROM scrape_cells WHERE location_id = ?)` and is now the
  primary filter path.
* Frontend: `MarketPage.tsx`, `SkillDrawer.tsx`, `api/types.ts` rename `role_family` -> `query`.

**Exit check:** the market page renders with 42 query rows.

### Phase 6: Migration v24 -- embed and drop

Single migration, `_v24_cell_centric_queries`, following the existing
`SCHEMA_VERSION` / `MIGRATIONS` pattern in `core/migrations.py`.

1. Add `search_label`, `country`, `indeed_country`, `is_remote`, `distance` to
   `scrape_cells`; backfill from `target_locations` by `location_id`.
2. Rebuild `scrape_cells` without `role_family` and `tier`; new
   `UNIQUE(source, location_id, query)`. Collapse any rows that now collide (same
   source/location/query under different old families).
   **Measured:** zero colliding pairs -- 840 cells over 840 distinct
   `(source, location_id, query)` in `jobs.db`, 698 over 698 in `prod_jobs.db`, and
   `target_queries` holds 42 rows with 42 distinct query strings. The merge rule is
   therefore a safety net, not a data change: the row with the most `total_scrapes`
   keeps its history and adopts the others' postings and observations, ties going to the
   lowest id. The 252 disabled remnants do not collide and are kept -- 29 of them are
   still the `scrape_cell_id` of a stored posting and 33 carry observations.
3. Rebuild `cell_observations` without `role_family` and `returned_on_topic`; rebuild
   `idx_cellobs_scope` as `(query, location_id, source, observed_at)`.
4. Drop `jobs.role_family`, `jobs.role_family_hint`, `jobs.access`, `jobs.dossier_id`,
   `idx_jobs_access`. Drop tables `company_dossiers`, `research_runs`, `target_roles`, and
   `idx_dossiers_display`.
   Recreate `v_supply_eligible` and `v_skill_eligible` without the `role_family IS NOT NULL`
   predicate, and `cell_cost` without `o.role_family`.
   **Note the behaviour change:** that predicate used to exclude the ~13% of postings the
   regex left unclassified. Cell-derived provenance is never null, so the views widen. This
   already happens for rows written after `42179cd`; the migration makes it uniform.
5. Rename `target_queries` -> `search_queries`, drop `role_key`, dedupe by `query`.
   Rename `target_locations` -> `search_locations`, drop `access`.
6. Delete `with_location_weights` (`scheduler.py:110-115`) and the `roles.locations` lookup at
   `scheduler.py:269`; `build_task` reads the embedded columns off the cell directly.
   `scheduler.py:180` reads `weight` from the cell instead of `config["locations"]`.
7. `search/repository.py`: `POSTING_COLUMNS` loses `role_family`, `role_family_hint`,
   `access`. `seed_cells` upserts the embedded columns.

**Exit check:** `migrate()` runs clean on a copy of `jobs.db`; cell count is 588; a dry-run
scrape builds identical JobSpy calls to a pre-migration run of the same cells.

### Phase 7 (optional): `job_cells` junction

Only needed if per-query and per-location counts must be exact.

* Make `scrape_cell_id` non-updatable in `upsert_posting` -- it becomes "the cell that first
  found this posting".
* Add `job_cells(job_id, cell_id, first_seen_at, last_seen_at, PRIMARY KEY(job_id, cell_id))`,
  written on every upsert.
* Point the yield query (`market/repository.py:81`) and the dashboard location filter at
  `job_cells`.

**Why it may matter:** `upsert_posting` matches an existing row on `job_key` or `url`, so a
posting re-found by a second cell keeps one row and its `scrape_cell_id` is overwritten --
last writer wins. A posting found by both a Stockholm cell and a Remote cell is credited only
to whichever ran most recently. Cosmetic in the feed; a bias in the per-query yield metric.

The 415 content-hash groups spanning more than one cell do **not** measure this. Those are
separate rows linked by `duplicate_of`, and each keeps its own cell, so the yield query
already attributes them correctly (`COUNT(DISTINCT j.id)` per cell, with `duplicate_of IS
NULL` for the unique count). The rate of actual overwrites is unmeasured, because the
overwrite destroys its own evidence; `times_seen > 1` (4728 jobs) is only an upper bound.

`search/repository.py` therefore logs one `cell-reattribution` line at INFO whenever an
incoming `cell_id` differs from the stored one. Decide phase 7 on that count:

```
grep cell-reattribution app.log | wc -l
```

Note also that the location filter reaches only the market and skills pages
(`web/app.py:424,688`). The job feed filters on the `jobs.location` text column, not through
cells, so phase 3 did not raise the stakes for the dashboard.

---

## 6. Success Criteria

| # | Criterion |
| :--- | :--- |
| 1 | `roles.py` is under 150 lines and contains no `re` import. |
| 2 | No table stores `role_family` or `access`. |
| 3 | `build_task` constructs a JobSpy call from one `scrape_cells` row, with no join. |
| 4 | Adding a search location through the dashboard requires no config-file edit. |
| 5 | `config.yaml` declares no per-role or per-role-x-location weights. |
| 6 | The scoring prompt contains no field derived from deleted taxonomy code. |
| 7 | The pipeline has two stages, `search` and `score`, and needs one LLM API key. |
| 8 | Search, scoring, and resume generation behave identically before and after. |

---

## 7. Deferred

Skill demand and market analysis are left running on plain counts. Once the core three
features are settled, decide whether to keep, rebuild, or delete `careerradar/market/`
(1237 lines) and the `job_skills` table. Nothing in this plan makes that decision harder --
after phase 5 the market module reads `scrape_cells` and `job_skills` only.
