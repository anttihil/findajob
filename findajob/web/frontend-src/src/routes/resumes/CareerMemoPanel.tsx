import { useEffect, useState } from "preact/hooks";
import { workspaceRequest, type CareerMemo } from "./workspaceApi";
import { ProfileChatPanel } from "./ProfileChatPanel";

export function CareerMemoPanel() {
  const [memo, setMemo] = useState<CareerMemo | null>(null);
  const [text, setText] = useState("");
  const [source, setSource] = useState("manual edit");
  const [revisions, setRevisions] = useState<CareerMemo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showCopilot, setShowCopilot] = useState(false);

  async function reload() {
    const current = await workspaceRequest<CareerMemo>("/api/career-memo");
    setMemo(current);
    setText(current.markdown || "");
    setRevisions(await workspaceRequest<CareerMemo[]>("/api/career-memo/revisions"));
  }

  useEffect(() => { reload().catch((e: Error) => setError(e.message)); }, []);

  async function save() {
    if (!memo) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const current = await workspaceRequest<CareerMemo>("/api/career-memo", "PUT", {
        markdown: text, source, base_revision_id: memo.id,
      });
      setMemo(current);
      setRevisions(await workspaceRequest<CareerMemo[]>("/api/career-memo/revisions"));
      setNotice(`Saved revision #${current.id}. New resume drafts will use this evidence.`);
      setSource("manual edit");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <details class="resume-card career-memo-panel">
      <summary><strong>Career memo — detailed work-history evidence</strong></summary>
      <p>Import your achievement notes in full. Dates, corrections, uncertainties and interview answers
        stay here; the master profile remains your structured baseline.</p>
      {error && <p role="alert" class="text-red">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {!memo && !error && <p>Loading career memo...</p>}
      <div class="workspace-actions">
        <button class="action-pill text-blue" disabled={!memo || busy || !text.trim()}
          onClick={() => setShowCopilot(true)}>
          <i class="fa-solid fa-robot"></i> AI Copilot
        </button>
        <label class="action-pill">Import Markdown / text
          <input aria-label="Import career memo" type="file" accept=".md,.txt,text/plain,text/markdown"
            disabled={!memo || busy} onChange={async (event) => {
              const input = event.currentTarget;
              const file = input.files?.[0];
              if (!file) return;
              try {
                setText(await file.text()); setSource(`import: ${file.name}`);
                setNotice("File loaded in editor. Save to create a memo revision.");
              } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
              input.value = "";
            }} />
        </label>
        <a class="action-pill" href="/api/career-memo/download" download>Export Markdown</a>
        <button class="action-pill" disabled={busy} onClick={() => {
          reload().then(() => { setError(""); setNotice("Latest memo loaded."); })
            .catch((e: Error) => setError(e.message));
        }}>Reload latest</button>
      </div>
      <textarea aria-label="Career memo Markdown" class="career-memo-editor" rows={20}
        value={text} disabled={!memo || busy} onInput={(e) => setText(e.currentTarget.value)} />
      <div class="workspace-actions">
        <span>{text.length.toLocaleString()} characters · Current revision {memo?.id ?? "none"}</span>
        <button class="action-pill text-green" onClick={save} disabled={!memo || busy || text === memo.markdown}>
          {busy ? "Saving..." : "Save memo revision"}
        </button>
      </div>
      <details><summary>Revision history</summary>
        {revisions.map((revision) => <div class="workspace-actions" key={revision.id}>
          <span>#{revision.id} · {revision.created_at} · {revision.source}</span>
          <button class="action-pill" disabled={busy} onClick={async () => {
            try {
              const old = await workspaceRequest<CareerMemo>(`/api/career-memo/revisions/${revision.id}`);
              setText(old.markdown); setSource(`restored revision ${revision.id}`);
              setNotice(`Revision #${revision.id} loaded. Save to restore it as a new revision.`);
            } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
          }}>Load into editor</button>
        </div>)}
      </details>
      {showCopilot && <div class="retro-modal-overlay profile-copilot-modal-overlay"
        role="dialog" aria-modal="true" aria-label="Career memo copilot"
        onClick={() => setShowCopilot(false)} onKeyDown={(e) => {
          if (e.key === "Escape") setShowCopilot(false);
        }} tabIndex={-1}>
        <div class="profile-copilot-modal" onClick={(e) => e.stopPropagation()}>
          <ProfileChatPanel mode="memo" currentMarkdown={text}
            onClose={() => setShowCopilot(false)} onUpdateMemo={(markdown) => {
              setText(markdown); setSource("AI memo copilot"); setError("");
              setNotice("Copilot proposal loaded in editor. Save to create a memo revision.");
              setShowCopilot(false);
            }} />
        </div>
      </div>}
    </details>
  );
}
