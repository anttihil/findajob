import type { Job } from "../../api/types";

export function TierBadge({ job }: { job: Job }) {
  if (job.priority) {
    const labels = { first: "Review first", consider: "Worth considering", low: "Low priority" };
    return (
      <span
        class={`match-badge ${job.priority === "first" ? "badge-top" : job.priority === "low" ? "badge-blocked" : "badge-unscored"}`}
        title={job.reason_description || ""}
      >
        {labels[job.priority]}
      </span>
    );
  }
  if (job.stage === "screening_excluded")
    return (
      <span class="match-badge badge-blocked" title={job.screening_reason || ""}>
        Screened out
      </span>
    );
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
  return <span class="match-badge badge-unscored">unscored</span>;
}
