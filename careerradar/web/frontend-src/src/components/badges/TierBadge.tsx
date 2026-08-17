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
  if (job.eligibility === "blocked") {
    return (
      <span class="match-badge badge-blocked" title="A requirement you cannot meet">
        blocked
      </span>
    );
  }
  if (job.pareto_tier == null) {
    return <span class="match-badge badge-unscored">unscored</span>;
  }
  const cls = job.pareto_tier <= 2 ? "badge-top" : job.pareto_tier <= 4 ? "badge-good" : "badge-far";
  return (
    <span class={`match-badge ${cls}`} title={tierTitle(job)}>
      tier {job.pareto_tier}
      {job.eligibility === "conditional" ? " *" : ""}
    </span>
  );
}
