import type { Job, RequirementRow } from "../../api/types";

// Ported from `partials/requirements.html`.
export function RequirementsPanel({ job, rows }: { job: Job; rows: RequirementRow[] }) {
  return (
    <>
      {job.requirement_summary && (
        <p class="req-summary">
          {job.requirement_summary.must_met} of {job.requirement_summary.must_total} must-haves met
        </p>
      )}
      <ul class="req-list">
        {rows.map((row, i) => (
          <li key={i} class={`req-row req-${row.status}`} title={row.quote}>
            <div class="req-head">
              <span class="req-status">{row.status}</span>
              <span class="req-importance">{row.importance.replace(/_/g, " ")}</span>
            </div>
            <div class="req-text">{row.requirement}</div>
            {row.evidence && <div class="req-evidence">{row.evidence}</div>}
          </li>
        ))}
      </ul>
    </>
  );
}
