import { useEffect, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import type { GeneratedResumeRecord, JobContext, JobStatus } from "../../api/types";
import { highlightTerms } from "../../lib/highlightTerms";
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

  const [resume, setResume] = useState<GeneratedResumeRecord | null>(null);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);

  useEffect(() => {
    setResume(context?.resume || null);
    setGenerateError(null);
    setGenerating(false);
  }, [context?.job?.id, context?.resume]);

  const handleGenerateResume = async () => {
    if (!job) return;
    setGenerating(true);
    setGenerateError(null);
    try {
      const resp = await fetch(`/api/jobs/${job.id}/resume/generate`, {
        method: "POST",
      });
      if (resp.ok) {
        const data = await resp.json();
        setResume(data.record || null);
      } else {
        const err = await resp.json();
        setGenerateError(err.detail || "Resume generation failed");
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setGenerateError(msg || "Resume generation failed");
    } finally {
      setGenerating(false);
    }
  };

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
                    {job.fit !== null && job.fit !== undefined
                      ? job.fit
                        ? `Fit${job.reason_type ? ` · ${job.reason_type}` : ""}`
                        : `No fit${job.reason_type ? ` · ${job.reason_type}` : ""}`
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
                {/* Tailored Resume Section */}
                <div
                  class="drawer-section"
                  style={{
                    backgroundColor: "var(--bg-screen-alt)",
                    border: "1px solid var(--ink-primary)",
                    padding: "1rem",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: "0.5rem",
                      flexWrap: "wrap",
                    }}
                  >
                    <h4 style={{ margin: 0, borderBottom: "none", paddingBottom: 0, color: "var(--ink-primary)" }}>
                      <i class="fa-solid fa-file-waveform"></i> Tailored 1-Page Resume
                    </h4>
                    {!generating && (
                      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                        {resume ? (
                          <Link
                            href={`/resumes?tab=tailored&job=${job.id}`}
                            class="action-pill"
                            style={{ textDecoration: "none", fontSize: "0.85rem", padding: "0.3rem 0.8rem" }}
                            title="Open this tailored resume in the Resumes tab"
                          >
                            <i class="fa-solid fa-arrow-up-right-from-square"></i> View Resume
                          </Link>
                        ) : (
                          <Link
                            href="/resumes?tab=tailored"
                            class="action-pill"
                            style={{ textDecoration: "none", fontSize: "0.85rem", padding: "0.3rem 0.8rem" }}
                            title="View Resumes Library"
                          >
                            <i class="fa-solid fa-folder-open"></i> Resumes Library
                          </Link>
                        )}
                        <button
                          class="action-pill active"
                          onClick={handleGenerateResume}
                          style={{ fontSize: "0.85rem", padding: "0.3rem 0.8rem" }}
                        >
                          <i class="fa-solid fa-wand-magic-sparkles"></i>{" "}
                          {resume ? "Regenerate" : "Generate Resume"}
                        </button>
                      </div>
                    )}
                  </div>

                  {generating && (
                    <div style={{ marginTop: "0.75rem", color: "var(--ink-secondary)", fontSize: "0.9rem" }}>
                      <i class="fa-solid fa-spinner fa-spin"></i> Generating tailored 1-page resume...
                    </div>
                  )}

                  {generateError && (
                    <div style={{ marginTop: "0.5rem", color: "#ef4444", fontSize: "0.85rem" }}>
                      <i class="fa-solid fa-circle-exclamation"></i> {generateError}
                    </div>
                  )}

                  {resume && !generating && (
                    <div style={{ marginTop: "0.75rem" }}>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "0.5rem",
                          flexWrap: "wrap",
                        }}
                      >
                        {resume.ats_score !== null && resume.ats_score !== undefined && (
                          <span
                            class="match-badge-lg"
                            style={{
                              padding: "0.2rem 0.5rem",
                              fontSize: "0.8rem",
                              fontWeight: 600,
                            }}
                          >
                            <i class="fa-solid fa-shield-halved"></i> ATS: {resume.ats_score}/10 (
                            {resume.ats_verdict || "evaluated"})
                          </span>
                        )}
                        {resume.pdf_path && (
                          <a
                            href={`/api/resumes/${resume.id}/download?format=pdf`}
                            class="action-pill"
                            style={{ textDecoration: "none", fontSize: "0.8rem" }}
                            download
                          >
                            <i class="fa-solid fa-file-pdf"></i> PDF
                          </a>
                        )}
                        {(resume.typst_path || resume.resume) && (
                          <a
                            href={`/api/resumes/${resume.id}/download?format=typst`}
                            class="action-pill"
                            style={{ textDecoration: "none", fontSize: "0.8rem" }}
                            download
                            title="Download Typst markup source"
                          >
                            <i class="fa-solid fa-code"></i> Typst
                          </a>
                        )}
                      </div>

                      {resume.summary && (
                        <p
                          style={{
                            margin: "0.75rem 0 0 0",
                            fontSize: "0.85rem",
                            lineHeight: "1.5",
                            color: "var(--ink-primary, #050505)",
                            fontStyle: "italic",
                          }}
                        >
                          "{resume.summary}"
                        </p>
                      )}
                    </div>
                  )}
                </div>

                <div class="drawer-section">
                  <h4>Verdict</h4>
                  <div>
                    <VerdictPanel job={job} />
                  </div>
                </div>

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
