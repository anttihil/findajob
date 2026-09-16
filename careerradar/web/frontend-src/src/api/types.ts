// Hand-written types for the `/api/*` JSON responses. Not codegen'd from `/openapi.json`:
// most of these routes return raw dict/SQLite-row shapes with no `response_model=`
// declared, so generated types would be `any` for exactly the fields that most need to be
// real (job rows, stats, skill-gap analysis). Revisit if `app.py` grows real Pydantic
// response models.

export type Liveness = "live" | "stale" | "likely_closed" | "unknown";
export type PipelineState = "new" | "scored";
export type JobStatus = "unread" | "saved" | "applied" | "rejected";

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
  tailored_resume_id?: number | null;
}

export interface JobsPage {
  jobs: Job[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

// GET /api/jobs/{id}/context
export interface JobContext {
  job: Job | null;
  resume?: GeneratedResumeRecord | null;
  requirement_rows?: unknown[];
  next_job_id: number | null;
}


export interface MasterEducation {
  institution: string;
  degree: string;
  details?: string | null;
}

export interface MasterSkillCategory {
  category: string;
  skills: string[];
}

export interface MasterProject {
  heading?: string | null;
  url?: string | null;
  bullets: string[];
}

export interface MasterRole {
  title: string;
  company: string;
  dates: string;
  location?: string | null;
  projects: MasterProject[];
}

export interface WorkEligibility {
  citizenship: string[];
  locations: string[];
  willing_to_relocate: boolean;
  comp_floor_usd?: number | null;
}

export interface RoleTargeting {
  target_roles: string[];
  work_modes: string[];
  target_industries: string[];
  dealbreakers: string[];
}

export interface Profile {
  name: string;
  email: string;
  phone: string;
  location: string;
  github: string;
  linkedin: string;
  website: string;

  eligibility: WorkEligibility;

  seniority?: string | null;
  years_experience?: number | null;
  executive_summary: string;
  model_guidance: string;
  dealbreakers: string[];

  experience: MasterRole[];
  projects: MasterProject[];
  skills: MasterSkillCategory[];
  education: MasterEducation[];

  // Backward compatibility shims
  summary_guidance?: string;
  targeting?: RoleTargeting;
}

export type ResumeMasterProfile = Profile;

export interface ProfileChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ProfileChatResponse {
  status: string;
  reply: string;
  updated_profile: ResumeMasterProfile;
  changes_made: string[];
}

export interface ResumeUploadResponse {
  status: string;
  filename: string;
  text_snippet: string;
  raw_text: string;
  profile: ResumeMasterProfile;
}

export interface GeneratedResumeRecord {
  id: number;
  job_id: number;
  profile_version?: number;
  model?: string;
  created_at: string;
  typst_path?: string;
  pdf_path?: string;
  resume?: Record<string, unknown>;
  summary?: string;
  ats_score?: number | null;
  ats_verdict?: string | null;
  ats_feedback?: string | null;
  status?: string;
  job_title?: string;
  job_company?: string;
  job_location?: string;
}

// GET /api/meta
export interface Meta {
  countries: [string, string][];
  verdicts?: [string, string][];
  reason_types?: [string, string][];
  fit_threshold: number;
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


export interface QueryYieldTuple {
  cell_id: number;
  source: string;
  query: string;
  location_id: string;
  enabled: boolean;
  search_query_id: number | null;
  total_postings: number;
  unique_postings: number;
  scored_postings: number;
  strong_fits: number;
  no_fits: number;
  fit_rate: number;
  fit_rate_pct: number;
  yield_rate: number;
  yield_rate_pct: number;
  total_scrapes: number;
  last_scraped_at: string | null;
  last_success_at: string | null;
  yield_category: "high_yield" | "moderate_yield" | "zero_yield" | "low_yield" | "unscored" | "unscraped";
}

export interface QueryTermYield {
  query: string;
  search_query_id: number | null;
  total_postings: number;
  unique_postings: number;
  scored_postings: number;
  strong_fits: number;
  no_fits: number;
  fit_rate: number;
  fit_rate_pct: number;
  yield_rate: number;
  yield_rate_pct: number;
  total_scrapes: number;
  sources: string[];
  locations: string[];
  cells_count: number;
  yield_category: "high_yield" | "moderate_yield" | "zero_yield" | "low_yield" | "unscored" | "unscraped";
}

export interface QueryYieldSummary {
  total_tuples: number;
  active_tuples: number;
  total_postings: number;
  total_unique_postings: number;
  total_scored: number;
  total_strong_fits: number;
  overall_fit_rate_pct: number;
  high_yield_queries_count: number;
  zero_yield_queries_count: number;
}

export interface SourceYield {
  source: string;
  total_postings: number;
  scored_postings: number;
  strong_fits: number;
  fit_rate_pct: number;
}

export interface LocationYield {
  location_id: string;
  location_label: string;
  total_postings: number;
  scored_postings: number;
  strong_fits: number;
  fit_rate_pct: number;
}

export interface MarketYieldResponse {
  window_days: number | null;
  source: string | null;
  location_id: string | null;
  query: string | null;
  summary: QueryYieldSummary;
  top_queries: QueryTermYield[];
  zero_yield_queries: QueryTermYield[];
  tuples: QueryYieldTuple[];
  by_source: SourceYield[];
  by_location: LocationYield[];
  provenance: {
    generated_at: string;
    window_days: number | null;
    exclude_agencies: boolean;
    search_targets_hash: string;
  };
}

export interface MarketLocation {
  id: string;
  label: string;
  country: string;
  is_remote: boolean;
}

export interface MarketLocationsResponse {
  locations: MarketLocation[];
  queries: string[];
}

export interface CoverageCell {
  source: string;
  location_id: string;
  query: string;
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


export interface SkillGapRow {
  skill: string;
  label: string;
  category: string;
  user_has: boolean;
  priority: number;
  blocking_gap: number;
  demand: number;
  demand_ci_low: number;
  demand_ci_high: number;
  adjacency: number;
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
  provenance: {
    n_postings?: number;
    n_good_fit?: number;
    good_fit_threshold?: number;
    window_below_minimum?: boolean;
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
  by_query: { query: string; n: number }[];
  postings: {
    url: string;
    title: string;
    company: string;
    match_score: number;
    in_title?: boolean;
  }[];
}


export interface ProfileSkill {
  key: string;
  label?: string;
  level: number;
  evidence?: string[];
}

export interface ProfileDocument {
  path: string;
  kind?: string;
  chars?: number;
}

export interface ProfileVectorResponse {
  version: number;
  updated_at?: string | null;
  model?: string;
  profile: ResumeMasterProfile;
  skills_vector: ProfileSkill[];
  summary_text: string;
}

export interface ProfileRecord {
  version?: number;
  updated_at?: string | null;
  created_at?: string | null;
  model?: string;
  seniority?: string | null;
  summary_guidance?: string | null;
  profile?: {
    name?: string;
    seniority?: string | null;
    bio?: string | null;
    summary_guidance?: string | null;
    skills?: ProfileSkill[];
  };
  skills_vector?: ProfileSkill[];
  summary_text?: string;
  documents?: ProfileDocument[];
}


// `ConfigUpdate` on the backend is `extra="allow"`, so there is no reason for the client
// side to be any stricter than the server it is posting to.
export type Config = Record<string, unknown>;


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

export interface ObservabilityPromptResponse {
  prompt_hash: string;
  rules: string;
  summary_text: string;
  system_prompt: string;
  updated_at?: string | null;
}

export interface TargetQuery {
  id: number;
  query: string;
  enabled: number | boolean;
}

export interface TargetLocation {
  id: string;
  label: string;
  search_label: string;
  country: string;
  indeed_country: string;
  is_remote: number | boolean;
  distance: number;
  enabled: number | boolean;
}

export interface TargetCapacity {
  active_queries: number;
  active_locations: number;
  search_pairs: number;
  total_cells: number;
  runs_per_day: number;
  daily_capacity_pairs: number;
  cycle_days: number;
  cycle_hours: number;
  zone: "optimal" | "balanced" | "overloaded" | "empty" | string;
  message: string;
  optimal_threshold_hours: number;
  balanced_threshold_hours: number;
}

export interface TargetsResponse {
  queries: TargetQuery[];
  locations: TargetLocation[];
}
