// Hand-written types for the `/api/*` JSON responses. Not codegen'd from `/openapi.json`:
// most of these routes return raw dict/SQLite-row shapes with no `response_model=`
// declared, so generated types would be `any` for exactly the fields that most need to be
// real (job rows, stats, skill-gap analysis). Revisit if `app.py` grows real Pydantic
// response models.

export type Verdict = "strong" | "worth_applying" | "stretch" | "poor_fit" | "mismatch";
export type Eligibility = "eligible" | "conditional" | "blocked";
export type RoleMatch = "same_role" | "adjacent" | "different_domain" | "different_field";
export type CapabilityMatch =
  | "exceeds"
  | "meets"
  | "most_with_gaps"
  | "major_gaps"
  | "not_close";
export type SeniorityGap = "matched" | "candidate_above" | "candidate_below";
export type Liveness = "live" | "stale" | "likely_closed" | "unknown";
export type PipelineState = "new" | "scored" | "researched";
export type JobStatus = "unread" | "saved" | "applied" | "rejected";
export type Access = "commutable" | "remote" | "relocation";

export interface RequirementEntry {
  requirement: string;
  quote: string;
  importance: "must_have" | "important" | "nice_to_have" | string;
}

export interface RequirementAssessment {
  requirement: string;
  status: "met" | "partial" | "unmet";
  candidate_evidence?: string;
}

export interface RequirementSummary {
  must_total: number;
  must_met: number;
  must_partial: number;
  must_unmet: number;
  must_unassessed: number;
}

export interface HardBlocker {
  quote: string;
  why?: string;
}

// A job row from `Database.query_jobs` (list or detail mode -- `/api/jobs` always uses
// detail=True, so every field below is present on every row that endpoint returns).
export interface Job {
  id: number;
  job_key: string;
  title: string;
  company: string;
  location: string | null;
  country: string | null;
  url: string;
  description: string | null;
  source: string;
  match_score: number | null;
  matched_skills: string[];
  resume_match: string | null;
  status: JobStatus;
  date_found: string | null;
  date_applied: string | null;
  access: Access | null;

  role_family: string | null;
  seniority: string | null;
  is_remote: boolean | null;
  city: string | null;
  region: string | null;
  date_posted: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_annual_usd: number | null;

  // Joined from `job_verdicts` for the active profile version; null until scored.
  fit: boolean | number | null;
  reason_type: string | null;
  reason_description: string | null;
  pipeline_state: PipelineState | null;

  // Computed server-side, not raw columns.
  liveness: Liveness;
}

export interface JobsPage {
  jobs: Job[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface JobFilters {
  status?: JobStatus | "";
  access?: Access | "";
  country?: string;
  fit?: boolean | "";
  reason_type?: string;
  liveness?: Liveness | "";
  limit?: number;
  offset?: number;
}

export interface Dossier {
  intel: {
    summary?: string;
    size?: string;
    stage?: string;
    funding?: string;
    concerns?: string[];
    application_angle?: string;
  } | null;
  contacts: { name: string; role?: string; public_url?: string; relevance?: string }[];
  nearby_jobs: { url?: string; title: string; company: string; source: string }[];
  sources: string[];
}

export interface RequirementRow {
  requirement: string;
  importance: string;
  quote: string;
  status: "met" | "partial" | "unmet" | "unassessed";
  evidence: string;
}

// GET /api/jobs/{id}/context
export interface JobContext {
  job: Job | null;
  dossier: Dossier | null;
  requirement_rows: RequirementRow[];
  next_job_id: number | null;
}

// GET /api/meta
export interface Meta {
  countries: [string, string][];
  verdicts?: [string, string][];
  reason_types?: [string, string][];
}

export interface StatusCounts {
  unread: number;
  saved: number;
  applied: number;
  rejected: number;
}

export interface Stats {
  status_counts: StatusCounts;
  total_jobs: number;
  skill_coverage: { matched: number; required: number; ratio: number | null };
  country_counts: Record<string, number>;
  pipeline_counts: Record<string, number>;
  ordinals?: Record<string, Record<string, number>>;
  strong_matches: number;
  liveness_counts: { live: number; stale: number; likely_closed: number; unknown: number };
}

// --- Market -------------------------------------------------------------------------

export interface MarketSupplyRow {
  role_family: string;
  label: string;
  flow_per_day: number | null;
  censored: boolean;
  zero_yield: boolean;
  n_postings?: number;
  n_companies?: number;
  coverage_fraction?: number;
  suppressed_reason?: string;
}

export interface MarketSupplyResponse {
  rows: MarketSupplyRow[];
  provenance: {
    published_rows: number;
    total_rows: number;
    window_days: number;
    window_below_minimum: boolean;
    min_window_days: number;
  };
}

export interface MarketLocation {
  id: string;
  label: string;
  country: string;
  is_remote: boolean;
}

export interface MarketRoleFamily {
  key: string;
  label: string;
  tier: number;
  resume: string;
}

export interface MarketLocationsResponse {
  locations: MarketLocation[];
  role_families: MarketRoleFamily[];
}

export interface CoverageCell {
  source: string;
  location_id: string;
  role_family: string;
  tier: number;
  total_scrapes: number;
  hours_since_success: number | null;
  last_result_count: number | null;
  consecutive_error: number;
  consecutive_empty: number;
  backoff_until: string | null;
  last_saturated: boolean;
}

export interface MarketCoverageResponse {
  cells: CoverageCell[];
}

// --- Skills -------------------------------------------------------------------------

export interface SkillGapRow {
  skill: string;
  label: string;
  category: string;
  priority: number;
  blocking_gap: number;
  demand: number;
  demand_ci_low: number;
  demand_ci_high: number;
  adjacency: number;
  effort: string;
  n_raw: number;
  n_companies: number;
  user_level?: number;
  salary_lift?: number;
}

export interface SuppressedSkill {
  skill: string;
  label: string;
  reason: string;
  n_raw: number;
}

export interface SkillGapResponse {
  window_days: number;
  weighting_mode: string;
  weighting_effective?: string;
  weighting_fallback_reason?: string;
  provenance: {
    n_postings?: number;
    n_eff?: number;
    n_good_fit?: number;
    good_fit_threshold?: number;
    window_below_minimum?: boolean;
    strata_used?: number;
    strata_available?: number;
    cold_start?: boolean;
    residual_bias_note?: string;
  };
  views: {
    priority_gaps: SkillGapRow[];
    validated_strengths: SkillGapRow[];
    dead_weight: SkillGapRow[];
    suppressed: SuppressedSkill[];
  };
}

export interface SkillDetailResponse {
  skill: string;
  label: string;
  category: string;
  user_has: boolean;
  user_level?: number;
  window_days: number;
  evidence: string[];
  cooccurring: { label: string; n: number; user_has: boolean }[];
  by_role_family: { label: string; n: number }[];
  postings: {
    url: string;
    title: string;
    company: string;
    match_score: number;
    in_title?: boolean;
  }[];
}

// --- Profile --------------------------------------------------------------------------

export interface ProfileSkill {
  key: string;
  label?: string;
  level: number;
}

export interface ProfileDocument {
  path: string;
  kind?: string;
  chars?: number;
}

export interface ProfileRecord {
  version: number;
  created_at: string;
  model?: string;
  profile: {
    seniority?: string;
    bio?: string;
    skills: ProfileSkill[];
  };
  documents: ProfileDocument[];
}

export interface ProfileVersionSummary {
  version: number;
  created_at: string;
  active: boolean;
}

// --- Config -----------------------------------------------------------------------------

// `ConfigUpdate` on the backend is `extra="allow"`, so there is no reason for the client
// side to be any stricter than the server it is posting to.
export type Config = Record<string, unknown>;

// --- Search links -----------------------------------------------------------------------

export interface SearchLinksResponse {
  search_query_used: string;
  linkedin: string;
  indeed: string;
}

// --- Sync / pipeline --------------------------------------------------------------------

export interface SyncPlanTask {
  location_id: string;
  role_family: string;
  [key: string]: unknown;
}

export interface SyncPlanResponse {
  source: string;
  cells_total: number;
  cells_planned: number;
  tasks: SyncPlanTask[];
}

export interface PipelineScrapeStatus {
  in_progress: boolean;
  started_at?: string | null;
  previous_run?: {
    last_run?: string;
    hours_since?: number;
    status?: string;
    cells?: [number, number];
    postings_new?: number;
  } | null;
  cells_done?: number;
  cells_planned?: number;
}

export interface PipelineScoreStatus {
  backlog: number;
  backlog_share: number;
  last_verdict?: string | null;
  hours_since?: number;
  recent_verdicts_5min: number;
}

export interface PipelineStatusResponse {
  scrape: PipelineScrapeStatus;
  score: PipelineScoreStatus;
}

// --- Digests --------------------------------------------------------------------------

export interface DigestSummary {
  filename: string;
  date_created: string;
  size_bytes: number;
}

export interface DigestContent {
  content: string;
}

// --- Observability --------------------------------------------------------------------

export interface ObservabilityReason {
  reason_type: string;
  count: number;
  avg_tokens_out: number;
  percentage: number;
}

export interface ObservabilityModel {
  model: string;
  count: number;
  total_tokens_out: number;
  avg_tokens_out: number;
  total_cost_usd: number;
}

export interface ObservabilityStats {
  total_verdicts: number;
  total_tokens_in: number;
  total_tokens_cached: number;
  total_tokens_out: number;
  cache_hit_rate: number;
  avg_tokens_out: number;
  min_tokens_out: number;
  max_tokens_out: number;
  avg_tokens_in: number;
  total_cost_usd: number;
  avg_cost_usd: number;
  fit_count: number;
  no_fit_count: number;
  fit_rate: number;
  reasons: ObservabilityReason[];
  models: ObservabilityModel[];
}

export interface ObservabilityVerdictItem {
  id: number;
  job_id: number;
  title: string;
  company: string;
  location: string | null;
  url: string;
  fit: boolean;
  reason_type: string;
  reason_description: string;
  tokens_in: number;
  tokens_cached: number;
  tokens_out: number;
  cost_usd: number;
  model: string;
  created_at: string;
}

export interface ObservabilityVerdictsResponse {
  items: ObservabilityVerdictItem[];
  total: number;
  limit: number;
  offset: number;
}

