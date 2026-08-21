import type { Job } from "../../api/types";

export function VerdictPanel({ job }: { job: Job }) {
  if (job.fit !== null && job.fit !== undefined) {
    return (
      <>
        <div class="verdict-head">
          <span class={`match-badge ${job.fit ? "badge-top" : "badge-blocked"}`}>
            {job.fit ? "Strong Fit (≥90%)" : "No Fit (<90%)"}
          </span>
          {job.reason_type && (
            <span class="verdict-score">
              {job.reason_type}
            </span>
          )}
        </div>

        {job.reason_description && <p class="verdict-reasoning">{job.reason_description}</p>}

        {job.liveness === "likely_closed" && (
          <p class="verdict-stale">
            This posting did not reappear the last time its search was run, so it has probably
            closed.
          </p>
        )}
      </>
    );
  }

  return (
    <p class="verdict-empty">
      Not scored yet. This posting is queued (<code>{job.pipeline_state || "new"}</code>); the
      scoring stage will pick it up on its next run.
    </p>
  );
}
