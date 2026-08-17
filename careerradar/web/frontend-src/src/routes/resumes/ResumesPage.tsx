import { useEffect, useState } from "preact/hooks";
import { getJSONOrNull, guard } from "../../api/client";
import type { ProfileRecord, ProfileSkill } from "../../api/types";

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
        <span key={skill.key} class="skill-tag" title={`level ${skill.level} — ${levelLabel(skill.level)}`}>
          {skill.label || skill.key.replace(/_/g, " ")} <em>{levelLabel(skill.level)}</em>
        </span>
      ))}
    </>
  );
}

// Ported from `templates/tabs/resumes.html` + `frontend/js/features/resumes.js`.
export function ResumesPage() {
  const [record, setRecord] = useState<ProfileRecord | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    guard("Loading the active profile", () => getJSONOrNull<ProfileRecord>("/api/profile")).then(
      (data) => {
        if (!cancelled) setRecord(data);
      }
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (record === undefined) return <section class="tab-pane active" />;

  return (
    <section class="tab-pane active">
      <div class="resumes-grid">
        {record === null ? (
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
                Profile version {record.version} · built {(record.created_at || "").slice(0, 10)} ·{" "}
                {record.model || "unknown model"}
              </span>
              <h3>{record.profile.seniority || "Active profile"}</h3>
            </div>
            <div class="divider"></div>
            <p class="verdict-summary">{record.profile.bio || ""}</p>
            <h4>Skills Vector ({record.profile.skills.length})</h4>
            <div class="skills-scroll-area">
              <SkillTags skills={record.profile.skills} />
            </div>
            <h4>Built from</h4>
            <ul class="detail-list">
              {record.documents.length === 0 ? (
                <li>No source documents recorded.</li>
              ) : (
                record.documents.map((doc, i) => {
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
    </section>
  );
}
