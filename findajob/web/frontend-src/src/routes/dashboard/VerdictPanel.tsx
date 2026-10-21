import type { Job } from "../../api/types";
import { TierBadge } from "../../components/badges/TierBadge";

export function VerdictPanel({ job }: { job: Job }) {
  if (
    job.priority ||
    job.stage === "screening_excluded" ||
    (job.fit !== null && job.fit !== undefined)
  ) {
    return (
      <>
        <div class="verdict-head">
          <TierBadge job={job} />
          {job.fit !== null && job.fit !== undefined && job.priority && (
            <span>{job.fit ? "Historical fit" : "Historical no-fit"}</span>
          )}
        </div>
        {job.reason_description && <p class="verdict-reasoning">{job.reason_description}</p>}
        {job.profile_stale && (
          <p class="verdict-stale">
            Assessed against an earlier profile. Reassessment is explicit.
          </p>
        )}
        {job.liveness === "likely_closed" && (
          <p class="verdict-stale">This posting has probably closed.</p>
        )}
      </>
    );
  }
  return (
    <p class="verdict-empty">
      {job.stage === "review_pending"
        ? "Legacy screening saved. Awaiting assessment."
        : job.stage === "screening_failed"
          ? "Screening failed; no assessment decision."
          : "Not assessed yet."}
    </p>
  );
}
