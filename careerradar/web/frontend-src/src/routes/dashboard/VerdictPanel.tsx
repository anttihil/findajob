import type { Job } from "../../api/types";
import { BulletList } from "../../components/BulletList";
import { VerdictBadge } from "../../components/badges/VerdictBadge";

// Ported from `partials/verdict.html`.
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

  if (job.fit_score == null && !job.verdict) {
    return (
      <p class="verdict-empty">
        Not scored yet. This posting is queued (<code>{job.pipeline_state || "new"}</code>); the
        scoring stage will pick it up on its next run.
      </p>
    );
  }

  const ordinals: [string, string][] = (
    [
      ["eligibility", job.eligibility],
      ["role", job.role_match],
      ["capability", job.capability_match],
      ["seniority", job.seniority_gap],
      ["evidence", job.evidence_quality],
    ] as [string, string | null][]
  ).filter((pair): pair is [string, string] => !!pair[1]);

  return (
    <>
      <div class="verdict-head">
        {job.verdict && <VerdictBadge verdict={job.verdict} />}
        {job.pareto_tier != null ? (
          <span class="verdict-score" title="1 dominates everything below it">
            tier {job.pareto_tier}
          </span>
        ) : (
          <span class="verdict-score">{job.fit_score}/100</span>
        )}
      </div>

      {job.role_summary && <p class="verdict-summary">{job.role_summary}</p>}

      {ordinals.length > 0 && (
        <div class="verdict-ordinals">
          {ordinals.map(([key, value]) => (
            <span key={key} class="ordinal">
              <b>{key}</b> {value.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      )}

      {job.liveness === "likely_closed" && (
        <p class="verdict-stale">
          This posting did not reappear the last time its search was run, so it has probably
          closed.
        </p>
      )}

      {job.reasoning && <p class="verdict-reasoning">{job.reasoning}</p>}

      {job.hard_blockers.length > 0 && (
        <>
          <h5 class="verdict-h5 text-red">Hard blockers ({job.hard_blockers.length})</h5>
          <ul class="verdict-list verdict-blockers">
            {job.hard_blockers.map((blocker, i) => {
              const quote = typeof blocker === "string" ? blocker : blocker.quote;
              const why = typeof blocker === "string" ? "" : blocker.why;
              return (
                <li key={i}>
                  <q>{quote}</q>
                  {why && <span class="blocker-why">{why}</span>}
                </li>
              );
            })}
          </ul>
        </>
      )}

      {job.key_gaps.length > 0 && (
        <>
          <h5 class="verdict-h5">Gaps</h5>
          <BulletList items={job.key_gaps} />
        </>
      )}

      {job.strengths.length > 0 && (
        <>
          <h5 class="verdict-h5 text-green">You bring</h5>
          <BulletList items={job.strengths} />
        </>
      )}
    </>
  );
}
