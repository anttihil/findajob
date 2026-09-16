import { useEffect, useMemo, useState } from "preact/hooks";
import { Link, useLocation, useSearch } from "wouter-preact";
import { getJSONOrNull, guard } from "../../api/client";
import type {
  GeneratedResumeRecord,
  MasterEducation,
  MasterProject,
  MasterRole,
  Profile,
  ResumeUploadResponse,
} from "../../api/types";
import { ResumeDropzone } from "./ResumeDropzone";
import { ProfileChatPanel } from "./ProfileChatPanel";

interface SkillCategoryForm {
  category: string;
  skillsText: string;
}

interface ProfileFormState {
  name: string;
  email: string;
  phone: string;
  location: string;
  github: string;
  linkedin: string;
  website: string;
  eligibility: {
    citizenshipText: string;
    locationsText: string;
    willing_to_relocate: boolean;
    comp_floor_usd: number | null;
  };
  seniority: string;
  years_experience: number;
  executive_summary: string;
  model_guidance: string;
  dealbreakersText: string;
  experience: MasterRole[];
  projects: MasterProject[];
  skills: SkillCategoryForm[];
  education: MasterEducation[];
}

function toFormState(p?: Partial<Profile> | null): ProfileFormState {
  return {
    name: p?.name || "",
    email: p?.email || "",
    phone: p?.phone || "",
    location: p?.location || "",
    github: p?.github || "",
    linkedin: p?.linkedin || "",
    website: p?.website || "",
    eligibility: {
      citizenshipText: (p?.eligibility?.citizenship || ["Authorized to work in US"]).join(", "),
      locationsText: (p?.eligibility?.locations || ["Remote"]).join(", "),
      willing_to_relocate: p?.eligibility?.willing_to_relocate ?? false,
      comp_floor_usd: p?.eligibility?.comp_floor_usd ?? 90000,
    },
    seniority: p?.seniority || "Mid / Senior",
    years_experience: p?.years_experience ?? 4.0,
    executive_summary: p?.executive_summary || p?.summary_guidance || "",
    model_guidance: p?.model_guidance || "",
    dealbreakersText: (
      p?.dealbreakers ||
      p?.targeting?.dealbreakers ||
      ["24-hour on-call site reliability rotations"]
    ).join("\n"),
    experience: p?.experience || [],
    projects: p?.projects || [],
    skills: (p?.skills || []).map((s) => ({
      category: s.category,
      skillsText: s.skills.join(", "),
    })),
    education: p?.education || [],
  };
}

function toProfilePayload(f: ProfileFormState): Profile {
  return {
    name: f.name,
    email: f.email,
    phone: f.phone,
    location: f.location,
    github: f.github,
    linkedin: f.linkedin,
    website: f.website,
    eligibility: {
      citizenship: f.eligibility.citizenshipText
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      locations: f.eligibility.locationsText
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      willing_to_relocate: f.eligibility.willing_to_relocate,
      comp_floor_usd: f.eligibility.comp_floor_usd,
    },
    seniority: f.seniority,
    years_experience: f.years_experience,
    executive_summary: f.executive_summary,
    model_guidance: f.model_guidance,
    dealbreakers: f.dealbreakersText
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean),
    experience: f.experience,
    projects: f.projects,
    skills: f.skills.map((s) => ({
      category: s.category,
      skills: s.skillsText
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
    })),
    education: f.education,
  };
}

export function ResumesPage() {
  const search = useSearch();
  const [, navigate] = useLocation();
  const searchParams = useMemo(() => new URLSearchParams(search), [search]);
  const targetJobId = searchParams.get("job") || searchParams.get("job_id");
  const targetResumeId = searchParams.get("resume") || searchParams.get("resume_id");
  const targetTab = searchParams.get("tab");

  // Single source of truth: activeSubTab is derived directly from router query params
  const activeSubTab: "master" | "tailored" =
    targetTab === "tailored" || Boolean(targetJobId) || Boolean(targetResumeId)
      ? "tailored"
      : "master";

  // Master profile form state
  const [masterProfile, setMasterProfile] = useState<ProfileFormState | null>(null);
  const [masterLoading, setMasterLoading] = useState(true);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Copilot Chat State
  const [showCopilot, setShowCopilot] = useState(false);
  const [uploadedResumeText, setUploadedResumeText] = useState<string | null>(null);

  // Tailored resumes list
  const [resumesList, setResumesList] = useState<GeneratedResumeRecord[]>([]);
  const [resumesLoading, setResumesLoading] = useState(false);

  // Load master profile on mount
  useEffect(() => {
    guard("Loading master resume profile", () =>
      getJSONOrNull<Profile>("/api/resume-builder/profile")
    ).then((data) => {
      setMasterProfile(toFormState(data));
      setMasterLoading(false);
    });
  }, []);

  // Load tailored resumes when switching subtabs
  useEffect(() => {
    if (activeSubTab === "tailored") {
      setResumesLoading(true);
      guard("Loading generated resumes", () =>
        getJSONOrNull<GeneratedResumeRecord[]>("/api/resumes")
      ).then((data) => {
        setResumesList(data || []);
        setResumesLoading(false);
      });
    }
  }, [activeSubTab]);

  // Auto-scroll to targeted resume card when loaded
  useEffect(() => {
    if (activeSubTab === "tailored" && (targetJobId || targetResumeId) && resumesList.length > 0) {
      const match = resumesList.find(
        (r) =>
          (targetJobId && String(r.job_id) === targetJobId) ||
          (targetResumeId && String(r.id) === targetResumeId)
      );
      if (match) {
        const timer = setTimeout(() => {
          const el = document.getElementById(`resume-card-${match.id}`);
          if (el) {
            el.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        }, 100);
        return () => clearTimeout(timer);
      }
    }
  }, [activeSubTab, targetJobId, targetResumeId, resumesList]);

  const handleSaveMasterProfile = async (formToSave?: ProfileFormState) => {
    const form = formToSave || masterProfile;
    if (!form) return;
    const payload = toProfilePayload(form);
    setSaving(true);
    setSaveStatus(null);
    try {
      const resp = await fetch("/api/resume-builder/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (resp.ok) {
        setSaveStatus("Saved and synced for scoring & resumes!");
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

  const handleResumeUploadSuccess = (data: ResumeUploadResponse) => {
    if (data.profile) {
      setMasterProfile(toFormState(data.profile));
      setUploadedResumeText(data.raw_text);
      setShowCopilot(true); // Open copilot automatically so user can review & chat!
    }
  };

  return (
    <section class="tab-pane active" style={{ padding: "1.5rem" }}>
      {/* Sub-tab navigation */}
      <div class="subtab-bar-container">
        <div class="subtab-bar">
          <Link
            href="/resumes"
            class={`action-pill ${activeSubTab === "master" ? "active" : ""}`}
            style={{ textDecoration: "none" }}
          >
            <i class="fa-solid fa-user-gear"></i> Master Profile & Copilot
          </Link>
          <Link
            href="/resumes?tab=tailored"
            class={`action-pill ${activeSubTab === "tailored" ? "active" : ""}`}
            style={{ textDecoration: "none" }}
          >
            <i class="fa-solid fa-file-lines"></i> Tailored Resumes Library ({resumesList.length})
          </Link>
        </div>
      </div>

      {/* Sub-tab 1: Unified Master Profile */}
      {activeSubTab === "master" && (
        <div class="master-profile-layout">
          {masterLoading || !masterProfile ? (
            <div class="resume-card">
              <i class="fa-solid fa-spinner fa-spin"></i> Loading master profile...
            </div>
          ) : (
            <div class={`profile-main-grid ${showCopilot ? "with-copilot" : ""}`}>
              {/* Left Column: Form Editor */}
              <div class="profile-editor-column">
                {/* Header Actions */}
                <div class="profile-header-card">
                  <div>
                    <h3 style={{ margin: 0, fontSize: "1.25rem" }}>Master Candidate Profile</h3>
                    <p style={{ color: "var(--ink-muted)", margin: "0.25rem 0 0 0", fontSize: "0.85rem" }}>
                      Single source of truth for job fit scoring and tailored resume generation.
                    </p>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
                    <button
                      class={`action-pill ${showCopilot ? "active text-blue" : ""}`}
                      onClick={() => setShowCopilot(!showCopilot)}
                    >
                      <i class="fa-solid fa-robot"></i> {showCopilot ? "Hide Copilot" : "AI Copilot"}
                    </button>
                    <button
                      class="action-pill text-green active"
                      onClick={() => handleSaveMasterProfile()}
                      disabled={saving}
                    >
                      <i class={`fa-solid ${saving ? "fa-spinner fa-spin" : "fa-floppy-disk"}`}></i>{" "}
                      {saving ? "Saving..." : "Save & Sync Profile"}
                    </button>
                  </div>
                </div>

                {saveStatus && (
                  <div
                    class={`dropzone-status-msg ${saveStatus.includes("error") ? "error" : "success"}`}
                    style={{ marginBottom: "1rem" }}
                  >
                    <i class={`fa-solid ${saveStatus.includes("error") ? "fa-triangle-exclamation" : "fa-check-circle"}`}></i>{" "}
                    {saveStatus}
                  </div>
                )}

                {/* Drag and Drop Resume Ingestion Zone */}
                <div style={{ marginBottom: "1.5rem" }}>
                  <ResumeDropzone onUploadSuccess={handleResumeUploadSuccess} disabled={saving} />
                </div>

                {/* Section 1: Personal & Contact Information */}
                <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                  <h4>1. Personal & Contact Information</h4>
                  <div class="profile-form-grid">
                    <div>
                      <label class="form-label">Full Name</label>
                      <input
                        type="text"
                        class="filter-input"
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
                      <label class="form-label">Email</label>
                      <input
                        type="email"
                        class="filter-input"
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
                      <label class="form-label">Phone</label>
                      <input
                        type="text"
                        class="filter-input"
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
                      <label class="form-label">Location (City, State)</label>
                      <input
                        type="text"
                        class="filter-input"
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
                      <label class="form-label">GitHub URL</label>
                      <input
                        type="text"
                        class="filter-input"
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
                      <label class="form-label">LinkedIn URL</label>
                      <input
                        type="text"
                        class="filter-input"
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
                      <label class="form-label">Website / Portfolio</label>
                      <input
                        type="text"
                        class="filter-input"
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
                </div>

                {/* Section 2: Work Eligibility & Availability */}
                <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                  <h4>2. Work Eligibility & Availability</h4>
                  <div class="profile-form-grid">
                    <div>
                      <label class="form-label">Citizenship & Work Auth (comma-separated)</label>
                      <input
                        type="text"
                        class="filter-input"
                        placeholder="e.g. US Citizen, Permanent Resident, EU Citizen"
                        value={masterProfile.eligibility.citizenshipText}
                        onInput={(e) =>
                          setMasterProfile({
                            ...masterProfile,
                            eligibility: {
                              ...masterProfile.eligibility,
                              citizenshipText: (e.target as HTMLInputElement).value,
                            },
                          })
                        }
                      />
                      <small style={{ color: "var(--ink-muted)", fontSize: "0.75rem" }}>
                        e.g. US Citizen, Permanent Resident, EU Citizen (no sponsorship needed)
                      </small>
                    </div>

                    <div>
                      <label class="form-label">Location Availability (comma-separated)</label>
                      <input
                        type="text"
                        class="filter-input"
                        placeholder="e.g. Los Angeles, CA, Remote"
                        value={masterProfile.eligibility.locationsText}
                        onInput={(e) =>
                          setMasterProfile({
                            ...masterProfile,
                            eligibility: {
                              ...masterProfile.eligibility,
                              locationsText: (e.target as HTMLInputElement).value,
                            },
                          })
                        }
                      />
                    </div>

                    <div>
                      <label class="form-label">Minimum Base Compensation (USD Floor)</label>
                      <input
                        type="number"
                        class="filter-input"
                        placeholder="e.g. 90000"
                        value={masterProfile.eligibility?.comp_floor_usd ?? ""}
                        onInput={(e) => {
                          const val = (e.target as HTMLInputElement).value;
                          setMasterProfile({
                            ...masterProfile,
                            eligibility: {
                              ...masterProfile.eligibility,
                              comp_floor_usd: val ? Number(val) : null,
                            },
                          });
                        }}
                      />
                    </div>

                    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "1.5rem" }}>
                      <input
                        type="checkbox"
                        id="relocateCheck"
                        checked={masterProfile.eligibility?.willing_to_relocate ?? false}
                        onChange={(e) =>
                          setMasterProfile({
                            ...masterProfile,
                            eligibility: {
                              ...masterProfile.eligibility,
                              willing_to_relocate: (e.target as HTMLInputElement).checked,
                            },
                          })
                        }
                      />
                      <label htmlFor="relocateCheck" style={{ fontSize: "0.85rem", cursor: "pointer" }}>
                        Open to Relocation for the right role
                      </label>
                    </div>
                  </div>
                </div>

                {/* Section 3: Positioning, Executive Summary & AI Directives */}
                <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                  <h4>3. Positioning, Executive Summary & AI Directives</h4>
                  <div class="profile-form-grid" style={{ marginBottom: "1rem" }}>
                    <div>
                      <label class="form-label">Years of Professional Experience</label>
                      <input
                        type="number"
                        step="0.5"
                        class="filter-input"
                        value={masterProfile.years_experience ?? 4.0}
                        onInput={(e) =>
                          setMasterProfile({
                            ...masterProfile,
                            years_experience: Number((e.target as HTMLInputElement).value),
                          })
                        }
                      />
                    </div>
                    <div>
                      <label class="form-label">Seniority Descriptor</label>
                      <input
                        type="text"
                        class="filter-input"
                        placeholder="e.g. Mid / Senior"
                        value={masterProfile.seniority || ""}
                        onInput={(e) =>
                          setMasterProfile({
                            ...masterProfile,
                            seniority: (e.target as HTMLInputElement).value,
                          })
                        }
                      />
                    </div>
                  </div>

                  <div style={{ marginBottom: "1.25rem" }}>
                    <label class="form-label">
                      Executive Summary / Elevator Pitch{" "}
                      <span style={{ color: "var(--ink-muted)", fontSize: "0.75rem" }}>
                        (Recruiter-facing sales pitch tailored into the top of generated resumes)
                      </span>
                    </label>
                    <textarea
                      class="profile-textarea"
                      rows={5}
                      style={{ minHeight: "115px" }}
                      placeholder="e.g. Staff Infrastructure Engineer with 8+ years architecting high-throughput distributed systems and cloud platforms. Proven track record reducing infrastructure costs by 35% while scaling Kafka and Kubernetes clusters."
                      value={masterProfile.executive_summary}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          executive_summary: (e.target as HTMLTextAreaElement).value,
                        })
                      }
                    />
                  </div>

                  <div style={{ marginBottom: "1.25rem" }}>
                    <label class="form-label">
                      AI Strategic Directives & Guidance{" "}
                      <span style={{ color: "var(--ink-muted)", fontSize: "0.75rem" }}>
                        (Internal instructions for scoring & tailoring; not printed on resume)
                      </span>
                    </label>
                    <textarea
                      class="profile-textarea"
                      rows={6}
                      style={{ minHeight: "135px" }}
                      placeholder="e.g. Focus on distributed systems and platform roles. Open to fintech, robotics, and developer tooling; avoid pure frontend roles. Count Go and Python experience as equivalent to Java backend requirements."
                      value={masterProfile.model_guidance}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          model_guidance: (e.target as HTMLTextAreaElement).value,
                        })
                      }
                    />
                  </div>

                  <div>
                    <label class="form-label">
                      Non-Negotiable Dealbreakers (one per line or comma-separated){" "}
                      <span style={{ color: "var(--ink-muted)", fontSize: "0.75rem" }}>
                        (Hard disqualifiers that trigger an immediate fit: false verdict)
                      </span>
                    </label>
                    <textarea
                      class="profile-textarea"
                      rows={4}
                      style={{ minHeight: "95px" }}
                      placeholder={"e.g.\n24-hour on-call site reliability rotations\nDoD security clearance required\nUnpaid overtime / 60+ hour work weeks"}
                      value={masterProfile.dealbreakersText}
                      onInput={(e) =>
                        setMasterProfile({
                          ...masterProfile,
                          dealbreakersText: (e.target as HTMLTextAreaElement).value,
                        })
                      }
                    />
                  </div>
                </div>

                {/* Section 4: Work Experience */}
                <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <h4>4. Work Experience (Employment)</h4>
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
                              location: "",
                              projects: [{ heading: "", bullets: [] }],
                            },
                          ],
                        });
                      }}
                    >
                      <i class="fa-solid fa-plus"></i> Add Role
                    </button>
                  </div>

                  {masterProfile.experience.map((role, rIdx) => (
                    <div key={rIdx} class="experience-role-card">
                      <div class="role-header-row">
                        <input
                          type="text"
                          placeholder="Title (e.g. Software Engineer)"
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
                        <input
                          type="text"
                          placeholder="Location (e.g. Remote / SF, CA)"
                          class="filter-input"
                          style={{ flex: 1.5 }}
                          value={role.location || ""}
                          onInput={(e) => {
                            const next = [...masterProfile.experience];
                            next[rIdx].location = (e.target as HTMLInputElement).value;
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

                      {/* Projects inside Role */}
                      <div style={{ marginTop: "0.75rem", paddingLeft: "0.5rem" }}>
                        {role.projects.map((proj, pIdx) => (
                          <div key={pIdx} class="project-card-block">
                            <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                              <input
                                type="text"
                                placeholder="Optional Sub-heading (e.g. Designed replacement for 20 sites: — leave empty for direct bullets)"
                                class="filter-input"
                                style={{ flex: 1, fontSize: "0.85rem" }}
                                value={proj.heading || ""}
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
                                style={{ fontSize: "0.75rem", padding: "0.2rem 0.4rem" }}
                                onClick={() => {
                                  const next = [...masterProfile.experience];
                                  next[rIdx].projects = next[rIdx].projects.filter((_, i) => i !== pIdx);
                                  setMasterProfile({ ...masterProfile, experience: next });
                                }}
                              >
                                <i class="fa-solid fa-xmark"></i>
                              </button>
                            </div>

                            {/* Bullets */}
                            <div style={{ marginTop: "0.4rem", paddingLeft: "0.75rem" }}>
                              {proj.bullets.map((b, bIdx) => (
                                <div key={bIdx} style={{ display: "flex", gap: "0.4rem", alignItems: "center", marginTop: "0.25rem" }}>
                                  <span style={{ color: "var(--ink-muted)" }}>•</span>
                                  <input
                                    type="text"
                                    placeholder="Quantified impact bullet (action + tech + result)"
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
                                style={{ fontSize: "0.75rem", marginTop: "0.35rem", padding: "0.2rem 0.5rem" }}
                                onClick={() => {
                                  const next = [...masterProfile.experience];
                                  next[rIdx].projects[pIdx].bullets.push("Engineered...");
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
                          style={{ fontSize: "0.8rem", marginTop: "0.5rem" }}
                          onClick={() => {
                            const next = [...masterProfile.experience];
                            next[rIdx].projects.push({
                              heading: "",
                              bullets: ["Engineered..."],
                            });
                            setMasterProfile({ ...masterProfile, experience: next });
                          }}
                        >
                          <i class="fa-solid fa-plus"></i> Add Sub-heading / Bullet Group
                        </button>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Section 5: Standalone Personal & Open Source Projects */}
                <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <h4>5. Personal & Open Source Projects</h4>
                    <button
                      class="action-pill"
                      onClick={() => {
                        setMasterProfile({
                          ...masterProfile,
                          projects: [
                            ...(masterProfile.projects || []),
                            {
                              heading: "New Project",
                              url: "https://github.com/...",
                              bullets: ["Engineered..."],
                            },
                          ],
                        });
                      }}
                    >
                      <i class="fa-solid fa-plus"></i> Add Project
                    </button>
                  </div>

                  {(masterProfile.projects || []).map((proj, pIdx) => (
                    <div key={pIdx} class="experience-role-card" style={{ marginTop: "0.75rem" }}>
                      <div class="role-header-row">
                        <input
                          type="text"
                          placeholder="Project Title / Heading (e.g. CareerRadar / ZMK Firmware)"
                          class="filter-input"
                          style={{ flex: 3 }}
                          value={proj.heading || ""}
                          onInput={(e) => {
                            const next = [...masterProfile.projects];
                            next[pIdx].heading = (e.target as HTMLInputElement).value;
                            setMasterProfile({ ...masterProfile, projects: next });
                          }}
                        />
                        <input
                          type="text"
                          placeholder="GitHub / Live Demo URL (optional)"
                          class="filter-input"
                          style={{ flex: 3 }}
                          value={proj.url || ""}
                          onInput={(e) => {
                            const next = [...masterProfile.projects];
                            next[pIdx].url = (e.target as HTMLInputElement).value;
                            setMasterProfile({ ...masterProfile, projects: next });
                          }}
                        />
                        <button
                          class="action-pill text-red"
                          onClick={() => {
                            const next = masterProfile.projects.filter((_, i) => i !== pIdx);
                            setMasterProfile({ ...masterProfile, projects: next });
                          }}
                        >
                          <i class="fa-solid fa-trash"></i>
                        </button>
                      </div>

                      {/* Bullets */}
                      <div style={{ marginTop: "0.5rem", paddingLeft: "0.5rem" }}>
                        {proj.bullets.map((b, bIdx) => (
                          <div key={bIdx} style={{ display: "flex", gap: "0.4rem", alignItems: "center", marginTop: "0.25rem" }}>
                            <span style={{ color: "var(--ink-muted)" }}>•</span>
                            <input
                              type="text"
                              placeholder="Quantified impact bullet (action + tech + result)"
                              class="filter-input"
                              style={{ flex: 1, fontSize: "0.85rem" }}
                              value={b}
                              onInput={(e) => {
                                const next = [...masterProfile.projects];
                                next[pIdx].bullets[bIdx] = (e.target as HTMLInputElement).value;
                                setMasterProfile({ ...masterProfile, projects: next });
                              }}
                            />
                            <button
                              class="action-pill text-red"
                              style={{ padding: "0.15rem 0.4rem" }}
                              onClick={() => {
                                const next = [...masterProfile.projects];
                                next[pIdx].bullets = next[pIdx].bullets.filter((_, i) => i !== bIdx);
                                setMasterProfile({ ...masterProfile, projects: next });
                              }}
                            >
                              <i class="fa-solid fa-minus"></i>
                            </button>
                          </div>
                        ))}
                        <button
                          class="action-pill"
                          style={{ fontSize: "0.75rem", marginTop: "0.35rem", padding: "0.2rem 0.5rem" }}
                          onClick={() => {
                            const next = [...masterProfile.projects];
                            next[pIdx].bullets.push("Engineered...");
                            setMasterProfile({ ...masterProfile, projects: next });
                          }}
                        >
                          <i class="fa-solid fa-plus"></i> Add Bullet
                        </button>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Section 6: Skills & Competencies */}
                <div class="resume-card" style={{ marginBottom: "1.5rem" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <h4>6. Skills & Competencies</h4>
                    <button
                      class="action-pill"
                      onClick={() => {
                        setMasterProfile({
                          ...masterProfile,
                          skills: [...masterProfile.skills, { category: "New Category", skillsText: "" }],
                        });
                      }}
                    >
                      <i class="fa-solid fa-plus"></i> Add Category
                    </button>
                  </div>
                  {masterProfile.skills.map((cat, idx) => (
                    <div key={idx} style={{ display: "flex", gap: "0.75rem", alignItems: "center", marginTop: "0.75rem" }}>
                      <input
                        type="text"
                        placeholder="Category Name"
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
                        value={cat.skillsText}
                        onInput={(e) => {
                          const next = [...masterProfile.skills];
                          next[idx].skillsText = (e.target as HTMLInputElement).value;
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

                {/* Section 7: Education History */}
                <div class="resume-card" style={{ marginBottom: "2rem" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <h4>7. Education History</h4>
                    <button
                      class="action-pill"
                      onClick={() => {
                        setMasterProfile({
                          ...masterProfile,
                          education: [...masterProfile.education, { institution: "", degree: "", details: "" }],
                        });
                      }}
                    >
                      <i class="fa-solid fa-plus"></i> Add Degree
                    </button>
                  </div>
                  {masterProfile.education.map((edu, idx) => (
                    <div key={idx} style={{ display: "flex", gap: "0.75rem", alignItems: "center", marginTop: "0.75rem" }}>
                      <input
                        type="text"
                        placeholder="Institution (e.g. UCLA)"
                        class="filter-input"
                        style={{ flex: 1.5 }}
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
                        style={{ flex: 1.5 }}
                        value={edu.degree}
                        onInput={(e) => {
                          const next = [...masterProfile.education];
                          next[idx].degree = (e.target as HTMLInputElement).value;
                          setMasterProfile({ ...masterProfile, education: next });
                        }}
                      />
                      <input
                        type="text"
                        placeholder="Details (e.g. Honors, GPA, thesis)"
                        class="filter-input"
                        style={{ flex: 1.5 }}
                        value={edu.details || ""}
                        onInput={(e) => {
                          const next = [...masterProfile.education];
                          next[idx].details = (e.target as HTMLInputElement).value;
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

                {/* Bottom Save Bar */}
                <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "2rem" }}>
                  <button
                    class="action-pill text-green active"
                    onClick={() => handleSaveMasterProfile()}
                    disabled={saving}
                    style={{ padding: "0.6rem 1.75rem", fontSize: "1rem" }}
                  >
                    <i class={`fa-solid ${saving ? "fa-spinner fa-spin" : "fa-floppy-disk"}`}></i>{" "}
                    {saving ? "Saving..." : "Save Master Profile & Sync Scoring"}
                  </button>
                </div>
              </div>

              {/* Right Column: AI Copilot Chat Panel (when toggled open) */}
              {showCopilot && (
                <div class="profile-copilot-column">
                  <ProfileChatPanel
                    currentProfile={toProfilePayload(masterProfile)}
                    resumeText={uploadedResumeText}
                    onUpdateProfile={(updated) => setMasterProfile(toFormState(updated))}
                    onClose={() => setShowCopilot(false)}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Sub-tab 2: Tailored Resumes Library */}
      {activeSubTab === "tailored" && (
        <div style={{ maxWidth: "1000px" }}>
          <div style={{ marginBottom: "1rem" }}>
            <h3 style={{ margin: 0 }}>Tailored Resumes Library</h3>
            <p style={{ color: "var(--ink-muted)", margin: "0.25rem 0 0 0" }}>
              1-page tailored resumes generated for specific job postings.
            </p>
          </div>

          {(targetJobId || targetResumeId) && (
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: "1rem",
                padding: "0.6rem 1rem",
                borderRadius: "6px",
                backgroundColor: "rgba(139, 92, 246, 0.12)",
                border: "1px solid var(--accent-purple, #a78bfa)",
                flexWrap: "wrap",
                gap: "0.5rem",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <i class="fa-solid fa-bullseye" style={{ color: "#a78bfa" }}></i>
                <span style={{ fontSize: "0.9rem", fontWeight: 600 }}>
                  {targetJobId
                    ? `Viewing tailored resume for Job #${targetJobId}`
                    : `Viewing Tailored Resume #${targetResumeId}`}
                </span>
              </div>
              <button
                class="action-pill"
                onClick={() => navigate("/resumes?tab=tailored")}
                style={{ fontSize: "0.8rem", padding: "0.25rem 0.6rem" }}
              >
                <i class="fa-solid fa-xmark"></i> Show All Resumes
              </button>
            </div>
          )}

          {resumesLoading ? (
            <p>Loading generated resumes...</p>
          ) : resumesList.length === 0 ? (
            <div class="resume-card">
              <p style={{ color: "var(--ink-muted)" }}>
                No tailored resumes generated yet. Open any job in Jobs and click{" "}
                <strong>"Generate Resume"</strong>!
              </p>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              {resumesList.map((res) => {
                const isTargeted = Boolean(
                  (targetJobId && String(res.job_id) === targetJobId) ||
                    (targetResumeId && String(res.id) === targetResumeId)
                );

                return (
                  <div
                    key={res.id}
                    id={`resume-card-${res.id}`}
                    class="resume-card"
                    style={
                      isTargeted
                        ? {
                            border: "2px solid #8b5cf6",
                            boxShadow: "0 0 12px rgba(139, 92, 246, 0.35)",
                          }
                        : undefined
                    }
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "flex-start",
                        gap: "1rem",
                        flexWrap: "wrap",
                      }}
                    >
                      <div>
                        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
                          {res.job_id ? (
                            <Link
                              href={`/?job=${res.job_id}`}
                              style={{ color: "inherit", textDecoration: "none" }}
                              title="Open this job in Jobs"
                            >
                              <h3 style={{ margin: 0, display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
                                {res.job_title || "Software Engineer"} @ {res.job_company || "Company"}
                                <i
                                  class="fa-solid fa-arrow-up-right-from-square"
                                  style={{ fontSize: "0.75rem", color: "var(--ink-muted)" }}
                                ></i>
                              </h3>
                            </Link>
                          ) : (
                            <h3 style={{ margin: 0 }}>
                              {res.job_title || "Software Engineer"} @ {res.job_company || "Company"}
                            </h3>
                          )}
                          {isTargeted && (
                            <span
                              class="skill-tag text-purple"
                              style={{ fontSize: "0.75rem", padding: "0.1rem 0.4rem", fontWeight: 700 }}
                            >
                              <i class="fa-solid fa-link"></i> Linked Job
                            </span>
                          )}
                        </div>

                        <div style={{ fontSize: "0.85rem", color: "var(--ink-muted)", marginTop: "0.25rem" }}>
                          {res.job_id ? (
                            <span style={{ marginRight: "0.5rem" }}>Job #{res.job_id}</span>
                          ) : null}
                          <span>
                            Generated {(res.created_at || "").slice(0, 16)} · Model:{" "}
                            {res.model || "deepseek-chat"}
                            {res.job_location ? ` · ${res.job_location}` : ""}
                          </span>
                        </div>
                      </div>

                      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
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

                        {(res.typst_path || res.resume) && (
                          <a
                            href={`/api/resumes/${res.id}/download?format=typst`}
                            class="action-pill"
                            style={{ textDecoration: "none" }}
                            download
                            title="Download Typst markup source"
                          >
                            <i class="fa-solid fa-code"></i> Typst
                          </a>
                        )}
                      </div>
                    </div>

                    {res.summary && (
                      <p style={{ marginTop: "0.75rem", fontSize: "0.9rem" }}>
                        <strong>Summary:</strong> {res.summary}
                      </p>
                    )}

                    {res.ats_feedback && (
                      <div
                        style={{
                          marginTop: "0.5rem",
                          padding: "0.5rem 0.75rem",
                          borderRadius: "4px",
                          backgroundColor: "var(--bg-screen-alt)",
                          fontSize: "0.85rem",
                          color: "var(--ink-secondary)",
                        }}
                      >
                        <i class="fa-solid fa-comments"></i> <strong>ATS Screener Feedback:</strong>{" "}
                        {res.ats_feedback}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
