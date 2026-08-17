import { useEffect, useState } from "preact/hooks";
import { getJSON, guard } from "../../api/client";
import type { DigestContent, DigestSummary } from "../../api/types";
import { renderMarkdown } from "../../lib/markdown";

// Ported from `templates/tabs/digests.html` + `frontend/js/features/digests.js`.
export function DigestsPage() {
  const [digests, setDigests] = useState<DigestSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [loadingContent, setLoadingContent] = useState(false);

  useEffect(() => {
    let cancelled = false;
    guard("Loading digests", () => getJSON<DigestSummary[]>("/api/digests")).then((data) => {
      if (!cancelled) setDigests(data ?? []);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function selectDigest(filename: string) {
    setSelected(filename);
    setContent(null);
    setLoadingContent(true);
    const data = await guard(`Loading digest ${filename}`, () =>
      getJSON<DigestContent>(`/api/digests/${encodeURIComponent(filename)}`)
    );
    setContent(data?.content ?? null);
    setLoadingContent(false);
  }

  const selectedDigest = digests?.find((d) => d.filename === selected) ?? null;

  return (
    <section class="tab-pane active">
      <div class="feed-layout">
        <aside class="filter-sidebar">
          <div class="filter-header">
            <h3>
              <i class="fa-solid fa-envelope-open-text text-purple"></i> Daily Digests
            </h3>
          </div>
          <div class="digests-list">
            {digests === null && (
              <div class="digests-loading">
                <i class="fa-solid fa-circle-notch fa-spin text-purple"></i> Loading digests...
              </div>
            )}
            {digests?.length === 0 && (
              <div class="no-digests-text">
                No digests found yet. Run the scraper sync to generate a digest of matches!
              </div>
            )}
            {digests?.map((d) => (
              <div
                key={d.filename}
                class={`digest-list-item ${selected === d.filename ? "is-selected" : ""}`}
                onClick={() => selectDigest(d.filename)}
              >
                <div class="digest-item-icon">
                  <i class="fa-solid fa-envelope-open-text"></i>
                </div>
                <div class="digest-item-details">
                  <span class="digest-item-date">{d.date_created}</span>
                  <span class="digest-item-size">
                    {(d.size_bytes / 1024).toFixed(1)} KB • Markdown Report
                  </span>
                </div>
              </div>
            ))}
          </div>
        </aside>

        <div class="feed-main">
          <div class="glass-card digest-view-card">
            <div class="digest-view-header">
              <h2>{selectedDigest ? "Job Search Digest" : "Select a digest to begin"}</h2>
              {selectedDigest && (
                <span>
                  Generated on {selectedDigest.date_created} | File: {selectedDigest.filename}
                </span>
              )}
            </div>
            <div class="divider"></div>
            <div class="digest-view-body">
              {!selected && (
                <div class="no-digest-selected">
                  <i class="fa-solid fa-envelope"></i>
                  <h3>No Digest Selected</h3>
                  <p>Select a matching digest from the sidebar list to view the compiled jobs report.</p>
                </div>
              )}
              {selected && loadingContent && (
                <div class="digests-loading">
                  <i class="fa-solid fa-circle-notch fa-spin text-purple"></i> Loading digest content...
                </div>
              )}
              {selected && !loadingContent && content && (
                <div class="digest-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
