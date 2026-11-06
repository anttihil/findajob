import { useEffect, useState } from "preact/hooks";
import { getJSON, guard } from "../../api/client";
import { useStoredPreference } from "../../lib/preferences";
import type {
  ObservabilityPromptResponse,
  ObservabilityStats,
  ObservabilityVerdictItem,
  ObservabilityVerdictsResponse,
  VerdictPriority,
} from "../../api/types";

const priorities: VerdictPriority[] = ["first", "consider", "low"];
const priorityLabels = {
  first: "Review first",
  consider: "Worth considering",
  low: "Low priority",
};
const priorityIcons = { first: "fa-flag", consider: "fa-circle-info", low: "fa-arrow-down" };

export function ObservabilityPage() {
  const [stats, setStats] = useState<ObservabilityStats | null>(null);
  const [verdictsData, setVerdictsData] = useState<ObservabilityVerdictsResponse | null>(null);
  const [loadingVerdicts, setLoadingVerdicts] = useState(false);

  const [promptData, setPromptData] = useState<ObservabilityPromptResponse | null>(null);
  const [promptViewMode, setPromptViewMode] = useState<"system" | "profile" | "rules">("system");
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);

  const [searchTerm, setSearchTerm] = useStoredPreference("observability-search-term", "");
  const [activeQuery, setActiveQuery] = useStoredPreference("observability-active-query", "");
  const [priorityFilter, setPriorityFilter] = useStoredPreference<"all" | VerdictPriority>(
    "observability-priority-filter",
    "all",
  );
  const [sortOrder, setSortOrder] = useStoredPreference<string>(
    "observability-sort-order-v2",
    "date_desc",
  );
  const [limit, setLimit] = useStoredPreference<number>("observability-limit", 20);
  const [offset, setOffset] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;
    guard("Loading observability stats", () =>
      getJSON<ObservabilityStats>("/api/observability/stats"),
    ).then((data) => {
      if (!cancelled && data) {
        setStats(data);
      }
    });

    guard("Loading scoring prompt prefix", () =>
      getJSON<ObservabilityPromptResponse>("/api/observability/prompt"),
    ).then((data) => {
      if (!cancelled && data) {
        setPromptData(data);
      }
    });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoadingVerdicts(true);

    const params = new URLSearchParams();
    if (activeQuery) params.set("q", activeQuery);
    if (priorityFilter !== "all") params.set("priority", priorityFilter);
    params.set("sort", sortOrder);
    params.set("limit", limit.toString());
    params.set("offset", offset.toString());

    guard("Loading model verdicts", () =>
      getJSON<ObservabilityVerdictsResponse>(`/api/observability/verdicts?${params.toString()}`),
    ).then((data) => {
      if (!cancelled) {
        setVerdictsData(data ?? null);
        setLoadingVerdicts(false);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [activeQuery, priorityFilter, sortOrder, limit, offset]);

  function handleSearchSubmit(e: Event) {
    e.preventDefault();
    setOffset(0);
    setActiveQuery(searchTerm);
  }

  function handleFilterPriority(val: "all" | VerdictPriority) {
    setOffset(0);
    setPriorityFilter(val);
  }

  function handleSortChange(val: string) {
    setOffset(0);
    setSortOrder(val);
  }

  function handleLimitChange(val: number) {
    setOffset(0);
    setLimit(val);
  }

  function handleCopyPrompt(text: string) {
    if (!text) return;
    navigator.clipboard.writeText(text);
    setCopiedPrompt(true);
    setTimeout(() => setCopiedPrompt(false), 2500);
  }

  const displayedPromptText =
    promptViewMode === "system"
      ? promptData?.system_prompt || ""
      : promptViewMode === "profile"
        ? promptData?.summary_text || ""
        : promptData?.rules || "";

  const totalVerdicts = verdictsData?.total ?? 0;
  const items = verdictsData?.items ?? [];
  const currentStart = totalVerdicts > 0 ? offset + 1 : 0;
  const currentEnd = Math.min(offset + limit, totalVerdicts);

  return (
    <section class="tab-pane active observability-page">
      <div class="obs-page-intro">
        <h2>Priority verdict review</h2>
        <p>Compare each priority with its reason to check whether they agree.</p>
      </div>
      <div class="stats-grid obs-stats-grid" aria-label="Priority verdict summary">
        <button
          class={`stat-card obs-stat-card obs-summary-card ${priorityFilter === "all" ? "active" : ""}`}
          aria-pressed={priorityFilter === "all"}
          onClick={() => handleFilterPriority("all")}
        >
          <span class="stat-label">All priority verdicts</span>
          <strong class="obs-summary-count">
            {stats ? stats.total_verdicts.toLocaleString() : "..."}
          </strong>
        </button>
        {priorities.map((priority) => {
          const summary = stats?.priorities.find((item) => item.priority === priority);
          return (
            <button
              key={priority}
              class={`stat-card obs-stat-card obs-summary-card ${priorityFilter === priority ? "active" : ""}`}
              aria-pressed={priorityFilter === priority}
              onClick={() => handleFilterPriority(priorityFilter === priority ? "all" : priority)}
            >
              <span class="stat-label">{priorityLabels[priority]}</span>
              <strong class="obs-summary-count">
                {stats ? (summary?.count ?? 0).toLocaleString() : "..."}
              </strong>
              <span>{((summary?.percentage ?? 0) * 100).toFixed(1)}%</span>
            </button>
          );
        })}
      </div>

      <div class="glass-card obs-explorer-card">
        <div class="obs-explorer-header">
          <div class="obs-explorer-title">
            <h3>
              <i class="fa-solid fa-magnifying-glass-chart text-purple"></i> Verdicts and reasons
            </h3>
            <span class="obs-explorer-subtitle">
              Priority verdicts only · newest first by default
            </span>
          </div>

          <form onSubmit={handleSearchSubmit} class="obs-search-form">
            <div class="obs-search-input-wrap">
              <i class="fa-solid fa-magnifying-glass obs-search-icon"></i>
              <input
                type="text"
                aria-label="Search verdicts"
                class="obs-search-input"
                placeholder="Search job title, company, or reason..."
                value={searchTerm}
                onInput={(e) => setSearchTerm((e.target as HTMLInputElement).value)}
              />
              {searchTerm && (
                <button
                  type="button"
                  class="obs-search-clear"
                  aria-label="Clear search"
                  onClick={() => {
                    setSearchTerm("");
                    setActiveQuery("");
                    setOffset(0);
                  }}
                >
                  <i class="fa-solid fa-xmark"></i>
                </button>
              )}
            </div>
            <button type="submit" class="primary-btn obs-search-btn">
              Search
            </button>
          </form>
        </div>

        <div class="obs-controls-bar">
          <div class="obs-filter-group">
            <label class="obs-filter-label" for="obs-priority">
              Priority:
            </label>
            <select
              id="obs-priority"
              class="obs-select"
              value={priorityFilter}
              onChange={(e) =>
                handleFilterPriority(
                  (e.target as HTMLSelectElement).value as "all" | VerdictPriority,
                )
              }
            >
              <option value="all">All priorities</option>
              {priorities.map((priority) => (
                <option key={priority} value={priority}>
                  {priorityLabels[priority]}
                </option>
              ))}
            </select>
          </div>

          <div class="obs-filter-group">
            <label class="obs-filter-label" for="obs-sort">
              Sort:
            </label>
            <select
              class="obs-select"
              id="obs-sort"
              value={sortOrder}
              onChange={(e) => handleSortChange((e.target as HTMLSelectElement).value)}
            >
              <option value="tokens_out_desc">Output Tokens (Highest First)</option>
              <option value="tokens_out_asc">Fewest Tokens (Lowest First)</option>
              <option value="cost_desc">Highest Cost</option>
              <option value="date_desc">Most Recent Verdict</option>
              <option value="date_asc">Oldest Verdict</option>
            </select>
          </div>

          <div class="obs-filter-group">
            <label class="obs-filter-label" for="obs-limit">
              Page size:
            </label>
            <select
              class="obs-select obs-select-sm"
              id="obs-limit"
              value={limit}
              onChange={(e) => handleLimitChange(Number((e.target as HTMLSelectElement).value))}
            >
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
            </select>
          </div>
        </div>

        <div class="obs-verdicts-container">
          {loadingVerdicts && (
            <div class="obs-loading-state">
              <i class="fa-solid fa-circle-notch fa-spin text-purple"></i> Loading model inspection
              records...
            </div>
          )}

          {!loadingVerdicts && items.length === 0 && (
            <div class="obs-empty-state">
              <i class="fa-solid fa-inbox"></i>
              <h4>No matching verdicts found</h4>
              <p>
                Try adjusting your search or priority filter. Only priority verdicts appear here.
              </p>
            </div>
          )}

          {!loadingVerdicts &&
            items.map((item: ObservabilityVerdictItem) => {
              const cacheRate =
                item.tokens_in > 0 ? ((item.tokens_cached / item.tokens_in) * 100).toFixed(0) : "0";

              return (
                <div key={item.id} class="obs-verdict-card">
                  <div class="obs-verdict-top">
                    <div class="obs-verdict-job-info">
                      <div class="obs-verdict-title-row">
                        {item.url ? (
                          <a
                            href={item.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            class="obs-job-title-link"
                          >
                            {item.title}{" "}
                            <i class="fa-solid fa-arrow-up-right-from-square obs-link-icon"></i>
                          </a>
                        ) : (
                          <span class="obs-job-title">{item.title}</span>
                        )}
                        <span class="obs-job-company">· {item.company}</span>
                        {item.location && <span class="obs-job-loc">({item.location})</span>}
                      </div>
                      <div class="obs-verdict-meta-row">
                        <span class="obs-meta-item">Job ID #{item.job_id}</span>
                        <span class="obs-meta-item">
                          Model: <code>{item.model}</code>
                        </span>
                        {item.created_at && (
                          <span class="obs-meta-item">
                            {new Date(item.created_at).toLocaleDateString("en-US", {
                              month: "short",
                              day: "numeric",
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </span>
                        )}
                      </div>
                    </div>

                    <div class="obs-verdict-badges">
                      <span class={`obs-badge-verdict priority-${item.priority}`}>
                        <i class={`fa-solid ${priorityIcons[item.priority]}`}></i>{" "}
                        {priorityLabels[item.priority]}
                      </span>
                    </div>
                  </div>

                  <div class="obs-explanation-box">
                    <div class="obs-explanation-label">
                      <i class="fa-solid fa-quote-left"></i> Reason:
                    </div>
                    <p class="obs-explanation-text">
                      {item.reason_description.trim() || (
                        <span class="text-muted italic">No reason recorded</span>
                      )}
                    </p>
                  </div>

                  <div class="obs-verdict-footer">
                    <div class="obs-token-pills">
                      <span class="obs-token-pill">
                        <i class="fa-solid fa-arrow-right-from-bracket"></i>{" "}
                        <strong>{item.tokens_out.toLocaleString()}</strong> output tokens
                      </span>
                      <span class="obs-token-pill pill-prompt">
                        <i class="fa-solid fa-arrow-right-to-bracket"></i>{" "}
                        {item.tokens_in.toLocaleString()} prompt in (
                        {item.tokens_cached.toLocaleString()} cached, {cacheRate}%)
                      </span>
                    </div>

                    <div class="obs-cost-tag">
                      <i class="fa-solid fa-coins"></i> ${item.cost_usd.toFixed(6)}
                    </div>
                  </div>
                </div>
              );
            })}
        </div>

        {totalVerdicts > 0 && (
          <nav class="feed-pagination obs-pagination">
            <button
              class="text-btn"
              disabled={offset <= 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              <i class="fa-solid fa-chevron-left"></i> Previous
            </button>
            <span class="results-count">
              {currentStart}–{currentEnd} of {totalVerdicts.toLocaleString()} verdicts
            </span>
            <button
              class="text-btn"
              disabled={offset + limit >= totalVerdicts}
              onClick={() => setOffset(offset + limit)}
            >
              Next <i class="fa-solid fa-chevron-right"></i>
            </button>
          </nav>
        )}
      </div>
      <section class="obs-diagnostics" aria-label="Model diagnostics">
        <h3>Model diagnostics</h3>
        <div class="stats-grid obs-stats-grid">
          <div class="stat-card obs-stat-card">
            <div class="stat-icon purple">
              <i class="fa-solid fa-microchip"></i>
            </div>
            <div class="stat-info">
              <span class="stat-label">Output Tokens</span>
              <h3>{stats ? stats.total_tokens_out.toLocaleString() : "..."}</h3>
              <div class="obs-card-sub">
                <span>
                  Avg: <strong>{stats?.avg_tokens_out ?? 0}</strong> tok/job
                </span>
              </div>
              <div class="obs-card-range">
                Min: {stats?.min_tokens_out ?? 0} · Max: {stats?.max_tokens_out ?? 0}
              </div>
            </div>
          </div>

          <div class="stat-card obs-stat-card">
            <div class="stat-icon blue">
              <i class="fa-solid fa-bolt"></i>
            </div>
            <div class="stat-info">
              <span class="stat-label">Prompt Cache Rate</span>
              <h3>{stats ? `${(stats.cache_hit_rate * 100).toFixed(1)}%` : "..."}</h3>
              <div class="obs-card-sub">
                <span>
                  Cached:{" "}
                  <strong>{stats ? (stats.total_tokens_cached / 1_000_000).toFixed(2) : 0}M</strong>{" "}
                  in
                </span>
              </div>
              <div class="obs-card-range">
                Total In: {stats ? (stats.total_tokens_in / 1_000_000).toFixed(2) : 0}M tokens
              </div>
            </div>
          </div>

          <div class="stat-card obs-stat-card">
            <div class="stat-icon gold">
              <i class="fa-solid fa-dollar-sign"></i>
            </div>
            <div class="stat-info">
              <span class="stat-label">Model Spend (USD)</span>
              <h3>${stats ? stats.total_cost_usd.toFixed(4) : "..."}</h3>
              <div class="obs-card-sub">
                <span>
                  Avg/verdict: <strong>${stats ? stats.avg_cost_usd.toFixed(6) : 0}</strong>
                </span>
              </div>
              <div class="obs-card-range">
                ~${stats ? (stats.avg_cost_usd * 1000).toFixed(3) : 0} / 1k jobs
              </div>
            </div>
          </div>
        </div>

        <div class="glass-card obs-panel">
          <div class="obs-panel-header">
            <h4>
              <i class="fa-solid fa-gauge-high text-blue"></i> Token Accounting & Diagnostics
            </h4>
          </div>

          <div class="obs-diagnostics-content">
            {stats && stats.models.length > 0 && (
              <div class="obs-models-table-wrap">
                <table class="obs-table">
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th>Verdicts</th>
                      <th>Avg Tokens Out</th>
                      <th>Total Spend</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stats.models.map((m) => (
                      <tr key={m.model}>
                        <td>
                          <code>{m.model}</code>
                        </td>
                        <td>{m.count.toLocaleString()}</td>
                        <td>
                          <strong>{m.avg_tokens_out}</strong> tok
                        </td>
                        <td>${m.total_cost_usd.toFixed(4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
        <div class="glass-card obs-panel" style={{ marginBottom: "18px" }}>
          <div
            class="obs-panel-header"
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              flexWrap: "wrap",
              gap: "0.75rem",
            }}
          >
            <div>
              <h4
                style={{
                  margin: 0,
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                }}
              >
                <i class="fa-solid fa-code text-blue"></i> Active Scoring Prompt & Cached System
                Prefix
              </h4>
              <span class="obs-panel-hint">
                Byte-identical prompt prefix cached on DeepSeek/LLM. Paid once, then read from
                cache.
              </span>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              {promptData?.prompt_hash && (
                <span
                  style={{
                    fontSize: "0.75rem",
                    fontFamily: "var(--font-mono)",
                    padding: "0.2rem 0.5rem",
                    border: "1px solid var(--ink-primary)",
                    backgroundColor: "var(--bg-screen-alt)",
                  }}
                >
                  hash: <strong>{promptData.prompt_hash}</strong>
                </span>
              )}
              <button
                class="action-pill"
                onClick={() => setPromptExpanded(!promptExpanded)}
                style={{ fontSize: "0.8rem", padding: "0.25rem 0.6rem" }}
              >
                <i class={`fa-solid ${promptExpanded ? "fa-chevron-up" : "fa-chevron-down"}`}></i>{" "}
                {promptExpanded ? "Collapse" : "Inspect Prompt"}
              </button>
            </div>
          </div>

          {promptExpanded && (
            <div style={{ marginTop: "1rem" }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "0.75rem",
                  flexWrap: "wrap",
                  gap: "0.5rem",
                }}
              >
                <div class="subtab-bar" style={{ gap: "0.35rem" }}>
                  <button
                    class={`action-pill ${promptViewMode === "system" ? "active" : ""}`}
                    onClick={() => setPromptViewMode("system")}
                    style={{ fontSize: "0.8rem", padding: "0.25rem 0.6rem" }}
                  >
                    <i class="fa-solid fa-layer-group"></i> Full Cached System Prompt (
                    {displayedPromptText.length} chars)
                  </button>
                  <button
                    class={`action-pill ${promptViewMode === "profile" ? "active" : ""}`}
                    onClick={() => setPromptViewMode("profile")}
                    style={{ fontSize: "0.8rem", padding: "0.25rem 0.6rem" }}
                  >
                    <i class="fa-solid fa-user"></i> Candidate Profile Prefix
                  </button>
                  <button
                    class={`action-pill ${promptViewMode === "rules" ? "active" : ""}`}
                    onClick={() => setPromptViewMode("rules")}
                    style={{ fontSize: "0.8rem", padding: "0.25rem 0.6rem" }}
                  >
                    <i class="fa-solid fa-gavel"></i> Scoring Rules
                  </button>
                </div>

                <button
                  class="action-pill text-blue"
                  onClick={() => handleCopyPrompt(displayedPromptText)}
                  disabled={!displayedPromptText}
                  style={{ fontSize: "0.8rem", padding: "0.25rem 0.6rem" }}
                >
                  <i class={`fa-solid ${copiedPrompt ? "fa-check text-green" : "fa-copy"}`}></i>{" "}
                  {copiedPrompt ? "Copied!" : "Copy Text"}
                </button>
              </div>

              <pre
                style={{
                  backgroundColor: "var(--bg-screen-alt)",
                  padding: "1rem",
                  fontSize: "0.8rem",
                  fontFamily: "var(--font-mono)",
                  whiteSpace: "pre-wrap",
                  border: "1px solid var(--ink-primary)",
                  maxHeight: "350px",
                  overflowY: "auto",
                  lineHeight: 1.45,
                  margin: 0,
                }}
              >
                {displayedPromptText || "No active prompt loaded."}
              </pre>
            </div>
          )}
        </div>
      </section>
    </section>
  );
}
