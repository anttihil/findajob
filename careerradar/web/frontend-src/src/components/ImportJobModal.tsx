import { useState } from "preact/hooks";
import { postJSON } from "../api/client";

interface ImportJobModalProps {
  isOpen: boolean;
  onClose: () => void;
  onJobImported: (jobId: number) => void;
}

interface ImportResponse {
  status: string;
  job_id: number;
}

export function ImportJobModal({ isOpen, onClose, onJobImported }: ImportJobModalProps) {
  const [url, setUrl] = useState("");
  const [score, setScore] = useState(true);
  const [generateResume, setGenerateResume] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: Event) => {
    e.preventDefault();
    const cleanUrl = url.trim();
    if (!cleanUrl) return;

    setLoading(true);
    setError(null);
    try {
      const res = await postJSON<ImportResponse>("/api/jobs/import", {
        url: cleanUrl,
        score,
        generate_resume: generateResume,
      });
      if (res.job_id) {
        setUrl("");
        onClose();
        onJobImported(res.job_id);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || "Failed to import job");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div class="modal-overlay active" onClick={onClose}>
      <div
        class="modal-content glass-card"
        style={{ maxWidth: "540px", width: "90%", padding: "1.5rem" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
          <h3 style={{ margin: 0, fontSize: "1.2rem" }}>
            <i class="fa-solid fa-link text-gold" style={{ marginRight: "8px" }}></i>
            Import Job from URL
          </h3>
          <button
            type="button"
            class="close-drawer-btn"
            onClick={onClose}
            aria-label="Close"
            style={{ position: "static" }}
          >
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <p style={{ fontSize: "0.875rem", color: "var(--color-text-muted)", marginBottom: "1rem" }}>
            Paste a link to any job posting (LinkedIn, Greenhouse, Lever, Ashby, Indeed, company site).
          </p>

          <div style={{ marginBottom: "1.25rem" }}>
            <label style={{ display: "block", fontSize: "0.8rem", fontWeight: 600, marginBottom: "0.4rem" }}>
              JOB POSTING URL
            </label>
            <input
              type="url"
              class="feed-search-input"
              style={{ width: "100%", boxSizing: "border-box" }}
              placeholder="https://..."
              value={url}
              onInput={(e) => setUrl((e.target as HTMLInputElement).value)}
              required
              disabled={loading}
              autoFocus
            />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "1.25rem" }}>
            <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "0.875rem", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={score}
                onChange={(e) => setScore((e.target as HTMLInputElement).checked)}
                disabled={loading}
              />
              Score match against active profile
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "0.875rem", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={generateResume}
                onChange={(e) => setGenerateResume((e.target as HTMLInputElement).checked)}
                disabled={loading}
              />
              Generate tailored 1-page resume (DOCX + PDF)
            </label>
          </div>

          {error && (
            <div style={{ padding: "0.75rem", background: "rgba(239, 68, 68, 0.1)", border: "1px solid rgba(239, 68, 68, 0.3)", borderRadius: "6px", color: "#f87171", fontSize: "0.85rem", marginBottom: "1rem" }}>
              <i class="fa-solid fa-circle-exclamation" style={{ marginRight: "6px" }}></i>
              {error}
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.75rem" }}>
            <button
              type="button"
              class="btn btn-secondary"
              onClick={onClose}
              disabled={loading}
            >
              Cancel
            </button>
            <button
              type="submit"
              class="btn btn-primary"
              disabled={loading || !url.trim()}
            >
              {loading ? (
                <span>
                  <i class="fa-solid fa-spinner fa-spin" style={{ marginRight: "6px" }}></i>
                  Processing...
                </span>
              ) : (
                <span>
                  <i class="fa-solid fa-file-import" style={{ marginRight: "6px" }}></i>
                  Import & Process
                </span>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
