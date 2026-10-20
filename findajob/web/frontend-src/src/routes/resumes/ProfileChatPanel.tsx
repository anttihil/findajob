import { useState, useRef, useEffect } from "preact/hooks";
import type {
  ProfileChatMessage,
  ProfileChatResponse,
  ResumeMasterProfile,
} from "../../api/types";

type ProfileChatPanelProps = {
  onClose: () => void;
} & ({
  mode?: "profile";
  currentProfile: ResumeMasterProfile;
  resumeText: string | null;
  onUpdateProfile: (updated: ResumeMasterProfile) => void;
} | {
  mode: "memo";
  currentMarkdown: string;
  onUpdateMemo: (markdown: string) => void;
});

interface MemoChatResponse {
  reply: string;
  updated_markdown: string | null;
  changes_made: string[];
}

const QUICK_PROMPTS = [
  "Polish my executive summary for resume header",
  "Suggest strategic model guidance directives for scoring",
  "Rewrite my project bullets to be more quantified",
  "Help me clarify my work eligibility and location preferences",
];

const MEMO_PROMPTS = [
  { label: "Compact memo", text: "Compact my career memo. Focus on what I did, what I offer, and what I am looking for. Remove notes about particular jobs I applied to and merge repeated clarifications. Preserve distinct achievements, metrics, dates, attribution, preferences, and meaningful uncertainty." },
  { label: "Clarify what I offer", text: "Edit my memo to make my strengths and what I have to offer clearer, using only supported facts." },
  { label: "Review for repetition", text: "Review my memo for repetition and stale application notes. Suggest improvements without editing it yet." },
];

export function ProfileChatPanel(props: ProfileChatPanelProps) {
  const isMemo = props.mode === "memo";
  const [pendingMemo, setPendingMemo] = useState<string | null>(null);
  const [messages, setMessages] = useState<ProfileChatMessage[]>([
    {
      role: "assistant",
      content: isMemo
        ? "I can compact or edit your achievements memo around your experience, strengths, and career direction. Review my proposal, apply it to the editor, then save a new revision."
        : "I can help refine experience bullets, categorize skills, and update your master profile for scoring and tailored resumes. What would you like to adjust?",
    },
  ]);
  const [inputVal, setInputVal] = useState("");
  const [loading, setLoading] = useState(false);
  const [lastChanges, setLastChanges] = useState<string[]>([]);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    chatInputRef.current?.focus({ preventScroll: true });
    return () => { trigger?.focus({ preventScroll: true }); };
  }, []);

  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const handleSendMessage = async (textToSend?: string) => {
    const query = (textToSend || inputVal).trim();
    if (!query || loading) return;

    const newMsgs: ProfileChatMessage[] = [...messages, { role: "user", content: query }];
    setMessages(newMsgs);
    setInputVal("");
    setLoading(true);

    try {
      const resp = await fetch(isMemo ? "/api/career-memo/chat" : "/api/profile/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(props.mode === "memo" ? {
          messages: newMsgs,
          current_markdown: pendingMemo ?? props.currentMarkdown,
        } : {
          messages: newMsgs,
          current_profile: props.currentProfile,
          resume_text: props.resumeText,
        }),
      });

      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.detail || `Chat failed with status ${resp.status}`);
      }

      if (props.mode === "memo") {
        const data: MemoChatResponse = await resp.json();
        setMessages((prev) => [...prev, { role: "assistant", content: data.reply }]);
        if (data.updated_markdown?.trim()) setPendingMemo(data.updated_markdown);
        setLastChanges(data.changes_made || []);
      } else {
        const data: ProfileChatResponse = await resp.json();
        setMessages((prev) => [...prev, { role: "assistant", content: data.reply }]);
        if (data.updated_profile) props.onUpdateProfile(data.updated_profile);
        setLastChanges(data.changes_made || []);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `⚠️ **Error:** ${msg}. Please try again.` },
      ]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div class="profile-chat-panel">
      <div class="chat-header">
        <div class="chat-title">
          <i class="fa-solid fa-robot"></i> <strong>{isMemo ? "AI MEMO COPILOT" : "AI PROFILE COPILOT"}</strong>
        </div>
        <button class="action-pill text-red" onClick={props.onClose} title="Close Chat">
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>

      {props.mode !== "memo" && props.resumeText && (
        <div class="chat-resume-badge">
          <i class="fa-solid fa-file-lines"></i> Active resume text loaded in context
        </div>
      )}

      {lastChanges.length > 0 && (
        <div class="chat-changes-banner">
          <strong>
            <i class="fa-solid fa-wand-magic-sparkles"></i> {isMemo ? "Proposed changes:" : "Applied Changes:"}
          </strong>
          <ul>
            {lastChanges.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      <div class="chat-messages" ref={chatScrollRef}>
        {messages.map((m, idx) => (
          <div key={idx} class={`chat-bubble-row ${m.role}`}>
            <div class="chat-bubble">
              <div class="bubble-sender">
                {m.role === "assistant" ? "COPILOT" : "YOU"}
              </div>
              <div class="bubble-content" style={{ whiteSpace: "pre-wrap" }}>
                {m.content}
              </div>
            </div>
          </div>
        ))}

        {loading && (
          <div class="chat-bubble-row assistant">
            <div class="chat-bubble thinking">
              <i class="fa-solid fa-spinner fa-spin"></i> {isMemo ? "Preparing memo proposal..." : "Analyzing and updating profile..."}
            </div>
          </div>
        )}
        {props.mode === "memo" && pendingMemo !== null && (
          <div class="memo-proposal">
            <h4>Memo proposal · not saved</h4>
            <p>{props.currentMarkdown.length.toLocaleString()} → {pendingMemo.length.toLocaleString()} characters.
              Review below or ask for further changes.</p>
            <details><summary>Original editor text</summary>
              <pre class="workspace-source-text">{props.currentMarkdown}</pre>
            </details>
            <textarea aria-label="Proposed career memo" rows={10} readOnly value={pendingMemo} />
            <div class="workspace-actions">
              <button class="action-pill text-green" disabled={loading}
                onClick={() => props.onUpdateMemo(pendingMemo)}>Apply to memo editor</button>
              <button class="action-pill" disabled={loading} onClick={() => {
                setPendingMemo(null); setLastChanges([]);
              }}>Discard proposal</button>
            </div>
            <p>Apply loads the proposal into your editor. Save memo revision there to keep it.</p>
          </div>
        )}
      </div>

      <div class="chat-quick-prompts">
        <span class="quick-label">Suggestions:</span>
        <div class="quick-pills-list">
          {(isMemo ? MEMO_PROMPTS : QUICK_PROMPTS.map((text) => ({ label: text, text }))).map((p, idx) => (
            <button
              key={idx}
              class="quick-pill"
              onClick={() => handleSendMessage(p.text)}
              disabled={loading}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div class="chat-input-row">
        <textarea
          ref={chatInputRef}
          class="chat-input"
          aria-label={isMemo ? "Message memo copilot" : "Message profile copilot"}
          rows={2}
          placeholder={isMemo ? "Ask copilot to compact, edit, or clarify your achievements memo..." : "Ask copilot to refine your summary, rewrite bullets, adjust skills..."}
          value={inputVal}
          onInput={(e) => setInputVal((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSendMessage();
            }
          }}
          disabled={loading}
        />
        <button
          class="primary-btn chat-send-btn"
          aria-label="Send message"
          onClick={() => handleSendMessage()}
          disabled={loading || !inputVal.trim()}
        >
          <i class="fa-solid fa-paper-plane"></i>
        </button>
      </div>
    </div>
  );
}
