import type { Job } from "../../api/types";

// Where the posting sits in the partial order, not a percent. Tier 1 dominates everything
// below it; equal tiers are genuinely incomparable rather than equal.
// Ported from `macros/badges.html::tier_badge` / `tier_title`.

function tierTitle(job: Job): string {
  return [job.role_match, job.capability_match, job.seniority_gap]
    .filter(Boolean)
    .join(" · ")
    .replace(/_/g, " ");
}

export function TierBadge({ job }: { job: Job }) {
  if (job.fit !== null && job.fit !== undefined) {
    if (job.fit) {
      return (
        <span class="match-badge badge-top" title={job.reason_description || "Fit"}>
          Fit{job.reason_type ? ` · ${job.reason_type}` : ""}
        </span>
      );
    }
    return (
      <span class="match-badge badge-blocked" title={job.reason_description || "No fit"}>
        {job.reason_type || "no fit"}
      </span>
    );
  }
  if (job.eligibility === "blocked") {
    return (
      <span class="match-badge badge-blocked" title="A requirement you cannot meet">
        blocked
      </span>
    );
  }
  if (job.pareto_tier == null && job.fit_score == null) {
    return <span class="match-badge badge-unscored">unscored</span>;
  }
  const cls = (job.pareto_tier != null && job.pareto_tier <= 2) || (job.fit_score != null && job.fit_score >= 80)
    ? "badge-top"
    : (job.pareto_tier != null && job.pareto_tier <= 4) || (job.fit_score != null && job.fit_score >= 60)
      ? "badge-good"
      : "badge-far";
  return (
    <span class={`match-badge ${cls}`} title={tierTitle(job)}>
      {job.pareto_tier != null ? `tier ${job.pareto_tier}` : `${job.fit_score}/100`}
      {job.eligibility === "conditional" ? " *" : ""}
    </span>
  );
}
