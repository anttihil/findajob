import type { Job } from "../../api/types";

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
  return <span class="match-badge badge-unscored">unscored</span>;
}
