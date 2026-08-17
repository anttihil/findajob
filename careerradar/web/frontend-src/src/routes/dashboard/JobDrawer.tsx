import type { JobContext, JobStatus } from "../../api/types";
import { highlightTerms } from "../../lib/highlightTerms";
import { DossierPanel } from "./DossierPanel";
import { RequirementsPanel } from "./RequirementsPanel";
import { VerdictPanel } from "./VerdictPanel";

// Ported from `partials/job_drawer.html` + the permanent drawer chrome in `base.html`. The
// chrome (overlay + panel) stays mounted even when closed, same reason as the Jinja
// version: a freshly-mounted element can't animate a slide-in, it just appears.
export function JobDrawer({
  context,
  onClose,
  onStatusChange,
}: {
  context: JobContext | null;
  onClose: () => void;
  onStatusChange: (jobId: number, status: JobStatus) => void;
}) {
  const job = context?.job ?? null;
  const requirementRows = context?.requirement_rows ?? [];
  const dossier = context?.dossier ?? null;

  return (
    <div id="drawer-root">
      <div class={`drawer-overlay ${job ? "active" : ""}`} onClick={onClose}></div>
      <div class="job-drawer">
        <div id="drawer-content">
          {job && (
            <>
              <button class="close-drawer-btn" aria-label="Close" onClick={onClose}>
                <i class="fa-solid fa-xmark"></i>
              </button>

              <div class="drawer-header">
                <div class="drawer-badge-row">
                  <span class="match-badge-lg">
                    {job.pareto_tier != null
                      ? `tier ${job.pareto_tier}`
                      : job.fit_score != null
                        ? `${job.fit_score} fit`
                        : "Not yet scored"}
                  </span>
                  <span class="source-badge">{job.source}</span>
                </div>
                <h2>{job.title}</h2>
                <div class="drawer-meta">
                  <span>
                    <i class="fa-solid fa-building"></i> {job.company}
                  </span>
                  <span>
                    <i class="fa-solid fa-location-dot"></i> {job.location || job.country}
                  </span>
                </div>
              </div>

              <div class="drawer-actions-row">
                <button
                  class={`action-pill text-green ${job.status === "saved" ? "active" : ""}`}
                  onClick={() => onStatusChange(job.id, "saved")}
                >
                  <i class="fa-solid fa-bookmark"></i> Save Job
                </button>
                <button
                  class={`action-pill text-blue ${job.status === "applied" ? "active" : ""}`}
                  onClick={() => onStatusChange(job.id, "applied")}
                >
                  <i class="fa-solid fa-paper-plane"></i> Mark Applied
                </button>
                <button
                  class={`action-pill text-red ${job.status === "rejected" ? "active" : ""}`}
                  onClick={() => onStatusChange(job.id, "rejected")}
                >
                  <i class="fa-solid fa-trash-can"></i> Reject
                </button>
                <a href={job.url || "#"} target="_blank" rel="noopener" class="action-pill-apply">
                  Apply on Site <i class="fa-solid fa-up-right-from-square"></i>
                </a>
              </div>

              <div class="drawer-body">
                <div class="drawer-section">
                  <h4>Verdict</h4>
                  <div>
                    <VerdictPanel job={job} />
                  </div>
                </div>

                {requirementRows.length > 0 && (
                  <div class="drawer-section">
                    <h4>Requirements</h4>
                    <div>
                      <RequirementsPanel job={job} rows={requirementRows} />
                    </div>
                  </div>
                )}

                {dossier && (
                  <div class="drawer-section">
                    <h4>Company Dossier</h4>
                    <div>
                      <DossierPanel dossier={dossier} />
                    </div>
                  </div>
                )}

                <div class="drawer-section">
                  <h4>Keyword Overlap</h4>
                  <div class="skills-tags-container">
                    {job.matched_skills.length === 0 ? (
                      <span class="skill-tag">No direct keyword overlap</span>
                    ) : (
                      job.matched_skills.map((skill) => (
                        <span key={skill} class="skill-tag text-purple">
                          {skill}
                        </span>
                      ))
                    )}
                  </div>
                </div>

                <div class="drawer-section">
                  <h4>Job Description</h4>
                  <div class="job-desc-content">
                    {highlightTerms(job.description, job.matched_skills).map((seg, i) =>
                      seg.highlighted ? (
                        <span key={i} class="highlight-term">
                          {seg.text}
                        </span>
                      ) : (
                        seg.text
                      )
                    )}
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
