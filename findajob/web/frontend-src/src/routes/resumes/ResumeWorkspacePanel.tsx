import { useEffect, useState } from "preact/hooks";
import { workspaceRequest, type ResumeWorkspace, type ResumeDraft } from "./workspaceApi";

interface Props {
  jobId: number;
  onGenerated: () => void;
}

function DraftText({ draft, other }: { draft: ResumeDraft; other?: ResumeDraft }) {
  const otherLines = other ? new Set(other.text.split("\n")) : null;
  return (
    <pre class="workspace-draft-text">
      {draft.text.split("\n").map((line, i) => (
        <span
          key={i}
          class={otherLines && line && !otherLines.has(line) ? "workspace-changed" : ""}
        >
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

export function ResumeWorkspacePanel({ jobId, onGenerated }: Props) {
  const [workspace, setWorkspace] = useState<ResumeWorkspace | null>(null);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [compareId, setCompareId] = useState<number | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [sources, setSources] = useState<{ draftId: number; evidence: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endpoint = `/api/jobs/${jobId}/resume`;

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const data = await workspaceRequest<ResumeWorkspace>(`${endpoint}/workspace`);
        if (active) setWorkspace(data);
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : String(e));
      }
    };
    load();
    const timer = setInterval(load, 4000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [endpoint]);

  const draft =
    workspace?.drafts.find((d) => d.id === draftId) ||
    workspace?.drafts.find((d) => d.id === workspace.selected_draft_id) ||
    workspace?.drafts[0];
  const compare = workspace?.drafts.find((d) => d.id === compareId);
  const running = busy || workspace?.status === "running";

  async function action(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      try {
        setWorkspace(await workspaceRequest<ResumeWorkspace>(`${endpoint}/workspace`));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      setBusy(false);
    }
  }

  return (
    <div class="resume-card resume-workspace">
      <h3>Resume workspace · Job #{jobId}</h3>
      <p>
        Draft from your profile and complete career memo, clarify important gaps, then review and
        revise.
      </p>
      {error && (
        <p role="alert" class="text-red">
          {error}
        </p>
      )}
      {workspace?.error && <p role="status">{workspace.error}</p>}
      <div class="workspace-actions">
        <span>Status: {workspace?.status || "loading"}</span>
        <button
          class="action-pill text-green"
          disabled={running || !workspace}
          onClick={() =>
            action(async () => {
              await workspaceRequest(`${endpoint}/generate`, "POST");
              setDraftId(null);
              onGenerated();
            })
          }
        >
          {running
            ? "Generating / reviewing..."
            : draft
              ? "Revise from latest evidence"
              : "Generate first draft"}
        </button>
        {workspace?.status === "running" && (
          <button
            class="action-pill"
            onClick={() =>
              action(async () => {
                await workspaceRequest(`${endpoint}/recover`, "POST");
              })
            }
          >
            Recover interrupted run (after 1 hour)
          </button>
        )}
      </div>
      {draft && workspace && (
        <>
          <div class="workspace-actions">
            <label>
              View draft{" "}
              <select
                aria-label="View draft"
                value={draft.id}
                onChange={(e) => {
                  setDraftId(Number(e.currentTarget.value));
                  setSources(null);
                }}
              >
                {workspace.drafts.map((d) => (
                  <option key={d.id} value={d.id}>
                    #{d.id} · {d.created_at} · review {d.review.recruiter?.score ?? "pending"}
                    {workspace.selected_draft_id === d.id ? " · selected" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Compare with{" "}
              <select
                aria-label="Compare draft"
                value={compareId ?? ""}
                onChange={(e) => setCompareId(Number(e.currentTarget.value) || null)}
              >
                <option value="">None</option>
                {workspace.drafts
                  .filter((d) => d.id !== draft.id)
                  .map((d) => (
                    <option key={d.id} value={d.id}>
                      Draft #{d.id} · review {d.review.recruiter?.score ?? "pending"}
                    </option>
                  ))}
              </select>
            </label>
            <button
              class="action-pill"
              disabled={running || !draft.review.evidence.grounded || !draft.layout.is_valid}
              onClick={() =>
                action(async () => {
                  await workspaceRequest(`${endpoint}/drafts/${draft.id}/select`, "POST");
                  onGenerated();
                })
              }
            >
              Select & export this draft
            </button>
            {draft.generated_resume_id && (
              <a
                class="action-pill"
                download
                href={`/api/resumes/${draft.generated_resume_id}/download?format=pdf`}
              >
                Download PDF
              </a>
            )}
          </div>
          <p>
            Memo revision #{draft.memo_revision_id ?? "none"} · Grounding:{" "}
            {draft.review.evidence.grounded ? "supported" : "needs review"}
            {" · "}Layout: {draft.layout.pdf_verified ? "one page verified" : "not approved"}
            {" · "}Resume review: {draft.review.recruiter?.score ?? "pending"}/10 (AI judgment)
          </p>
          {draft.memo_revision_id !== workspace.memo_revision_id && (
            <p>Memo has changed since this draft. Revise to review the latest evidence.</p>
          )}
          <p>{draft.review.evidence.revision_advice}</p>
          <p>{draft.review.recruiter?.actionable_feedback}</p>
          {[
            ...(draft.review.evidence.unsupported_claims || []),
            ...(draft.review.evidence.conflicts || []),
            ...(draft.review.evidence.qualification_gaps || []),
            ...(draft.layout.violations || []),
          ].map((issue, i) => (
            <p key={i}>• {issue}</p>
          ))}
          <div class={`workspace-comparison ${compare ? "two-drafts" : ""}`}>
            <div>
              <h4>Draft #{draft.id}</h4>
              <DraftText draft={draft} other={compare} />
            </div>
            {compare && (
              <div>
                <h4>
                  Draft #{compare.id} · Review {compare.review.recruiter?.score ?? "pending"}/10
                </h4>
                <DraftText draft={compare} other={draft} />
              </div>
            )}
          </div>
          {compare && <p>Highlighted lines differ between drafts.</p>}
          <details>
            <summary>Evidence citations ({draft.review.evidence.citations.length})</summary>
            <button
              class="action-pill"
              onClick={() =>
                action(async () => {
                  const data = await workspaceRequest<{ evidence: string }>(
                    `${endpoint}/drafts/${draft.id}/sources`,
                  );
                  setSources({ draftId: draft.id, evidence: data.evidence });
                })
              }
            >
              Load numbered source snapshot
            </button>
            {draft.review.evidence.citations.map((c, i) => (
              <p key={i}>
                <strong>{c.sources.join(", ")}</strong> — {c.claim}
              </p>
            ))}
            {sources?.draftId === draft.id && (
              <pre class="workspace-source-text">{sources.evidence}</pre>
            )}
          </details>
          {draft.review.evidence.questions.length > 0 && (
            <div>
              <h4>Interview — clarify facts that could strengthen this application</h4>
              <p>
                Answers are saved as dated additions to your career memo. Save answers, then revise
                the draft above.
              </p>
              {draft.review.evidence.questions.map((question) => {
                const saved = workspace.answers.find(
                  (a) => a.draft_id === draft.id && a.question === question,
                );
                const key = `${draft.id}:${question}`;
                return (
                  <div class="workspace-question" key={key}>
                    <label>
                      {question}
                      <textarea
                        aria-label={question}
                        rows={3}
                        disabled={running || Boolean(saved)}
                        value={saved?.answer ?? answers[key] ?? ""}
                        onInput={(e) => {
                          setAnswers({ ...answers, [key]: e.currentTarget.value });
                        }}
                      />
                    </label>
                    {saved ? (
                      <p>Saved {saved.created_at}</p>
                    ) : (
                      <button
                        class="action-pill"
                        disabled={running || !answers[key]?.trim()}
                        onClick={() =>
                          action(async () => {
                            await workspaceRequest(`${endpoint}/answers`, "POST", {
                              draft_id: draft.id,
                              question,
                              answer: answers[key],
                              base_revision_id: workspace.memo_revision_id,
                            });
                          })
                        }
                      >
                        Save answer to memo
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          <details>
            <summary>Interview history ({workspace.answers.length})</summary>
            {workspace.answers.map((a) => (
              <div key={a.id}>
                <strong>{a.question}</strong>
                <p>{a.answer}</p>
                <small>{a.created_at}</small>
              </div>
            ))}
          </details>
        </>
      )}
    </div>
  );
}
