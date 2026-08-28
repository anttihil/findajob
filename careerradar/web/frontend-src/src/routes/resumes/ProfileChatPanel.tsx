import { useState, useRef, useEffect } from "preact/hooks";
import type {
  ProfileChatMessage,
  ProfileChatResponse,
  ResumeMasterProfile,
} from "../../api/types";

interface ProfileChatPanelProps {
  currentProfile: ResumeMasterProfile;
  resumeText: string | null;
  onUpdateProfile: (updated: ResumeMasterProfile) => void;
  onClose: () => void;
}

const QUICK_PROMPTS = [
  "Review my skills and suggest what to highlight for Platform roles",
  "Rewrite my recent project bullets to be more quantified",
  "Set my target roles to Platform Engineer & Backend Engineer",
  "Ensure my work eligibility reflects authorized US employment with no sponsorship",
];

export function ProfileChatPanel({
  currentProfile,
  resumeText,
  onUpdateProfile,
  onClose,
}: ProfileChatPanelProps) {
  const [messages, setMessages] = useState<ProfileChatMessage[]>([
    {
      role: "assistant",
      content:
        "I can help refine experience bullets, categorize skills, and update your master profile for scoring and tailored resumes. What would you like to adjust?",
    },
  ]);
  const [inputVal, setInputVal] = useState("");
  const [loading, setLoading] = useState(false);
  const [lastChanges, setLastChanges] = useState<string[]>([]);
  const chatScrollRef = useRef<HTMLDivElement>(null);

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
      const resp = await fetch("/api/profile/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: newMsgs,
          current_profile: currentProfile,
          resume_text: resumeText,
        }),
      });

      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.detail || `Chat failed with status ${resp.status}`);
      }

      const data: ProfileChatResponse = await resp.json();
      setMessages((prev) => [...prev, { role: "assistant", content: data.reply }]);

      if (data.updated_profile) {
        onUpdateProfile(data.updated_profile);
      }
      if (data.changes_made && data.changes_made.length > 0) {
        setLastChanges(data.changes_made);
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
          <i class="fa-solid fa-robot"></i> <strong>AI PROFILE COPILOT</strong>
        </div>
        <button class="action-pill text-red" onClick={onClose} title="Close Chat">
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>

      {resumeText && (
        <div class="chat-resume-badge">
          <i class="fa-solid fa-file-lines"></i> Active resume text loaded in context
        </div>
      )}

      {lastChanges.length > 0 && (
        <div class="chat-changes-banner">
          <strong>
            <i class="fa-solid fa-wand-magic-sparkles"></i> Applied Changes:
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
              <i class="fa-solid fa-spinner fa-spin"></i> Analyzing and updating profile...
            </div>
          </div>
        )}
      </div>

      <div class="chat-quick-prompts">
        <span class="quick-label">Suggestions:</span>
        <div class="quick-pills-list">
          {QUICK_PROMPTS.map((p, idx) => (
            <button
              key={idx}
              class="quick-pill"
              onClick={() => handleSendMessage(p)}
              disabled={loading}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      <div class="chat-input-row">
        <textarea
          class="chat-input"
          rows={2}
          placeholder="Ask copilot to refine your summary, rewrite bullets, adjust skills..."
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
          onClick={() => handleSendMessage()}
          disabled={loading || !inputVal.trim()}
        >
          <i class="fa-solid fa-paper-plane"></i>
        </button>
      </div>
    </div>
  );
}
