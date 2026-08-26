import { useEffect, useState } from "preact/hooks";
import { getJSONOrNull, guard } from "../../api/client";
import type {
  GeneratedResumeRecord,
  ProfileRecord,
  ProfileSkill,
  ResumeMasterProfile,
} from "../../api/types";

const LEVEL_LABELS = ["none", "aware", "working", "strong", "expert"];

function levelLabel(level: number): string {
  return LEVEL_LABELS[level] ?? String(level);
}

function SkillTags({ skills }: { skills: ProfileSkill[] }) {
  if (!skills.length) return <span class="skill-tag">No skills recorded</span>;
  const sorted = [...skills].sort((a, b) => b.level - a.level || a.key.localeCompare(b.key));
  return (
    <>
      {sorted.map((skill) => (
        <span
          key={skill.key}
          class="skill-tag"
          title={`level ${skill.level} — ${levelLabel(skill.level)}`}
        >
          {skill.label || skill.key.replace(/_/g, " ")} <em>{levelLabel(skill.level)}</em>
        </span>
      ))}
    </>
  );
}

export function ResumesPage() {
  const [activeSubTab, setActiveSubTab] = useState<"master" | "tailored" | "vector">("master");

  // Master profile state
  const [masterProfile, setMasterProfile] = useState<ResumeMasterProfile | null>(null);
  const [masterLoading, setMasterLoading] = useState(true);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Tailored resumes list
  const [resumesList, setResumesList] = useState<GeneratedResumeRecord[]>([]);
  const [resumesLoading, setResumesLoading] = useState(false);

  // Active scoring profile
  const [vectorRecord, setVectorRecord] = useState<ProfileRecord | null | undefined>(undefined);

  // Load master profile on mount
  useEffect(() => {
    guard("Loading master resume profile", () =>
      getJSONOrNull<ResumeMasterProfile>("/api/resume-builder/profile")
    ).then((data) => {
      setMasterProfile(
        data || {
          name: "",
          email: "",
          phone: "",
          location: "",
          github: "",
          linkedin: "",
          website: "",
          summary_guidance: "",
          education: [],
          skills: [],
          experience: [],
          raw_achievements_md: "",
        }
      );
      setMasterLoading(false);
    });
  }, []);

  // Load tailored resumes when subtab switches
  useEffect(() => {
    if (activeSubTab === "tailored") {
      setResumesLoading(true);
      guard("Loading generated resumes", () =>
        getJSONOrNull<GeneratedResumeRecord[]>("/api/resumes")
      ).then((data) => {
        setResumesList(data || []);
        setResumesLoading(false);
      });
    } else if (activeSubTab === "vector" && vectorRecord === undefined) {
      guard("Loading active profile vector", () =>
        getJSONOrNull<ProfileRecord>("/api/profile")
      ).then((data) => {
        setVectorRecord(data);
      });
    }
  }, [activeSubTab, vectorRecord]);

  const handleSaveMasterProfile = async () => {
    if (!masterProfile) return;
    setSaving(true);
    setSaveStatus(null);
    try {
      const resp = await fetch("/api/resume-builder/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(masterProfile),
      });
      if (resp.ok) {
        setSaveStatus("Saved successfully!");
        setTimeout(() => setSaveStatus(null), 4000);
      } else {
        const err = await resp.json();
        setSaveStatus(`Save error: ${err.detail || "Unknown"}`);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveStatus(`Save error: ${msg}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section class="tab-pane active" style={{ padding: "1.5rem" }}>
      {/* Sub-tab navigation */}
      <div
        class="subtab-bar"
        style={{
          display: "flex",
          gap: "1rem",
          borderBottom: "1px solid var(--border-color, #333)",
          marginBottom: "1.5rem",
          paddingBottom: "0.5rem",
        }}
      >
        <button
          class={`action-pill ${activeSubTab === "master" ? "active" : ""}`}
          onClick={() => setActiveSubTab("master")}
          style={{ fontSize: "0.95rem", fontWeight: 600 }}
        >
          <i class="fa-solid fa-user-pen"></i> Master Resume Profile
        </button>
        <button
          class={`action-pill ${activeSubTab === "tailored" ? "active" : ""}`}
          onClick={() => setActiveSubTab("tailored")}
          style={{ fontSize: "0.95rem", fontWeight: 600 }}
        >
          <i class="fa-solid fa-file-lines"></i> Tailored Resumes Library
        </button>
        <button
          class={`action-pill ${activeSubTab === "vector" ? "active" : ""}`}
          onClick={() => setActiveSubTab("vector")}
          style={{ fontSize: "0.95rem", fontWeight: 600 }}
        >
          <i class="fa-solid fa-brain"></i> Active Scoring Profile
        </button>
      </div>

      {/* Sub-tab 1: Master Profile Editor */}
      {activeSubTab === "master" && (
        <div>
          {masterLoading || !masterProfile ? (
            <p>Loading master profile...</p>
          ) : (
            <div style={{ maxWidth: "1000px" }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "1rem",
                }}
              >
                <div>
                  <h3 style={{ margin: 0 }}>Master Resume Profile & Achievements Knowledge Base</h3>
                  <p style={{ color: "var(--text-muted, #888)", margin: "0.25rem 0 0 0" }}>
                    This data is saved directly in your CareerRadar database and dynamically loaded
                    by the DeepSeek resume builder.
                  </p>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
                  {saveStatus && (
                    <span
                      style={{
                        color: saveStatus.includes("error") ? "#ef4444" : "#10b981",
                        fontWeight: 600,
                      }}
                    >
                      {saveStatus}
                    </span>
                  )}
                  <button
                    class="action-pill text-green active"
                    onClick={handleSaveMasterProfile}
                    disabled={saving}
                    style={{ padding: "0.5rem 1.25rem", fontSize: "0.95rem" }}
                  >
                    <i class={`fa-solid ${saving ? "fa-spinner fa-spin" : "fa-floppy-disk"}`}></i>{" "}
                    {saving ? "Saving..." : "Save Changes"}
                  </button>
                </div>
              </div>

              {/* Personal / Contact Information */}
              <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                <h4>Personal & Contact Information</h4>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                    gap: "0.75rem",
                    marginTop: "0.5rem",
                  }}
                >
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>Full Name</label>
                    <input
                      type="text"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.name}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          name: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>Email</label>
                    <input
                      type="email"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.email}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          email: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>Phone</label>
                    <input
                      type="text"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.phone}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          phone: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>Location</label>
                    <input
                      type="text"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.location}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          location: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>GitHub URL</label>
                    <input
                      type="text"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.github}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          github: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>LinkedIn URL</label>
                    <input
                      type="text"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.linkedin}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          linkedin: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: "0.8rem", color: "#888" }}>Website / Portfolio</label>
                    <input
                      type="text"
                      class="filter-input"
                      style={{ width: "100%" }}
                      value={masterProfile.website}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          website: (e.target as HTMLInputElement).value,
                        })
                      }
                    />
                  </div>
                </div>

                <div style={{ marginTop: "1rem" }}>
                  <label style={{ fontSize: "0.8rem", color: "#888" }}>
                    Summary Guidance & Core Positioning
                  </label>
                  <textarea
                    class="filter-input"
                    rows={3}
                    style={{ width: "100%", marginTop: "0.25rem" }}
                    value={masterProfile.summary_guidance}
                    onInput={(e) =>
                      setMasterProfile({
                        ...masterProfile,
                        summary_guidance: (e.target as HTMLTextAreaElement).value,
                      })
                    }
                  />
                </div>
              </div>

              {/* Education */}
              <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                <div
                  style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
                >
                  <h4>Education History</h4>
                  <button
                    class="action-pill"
                    onClick={() => {
                      setMasterProfile({
                        ...masterProfile,
                        education: [
                          ...masterProfile.education,
                          { institution: "", degree: "", details: "" },
                        ],
                      });
                    }}
                  >
                    <i class="fa-solid fa-plus"></i> Add Degree
                  </button>
                </div>
                {masterProfile.education.map((edu, idx) => (
                  <div
                    key={idx}
                    style={{
                      display: "flex",
                      gap: "0.75rem",
                      alignItems: "center",
                      marginTop: "0.75rem",
                    }}
                  >
                    <input
                      type="text"
                      placeholder="Institution (e.g. UCLA)"
                      class="filter-input"
                      style={{ flex: 1 }}
                      value={edu.institution}
                      onInput={(e) => {
                        const next = [...masterProfile.education];
                        next[idx].institution = (e.target as HTMLInputElement).value;
                        setMasterProfile({ ...masterProfile, education: next });
                      }}
                    />
                    <input
                      type="text"
                      placeholder="Degree (e.g. PhD in Philosophy (2019); MA)"
                      class="filter-input"
                      style={{ flex: 2 }}
                      value={edu.degree}
                      onInput={(e) => {
                        const next = [...masterProfile.education];
                        next[idx].degree = (e.target as HTMLInputElement).value;
                        setMasterProfile({ ...masterProfile, education: next });
                      }}
                    />
                    <button
                      class="action-pill text-red"
                      onClick={() => {
                        const next = masterProfile.education.filter((_, i) => i !== idx);
                        setMasterProfile({ ...masterProfile, education: next });
                      }}
                    >
                      <i class="fa-solid fa-trash"></i>
                    </button>
                  </div>
                ))}
              </div>

              {/* Skills Master Categories */}
              <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                <div
                  style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
                >
                  <h4>Skills Categories & Tools</h4>
                  <button
                    class="action-pill"
                    onClick={() => {
                      setMasterProfile({
                        ...masterProfile,
                        skills: [...masterProfile.skills, { category: "New Category", skills: [] }],
                      });
                    }}
                  >
                    <i class="fa-solid fa-plus"></i> Add Category
                  </button>
                </div>
                {masterProfile.skills.map((cat, idx) => (
                  <div
                    key={idx}
                    style={{
                      display: "flex",
                      gap: "0.75rem",
                      alignItems: "center",
                      marginTop: "0.75rem",
                    }}
                  >
                    <input
                      type="text"
                      placeholder="Category Name (e.g. Infrastructure)"
                      class="filter-input"
                      style={{ flex: 1 }}
                      value={cat.category}
                      onInput={(e) => {
                        const next = [...masterProfile.skills];
                        next[idx].category = (e.target as HTMLInputElement).value;
                        setMasterProfile({ ...masterProfile, skills: next });
                      }}
                    />
                    <input
                      type="text"
                      placeholder="Skills comma-separated (e.g. AWS, Terraform, Docker)"
                      class="filter-input"
                      style={{ flex: 3 }}
                      value={cat.skills.join(", ")}
                      onInput={(e) => {
                        const raw = (e.target as HTMLInputElement).value;
                        const parsed = raw
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean);
                        const next = [...masterProfile.skills];
                        next[idx].skills = parsed;
                        setMasterProfile({ ...masterProfile, skills: next });
                      }}
                    />
                    <button
                      class="action-pill text-red"
                      onClick={() => {
                        const next = masterProfile.skills.filter((_, i) => i !== idx);
                        setMasterProfile({ ...masterProfile, skills: next });
                      }}
                    >
                      <i class="fa-solid fa-trash"></i>
                    </button>
                  </div>
                ))}
              </div>

              {/* Master Experience & Projects Pool */}
              <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                <div
                  style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
                >
                  <h4>Master Experience & Projects Pool</h4>
                  <button
                    class="action-pill"
                    onClick={() => {
                      setMasterProfile({
                        ...masterProfile,
                        experience: [
                          ...masterProfile.experience,
                          {
                            title: "New Role",
                            company: "Company",
                            dates: "Jan 2025 - Present",
                            projects: [{ name: "Project", heading: "", bullets: [] }],
                          },
                        ],
                      });
                    }}
                  >
                    <i class="fa-solid fa-plus"></i> Add Role
                  </button>
                </div>
                {masterProfile.experience.map((role, rIdx) => (
                  <div
                    key={rIdx}
                    style={{
                      border: "1px solid var(--border-color, #333)",
                      borderRadius: "6px",
                      padding: "1rem",
                      marginTop: "1rem",
                      backgroundColor: "rgba(255, 255, 255, 0.02)",
                    }}
                  >
                    <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
                      <input
                        type="text"
                        placeholder="Title"
                        class="filter-input"
                        style={{ flex: 2 }}
                        value={role.title}
                        onInput={(e) => {
                          const next = [...masterProfile.experience];
                          next[rIdx].title = (e.target as HTMLInputElement).value;
                          setMasterProfile({ ...masterProfile, experience: next });
                        }}
                      />
                      <input
                        type="text"
                        placeholder="Company"
                        class="filter-input"
                        style={{ flex: 2 }}
                        value={role.company}
                        onInput={(e) => {
                          const next = [...masterProfile.experience];
                          next[rIdx].company = (e.target as HTMLInputElement).value;
                          setMasterProfile({ ...masterProfile, experience: next });
                        }}
                      />
                      <input
                        type="text"
                        placeholder="Dates (e.g. 2024 - Present)"
                        class="filter-input"
                        style={{ flex: 1.5 }}
                        value={role.dates}
                        onInput={(e) => {
                          const next = [...masterProfile.experience];
                          next[rIdx].dates = (e.target as HTMLInputElement).value;
                          setMasterProfile({ ...masterProfile, experience: next });
                        }}
                      />
                      <button
                        class="action-pill text-red"
                        onClick={() => {
                          const next = masterProfile.experience.filter((_, i) => i !== rIdx);
                          setMasterProfile({ ...masterProfile, experience: next });
                        }}
                      >
                        <i class="fa-solid fa-trash"></i>
                      </button>
                    </div>

                    {/* Projects & Bullets inside Role */}
                    <div style={{ marginTop: "0.75rem", paddingLeft: "1rem" }}>
                      {role.projects.map((proj, pIdx) => (
                        <div key={pIdx} style={{ marginTop: "0.5rem" }}>
                          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                            <input
                              type="text"
                              placeholder="Project Heading / Italic Scope line"
                              class="filter-input"
                              style={{ flex: 1, fontSize: "0.9rem" }}
                              value={proj.heading}
                              onInput={(e) => {
                                const next = [...masterProfile.experience];
                                next[rIdx].projects[pIdx].heading = (
                                  e.target as HTMLInputElement
                                ).value;
                                setMasterProfile({ ...masterProfile, experience: next });
                              }}
                            />
                            <button
                              class="action-pill text-red"
                              style={{ fontSize: "0.8rem" }}
                              onClick={() => {
                                const next = [...masterProfile.experience];
                                next[rIdx].projects = next[rIdx].projects.filter(
                                  (_, i) => i !== pIdx
                                );
                                setMasterProfile({ ...masterProfile, experience: next });
                              }}
                            >
                              <i class="fa-solid fa-xmark"></i>
                            </button>
                          </div>
                          {/* Bullets */}
                          <div style={{ marginTop: "0.25rem", paddingLeft: "1rem" }}>
                            {proj.bullets.map((b, bIdx) => (
                              <div
                                key={bIdx}
                                style={{
                                  display: "flex",
                                  gap: "0.5rem",
                                  alignItems: "center",
                                  marginTop: "0.25rem",
                                }}
                              >
                                <span style={{ color: "#888" }}>•</span>
                                <input
                                  type="text"
                                  placeholder="Achievement bullet (quantified impact + tech)"
                                  class="filter-input"
                                  style={{ flex: 1, fontSize: "0.85rem" }}
                                  value={b}
                                  onInput={(e) => {
                                    const next = [...masterProfile.experience];
                                    next[rIdx].projects[pIdx].bullets[bIdx] = (
                                      e.target as HTMLInputElement
                                    ).value;
                                    setMasterProfile({ ...masterProfile, experience: next });
                                  }}
                                />
                                <button
                                  class="action-pill text-red"
                                  style={{ padding: "0.15rem 0.4rem" }}
                                  onClick={() => {
                                    const next = [...masterProfile.experience];
                                    next[rIdx].projects[pIdx].bullets = next[rIdx].projects[
                                      pIdx
                                    ].bullets.filter((_, i) => i !== bIdx);
                                    setMasterProfile({ ...masterProfile, experience: next });
                                  }}
                                >
                                  <i class="fa-solid fa-minus"></i>
                                </button>
                              </div>
                            ))}
                            <button
                              class="action-pill"
                              style={{
                                fontSize: "0.75rem",
                                marginTop: "0.25rem",
                                padding: "0.2rem 0.5rem",
                              }}
                              onClick={() => {
                                const next = [...masterProfile.experience];
                                next[rIdx].projects[pIdx].bullets.push("New achievement bullet");
                                setMasterProfile({ ...masterProfile, experience: next });
                              }}
                            >
                              <i class="fa-solid fa-plus"></i> Add Bullet
                            </button>
                          </div>
                        </div>
                      ))}
                      <button
                        class="action-pill"
                        style={{
                          fontSize: "0.8rem",
                          marginTop: "0.5rem",
                          padding: "0.25rem 0.6rem",
                        }}
                        onClick={() => {
                          const next = [...masterProfile.experience];
                          next[rIdx].projects.push({
                            name: "New Project",
                            heading: "Project scope:",
                            bullets: ["Engineered..."],
                          });
                          setMasterProfile({ ...masterProfile, experience: next });
                        }}
                      >
                        <i class="fa-solid fa-plus"></i> Add Project Scope
                      </button>
                    </div>
                  </div>
                ))}
              </div>

              {/* Raw Achievements Knowledge Base Markdown */}
              <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                <h4>Detailed Achievements Knowledge Base (Markdown)</h4>
                <p style={{ color: "#888", fontSize: "0.85rem", margin: "0.25rem 0 0.5rem 0" }}>
                  Additional factual achievements pool provided to the DeepSeek generator agent to
                  draw from.
                </p>
                <textarea
                  class="filter-input"
                  rows={10}
                  style={{ width: "100%", fontFamily: "monospace", fontSize: "0.85rem" }}
                  value={masterProfile.raw_achievements_md}
                  onInput={(e) =>
                    setMasterProfile({
                      ...masterProfile,
                      raw_achievements_md: (e.target as HTMLTextAreaElement).value,
                    })
                  }
                />
              </div>

              {/* Bottom Save Bar */}
              <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "2rem" }}>
                <button
                  class="action-pill text-green active"
                  onClick={handleSaveMasterProfile}
                  disabled={saving}
                  style={{ padding: "0.6rem 1.5rem", fontSize: "1rem" }}
                >
                  <i class={`fa-solid ${saving ? "fa-spinner fa-spin" : "fa-floppy-disk"}`}></i>{" "}
                  {saving ? "Saving..." : "Save Master Profile"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Sub-tab 2: Tailored Resumes Library */}
      {activeSubTab === "tailored" && (
        <div style={{ maxWidth: "1000px" }}>
          <div style={{ marginBottom: "1rem" }}>
            <h3 style={{ margin: 0 }}>Tailored Resumes Library</h3>
            <p style={{ color: "#888", margin: "0.25rem 0 0 0" }}>
              1-page tailored resumes generated by LangGraph Actor-Critic pipeline for specific job
              postings.
            </p>
          </div>

          {resumesLoading ? (
            <p>Loading generated resumes...</p>
          ) : resumesList.length === 0 ? (
            <div class="resume-card">
              <p style={{ color: "#888" }}>
                No tailored resumes generated yet. Open any job in the Dashboard and click{" "}
                <strong>"Generate Resume"</strong>!
              </p>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              {resumesList.map((res) => (
                <div key={res.id} class="resume-card">
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "flex-start",
                    }}
                  >
                    <div>
                      <h3 style={{ margin: 0 }}>
                        {res.job_title || "Software Engineer"} @ {res.job_company || "Company"}
                      </h3>
                      <span style={{ fontSize: "0.85rem", color: "#888" }}>
                        Job #{res.job_id} · Generated {(res.created_at || "").slice(0, 16)} · Model:{" "}
                        {res.model || "deepseek-chat"}
                      </span>
                    </div>

                    <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                      {res.ats_score !== null && res.ats_score !== undefined && (
                        <span
                          class={`match-badge-lg ${res.ats_score >= 9 ? "text-green" : "text-yellow"}`}
                          style={{
                            padding: "0.25rem 0.6rem",
                            borderRadius: "4px",
                            backgroundColor:
                              res.ats_score >= 9
                                ? "rgba(16, 185, 129, 0.15)"
                                : "rgba(245, 158, 11, 0.15)",
                            border: `1px solid ${res.ats_score >= 9 ? "#10b981" : "#f59e0b"}`,
                            fontSize: "0.85rem",
                            fontWeight: 600,
                          }}
                        >
                          <i class="fa-solid fa-shield-halved"></i> ATS: {res.ats_score}/10 (
                          {res.ats_verdict || "evaluated"})
                        </span>
                      )}

                      <a
                        href={`/api/resumes/${res.id}/download?format=docx`}
                        class="action-pill text-blue"
                        style={{ textDecoration: "none" }}
                        download
                      >
                        <i class="fa-solid fa-file-word"></i> DOCX
                      </a>

                      {res.pdf_path && (
                        <a
                          href={`/api/resumes/${res.id}/download?format=pdf`}
                          class="action-pill text-red"
                          style={{ textDecoration: "none" }}
                          download
                        >
                          <i class="fa-solid fa-file-pdf"></i> PDF
                        </a>
                      )}
                    </div>
                  </div>

                  {res.summary && (
                    <p style={{ marginTop: "0.75rem", fontSize: "0.9rem", color: "#ccc" }}>
                      <strong>Summary:</strong> {res.summary}
                    </p>
                  )}

                  {res.ats_feedback && (
                    <div
                      style={{
                        marginTop: "0.5rem",
                        padding: "0.5rem 0.75rem",
                        borderRadius: "4px",
                        backgroundColor: "rgba(255, 255, 255, 0.03)",
                        fontSize: "0.85rem",
                        color: "#9ca3af",
                      }}
                    >
                      <i class="fa-solid fa-comments"></i> <strong>ATS Screener Feedback:</strong>{" "}
                      {res.ats_feedback}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Sub-tab 3: Active Scoring Profile */}
      {activeSubTab === "vector" && (
        <div class="resumes-grid" style={{ maxWidth: "1000px" }}>
          {vectorRecord === undefined ? (
            <p>Loading active scoring profile...</p>
          ) : vectorRecord === null ? (
            <div class="resume-card">
              <div class="resume-card-header">
                <h3>No active profile</h3>
              </div>
              <p class="card-note">
                Nothing downstream is meaningful without one. Build it with{" "}
                <code>careerradar profile build</code>.
              </p>
            </div>
          ) : (
            <div class="resume-card">
              <div class="resume-card-header">
                <span>
                  Profile version {vectorRecord.version} · built{" "}
                  {(vectorRecord.created_at || "").slice(0, 10)} ·{" "}
                  {vectorRecord.model || "unknown model"}
                </span>
                <h3>{vectorRecord.profile.seniority || "Active profile"}</h3>
              </div>
              <div class="divider"></div>
              <p class="verdict-summary">{vectorRecord.profile.bio || ""}</p>
              <h4>Skills Vector ({vectorRecord.profile.skills.length})</h4>
              <div class="skills-scroll-area">
                <SkillTags skills={vectorRecord.profile.skills} />
              </div>
              <h4>Built from</h4>
              <ul class="detail-list">
                {vectorRecord.documents.length === 0 ? (
                  <li>No source documents recorded.</li>
                ) : (
                  vectorRecord.documents.map((doc, i) => {
                    const name = String(doc.path || "").split("/").pop();
                    return (
                      <li key={i}>
                        <strong>{name}</strong>{" "}
                        <span class="detail-meta">
                          {doc.kind || ""} · {doc.chars ?? 0} chars
                        </span>
                      </li>
                    );
                  })
                )}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
