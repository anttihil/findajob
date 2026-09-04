# Architectural Plan: Decoupling from `skills.yaml`

## 1. Executive Summary & Problem Statement

CareerRadar transitioned from an early deterministic keyword-matching system (`JobScorer`, BM25) to an LLM-agentic pipeline for candidate-job fit evaluation. However, the static taxonomy file (`data/skills.yaml`) remains embedded across several core components.

Retaining this hardcoded taxonomy introduces three significant architectural issues:

1. **Prevents Generalization Across Industries:**
   * `skills.yaml` hardcodes 172 skills exclusively for software engineering, DevOps, and cloud/AI infrastructure.
   * Applying CareerRadar to any other discipline (e.g., healthcare, finance, legal, sales, biotechnology) currently requires authoring and maintaining a massive custom YAML file.
   * Even within software engineering, it requires perpetual manual maintenance as new frameworks, tools, and languages emerge.

2. **Regex Extraction is Brittle:**
   * Standard regex token boundaries (`\b`) fail on non-alphanumeric tokens (e.g., `C++`, `C#`, `.NET`).
   * Short tokens (`go`, `r`, `ray`, `spark`) suffer from high false-positive rates, requiring fragile heuristics like `strict_aliases` constrained to a 120-character proximity context window.
   * Keyword matching fails on natural semantic phrasing (e.g., *"architecting event-driven pipelines"* fails to match `kafka` unless explicitly written).

3. **Anchoring Bias in LLM Scoring:**
   * In `careerradar/scoring/prompts.py`, the scoring prompt injects a `<taxonomy_signal>` section listing *"candidate profile has: ... / posting asks, not on profile: ..."*.
   * This explicit diff primes the LLM with a checklist mindset, nudging it to penalize candidates for missing superficial keywords rather than assessing transferable skills, seniority slope, and holistic fit.

---

## 2. Current Footprint of `data/skills.yaml`

Before refactoring, the components relying on `data/skills.yaml` and `Taxonomy` must be mapped:

| Subsystem | File(s) | Role of `skills.yaml` |
| :--- | :--- | :--- |
| **Scoring Prompt** | `careerradar/scoring/prompts.py`, `careerradar/scoring/worker.py` | Injects `<taxonomy_signal>` (`evidenced` vs `missing` skills) into the LLM prompt. |
| **Search Ingestion** | `careerradar/search/runner.py` | Scans job descriptions via `taxonomy.extract()` and writes canonical skill IDs into the `job_skills` table. |
| **Market Analytics** | `careerradar/market/gap_analysis.py`, `careerradar/market/repository.py` | Aggregates skill demand, blocking gaps, and salary lift from `job_skills`. |
| **Profile Ingestion** | `careerradar/profile/builder.py` | Canonicalizes resume/interview skill strings via `taxonomy.canonicalize()`. |
| **Eligibility Blockers** | `careerradar/taxonomy/skills.py` | Defines clearance regex patterns (`blockers:` section like `top_secret`, `polygraph`). |
| **Audit & Integrity** | `careerradar/core/status_repository.py` | Uses `taxonomy.hash` in `jobs.taxonomy_hash` and `sync_runs` to detect taxonomy drift. |

---

## 3. Phased Implementation Roadmap

To avoid breaking active scraping and dashboard analytics, the migration should follow a 4-phase rollout:

```mermaid
flowchart TD
    Phase1["Phase 1: Decouple LLM Scoring Prompt<br/>(Toggle off skill_hint, remove LLM bias)"]
    Phase2["Phase 2: Decouple Profile Canonicalization<br/>(Allow open-vocabulary candidate skills)"]
    Phase3["Phase 3: Generalize Market & Gap Analytics<br/>(Dynamic extraction / semantic embeddings)"]
    Phase4["Phase 4: Deprecate & Remove skills.yaml<br/>(Retire legacy JobScorer & regex taxonomy)"]

    Phase1 --> Phase2 --> Phase3 --> Phase4
```

---

### Phase 1: Decouple LLM Scoring from `skill_hint` (Immediate / Low Risk)

**Goal:** Eliminate LLM anchoring bias immediately with minimal code changes.

1. **Add Configuration Toggle in `config.yaml`:**
   ```yaml
   scoring:
     include_skill_hint: false  # Default to false to eliminate prompt bias
   ```
2. **Update `careerradar/scoring/worker.py`:**
   * In `_skill_hint()`, respect the configuration flag:
     ```python
     if not config.get("scoring", {}).get("include_skill_hint", False):
         return ""
     ```
   * When disabled, `render_posting()` simply outputs the raw posting `<title>`, `<company>`, `<location>`, `<facts>`, and `<description>` without `<taxonomy_signal>`.
3. **Verification:**
   * Run metamorphic evaluations (`uv run python -m evals.metamorphic`) to verify that verdict quality and reasoning fidelity improve or hold steady.
   * Compare verdict distributions on sample backlog jobs with and without the hint.

---

### Phase 2: Decouple Profile Generation from Static Taxonomy

**Goal:** Allow candidates to define skills openly across any domain or industry without requiring entries in `skills.yaml`.

1. **Update `careerradar/profile/builder.py`:**
   * Currently, `taxonomy.canonicalize()` filters and drops unrecognized skills.
   * Switch to open-vocabulary extraction: allow the profile builder LLM to extract normalized skill strings directly from resumes without dropping skills not present in `skills.yaml`.
2. **Preserve Subsumption in Profile:**
   * Replace the hardcoded `implies:` graph in YAML with LLM-generated related capabilities or hierarchical grouping within the profile JSON.

---

### Phase 3: Generalize Market & Gap Analytics (Completed)

**Goal:** Remove dependence of `job_skills` and market reporting on a 1,000-line hardcoded regex file.

1. **Target-Driven & Open-Vocabulary Extraction (Implemented):**
   * Extended `Taxonomy` with `Taxonomy.from_profile(profile, domain_skills=...)` and `enrich_from_profile(profile)` to derive vocabulary directly from the candidate's active profile and target domain.
   * `Skill` now compiles boundary-safe alternation using `(?<!\w)...(?!\w)` to properly match non-alphanumeric tokens (`C++`, `C#`, `.NET`, `A/B testing`) and open-vocabulary skills without manual regex authoring.
   * `Taxonomy.clone()` ensures cached base taxonomies remain immutable when enriched per-profile.
2. **Decouple Market Analytics & Gap Analysis (Implemented):**
   * `GapAnalysis`, `MarketAnalytics`, and `JobScorer` now accept `taxonomy: Taxonomy | None = None`.
   * Open-vocabulary skills not in `skills.yaml` cleanly resolve label, category, and effort from `profile.skills` or clean fallback.
   * `market_repo.skill_exists` added to verify skills directly in the database.
   * `/api/skills/{skill}` now supports any skill in the candidate's profile or observed in market postings, eliminating false 404s for non-YAML skills.
3. **Migrate Hard Blocker Patterns (Implemented):**
   * Decoupled `blockers:` from `skills.yaml` into `careerradar/taxonomy/blockers.py` (`DEFAULT_BLOCKERS`, `Blocker`, and `BlockerExtractor`).
   * Supports custom blocker overrides from `config.yaml` and dynamic candidate dealbreakers from `profile.dealbreakers` / `profile.dealbreakers_json`.
   * `Taxonomy.extract_blockers()` and `runner.py` leverage the decoupled blocker extractor with full backward compatibility.

---

### Phase 4: Deprecate and Retire `data/skills.yaml` (Completed)

**Goal:** Clean removal of the legacy scaffold.

1. **Retire `careerradar/search/keyword_score.py` (`JobScorer`):**
   * Marked `keyword_score.py` and `JobScorer` as deprecated and retired with runtime `DeprecationWarning`.
   * Updated `runner.py` to decouple scraping from mandatory keyword scoring (`scorer: JobScorer | None`), cleanly defaulting to `match_score: 0` without requiring deterministic scoring.
   * `scoring/worker.py` no longer invokes `JobScorer` when `include_skill_hint` is disabled.

2. **Deprecate `jobs.taxonomy_hash` & Re-point Provenance:**
   * Deprecated `jobs.taxonomy_hash` in favor of tracking evaluation provenance via `profile_version` and `prompt_hash` in `job_verdicts`.
   * Updated `status.py` output to reflect that `jobs.taxonomy_hash` is deprecated and show `skills` hash dynamically.
   * `runner.py` now writes `tax_hash` only if taxonomy has skills.

3. **Safely Remove `data/skills.yaml` & Simplify `careerradar/taxonomy/`:**
   * Removed `data/skills.yaml` from the repository entirely.
   * Updated `careerradar/taxonomy/skills.py` (`Taxonomy` and `load_taxonomy()`) to operate as an open-vocabulary engine that dynamically populates from candidate profiles or runs cleanly empty with default blockers.
   * Unit tests updated to run with decoupled test fixtures, preserving 100% test coverage across all taxonomy regex, extraction, blocker, and scoring functionality.

---

## 4. Key Metrics for Success

* **Domain Agnosticism:** The system can be configured for non-tech roles (e.g. Registered Nurse, Corporate Counsel) with zero modifications to python or YAML files.
* **Scoring Accuracy:** Metamorphic test pass rate remains at 100%, with lower rates of false-negative rejections caused by missing keyword synonyms.
* **Maintainability:** Elimination of regex edge cases (`strict_aliases`, `context` windows, token boundary regexes).
