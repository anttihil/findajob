import { useEffect, useState } from "preact/hooks";
import { getJSON, reportError } from "../../api/client";
import type { SkillDetailResponse } from "../../api/types";

// Ported from `partials/skill_drawer.html` + the `openSkillDetail`/drawer-toggle logic in
// `frontend/js/features/skills.js`. Kept as its own shell (not a reuse of the job drawer):
// the two only ever shared a shell because both were server-rendered by the same Jinja
// include, and that stopped being true once the job drawer became page state.
export function SkillDrawer({ skill, onClose }: { skill: string | null; onClose: () => void }) {
  const [detail, setDetail] = useState<SkillDetailResponse | null>(null);

  useEffect(() => {
    if (!skill) return;
    setDetail(null);
    let cancelled = false;
    getJSON<SkillDetailResponse>(`/api/skills/${encodeURIComponent(skill)}`)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        reportError(`Loading detail for "${skill}"`, err);
        onClose();
      });
    return () => {
      cancelled = true;
    };
  }, [skill, onClose]);

  useEffect(() => {
    if (!skill) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [skill, onClose]);

  const active = skill !== null;

  return (
    <>
      <div class={`drawer-overlay ${active ? "active" : ""}`} onClick={onClose}></div>
      <div class={`job-drawer ${active ? "active" : ""}`}>
        {active && (
          <>
            <button class="close-drawer-btn" onClick={onClose}>
              <i class="fa-solid fa-xmark"></i>
            </button>

            <div class="drawer-header">
              <div class="drawer-badge-row">
                <span class="match-badge-lg">
                  {detail ? (detail.user_has ? `Level ${detail.user_level}` : "Not on resume") : ""}
                </span>
                <span class="source-badge">{detail?.category ?? ""}</span>
              </div>
              <h2>{detail?.label ?? skill}</h2>
              <div class="drawer-meta">
                <span>
                  <i class="fa-solid fa-briefcase"></i> {detail ? `${detail.postings.length} postings` : ""}
                </span>
                <span>
                  <i class="fa-solid fa-clock"></i> {detail ? `last ${detail.window_days} days` : ""}
                </span>
              </div>
            </div>

            <div class="drawer-body">
              <div class="drawer-section">
                <h4>Your evidence</h4>
                {detail && (
                  <div>
                    {detail.evidence.length ? (
                      <p class="verdict-reasoning">{detail.evidence.join(" · ")}</p>
                    ) : (
                      <p class="verdict-empty">No evidence for this skill in your profile.</p>
                    )}
                  </div>
                )}
              </div>
              <div class="drawer-section">
                <h4>Co-occurring skills</h4>
                <div class="skills-tags-container">
                  {detail && detail.cooccurring.length === 0 && (
                    <span class="skill-tag">No co-occurring skills</span>
                  )}
                  {detail?.cooccurring.map((c) => (
                    <span key={c.label} class={`skill-tag ${c.user_has ? "" : "is-missing"}`}>
                      {c.label} <em>{c.n}</em>
                    </span>
                  ))}
                </div>
              </div>
              <div class="drawer-section">
                <h4>Where it appears</h4>
                {detail && (
                  <>
                    <ul class="detail-list">
                      {detail.by_role_family.map((f) => (
                        <li key={f.label}>
                          {f.label} <strong>{f.n}</strong>
                        </li>
                      ))}
                    </ul>
                    <h4>Postings requiring it</h4>
                    <ul class="detail-list">
                      {detail.postings.slice(0, 25).map((j, i) => (
                        <li key={i}>
                          <a href={j.url} target="_blank" rel="noopener">
                            {j.title}
                          </a>
                          <span class="detail-meta">
                            {j.company} · score {j.match_score}
                            {j.in_title ? " · in title" : ""}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </>
  );
}
