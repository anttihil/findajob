import { useEffect, useState } from "preact/hooks";
import { getJSON, guard } from "../../api/client";
import { useStoredPreference } from "../../lib/preferences";
import type {
  Meta,
  ObservabilityPromptResponse,
  ObservabilityReason,
  ObservabilityStats,
  ObservabilityVerdictItem,
  ObservabilityVerdictsResponse,
} from "../../api/types";

export function ObservabilityPage() {
  const [fitThreshold, setFitThreshold] = useState(70);
  const [stats, setStats] = useState<ObservabilityStats | null>(null);
  const [verdictsData, setVerdictsData] =
    useState<ObservabilityVerdictsResponse | null>(null);
  const [loadingVerdicts, setLoadingVerdicts] = useState(false);

  const [promptData, setPromptData] =
    useState<ObservabilityPromptResponse | null>(null);
  const [promptViewMode, setPromptViewMode] = useState<
    "system" | "profile" | "rules"
  >("system");
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);

  const [searchTerm, setSearchTerm] = useStoredPreference("observability-search-term", "");
  const [activeQuery, setActiveQuery] = useStoredPreference("observability-active-query", "");
  const [fitFilter, setFitFilter] = useStoredPreference<"all" | "fit" | "no_fit">("observability-fit-filter", "all");
  const [reasonFilter, setReasonFilter] = useStoredPreference<string>("observability-reason-filter", "all");
  const [sortOrder, setSortOrder] = useStoredPreference<string>("observability-sort-order", "tokens_out_desc");
  const [limit, setLimit] = useStoredPreference<number>("observability-limit", 20);
  const [offset, setOffset] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;
    guard("Loading scoring configuration", () => getJSON<Meta>("/api/meta")).then((data) => {
      if (!cancelled && data) setFitThreshold(data.fit_threshold);
    });

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
    if (fitFilter !== "all") params.set("fit", fitFilter);
    if (reasonFilter !== "all") params.set("reason_type", reasonFilter);
    params.set("sort", sortOrder);
    params.set("limit", limit.toString());
    params.set("offset", offset.toString());

    guard("Loading model verdicts", () =>
      getJSON<ObservabilityVerdictsResponse>(
        `/api/observability/verdicts?${params.toString()}`,
      ),
    ).then((data) => {
      if (!cancelled) {
        setVerdictsData(data ?? null);
        setLoadingVerdicts(false);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [activeQuery, fitFilter, reasonFilter, sortOrder, limit, offset]);

  function handleSearchSubmit(e: Event) {
    e.preventDefault();
    setOffset(0);
    setActiveQuery(searchTerm);
  }

  function handleFilterFit(val: "all" | "fit" | "no_fit") {
    setOffset(0);
    setFitFilter(val);
  }

  function handleFilterReason(val: string) {
    setOffset(0);
    setReasonFilter(val);
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
              <span class="obs-sub-tag">Expected: ~60</span>
            </div>
            <div class="obs-card-range">
              Min: {stats?.min_tokens_out ?? 0} · Max:{" "}
              {stats?.max_tokens_out ?? 0}
            </div>
          </div>
        </div>

        <div class="stat-card obs-stat-card">
          <div class="stat-icon blue">
            <i class="fa-solid fa-bolt"></i>
          </div>
          <div class="stat-info">
            <span class="stat-label">Prompt Cache Rate</span>
            <h3>
              {stats ? `${(stats.cache_hit_rate * 100).toFixed(1)}%` : "..."}
            </h3>
            <div class="obs-card-sub">
              <span>
                Cached:{" "}
                <strong>
                  {stats
                    ? (stats.total_tokens_cached / 1_000_000).toFixed(2)
                    : 0}
                  M
                </strong>{" "}
                in
              </span>
            </div>
            <div class="obs-card-range">
              Total In:{" "}
              {stats ? (stats.total_tokens_in / 1_000_000).toFixed(2) : 0}M
              tokens
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
                Avg/verdict:{" "}
                <strong>${stats ? stats.avg_cost_usd.toFixed(6) : 0}</strong>
              </span>
            </div>
            <div class="obs-card-range">
              ~${stats ? (stats.avg_cost_usd * 1000).toFixed(3) : 0} / 1k jobs
            </div>
          </div>
        </div>

        <div class="stat-card obs-stat-card">
          <div class="stat-icon green">
            <i class="fa-solid fa-check-double"></i>
          </div>
          <div class="stat-info">
            <span class="stat-label">Scored Postings</span>
            <h3>{stats ? stats.total_verdicts.toLocaleString() : "..."}</h3>
            <div class="obs-card-sub">
              <span class="text-green">
                Fit: <strong>{stats?.fit_count.toLocaleString() ?? 0}</strong>
              </span>
              <span class="text-muted">
                ({stats ? (stats.fit_rate * 100).toFixed(1) : 0}%)
              </span>
            </div>
            <div class="obs-card-range">
              No Fit: {stats?.no_fit_count.toLocaleString() ?? 0}
            </div>
          </div>
        </div>
      </div>

      <div class="obs-analytics-row">
        <div class="glass-card obs-panel">
          <div class="obs-panel-header">
            <h4>
              <i class="fa-solid fa-chart-pie text-purple"></i> Verdict Reason
              Distribution
            </h4>
            <span class="obs-panel-hint">
              Click a category to filter inspection table
            </span>
          </div>

          <div class="obs-reasons-list">
            {(!stats || stats.reasons.length === 0) && (
              <p class="text-muted">No verdicts recorded yet.</p>
            )}
            {stats?.reasons.map((r: ObservabilityReason) => {
              const isSelected = reasonFilter === r.reason_type;
              return (
                <div
                  key={r.reason_type}
                  class={`obs-reason-row ${isSelected ? "is-selected" : ""}`}
                  onClick={() =>
                    handleFilterReason(isSelected ? "all" : r.reason_type)
                  }
                >
                  <div class="obs-reason-meta">
                    <span class="obs-reason-badge">{r.reason_type}</span>
                    <span class="obs-reason-count">
                      {r.count.toLocaleString()} (
                      {(r.percentage * 100).toFixed(1)}%)
                    </span>
                    <span class="obs-reason-tokens">
                      avg {r.avg_tokens_out} tok out
                    </span>
                  </div>
                  <div class="obs-progress-bg">
                    <div
                      class="obs-progress-bar"
                      style={{ width: `${Math.max(2, r.percentage * 100)}%` }}
                    ></div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div class="glass-card obs-panel">
          <div class="obs-panel-header">
            <h4>
              <i class="fa-solid fa-gauge-high text-blue"></i> Token Accounting
              & Diagnostics
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
              <i class="fa-solid fa-code text-blue"></i> Active Scoring Prompt &
              Cached System Prefix
            </h4>
            <span class="obs-panel-hint">
              Byte-identical prompt prefix cached on DeepSeek/LLM. Paid once,
              then read from cache.
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
              <i
                class={`fa-solid ${promptExpanded ? "fa-chevron-up" : "fa-chevron-down"}`}
              ></i>{" "}
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
                  <i class="fa-solid fa-layer-group"></i> Full Cached System
                  Prompt ({displayedPromptText.length} chars)
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
                <i
                  class={`fa-solid ${copiedPrompt ? "fa-check text-green" : "fa-copy"}`}
                ></i>{" "}
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

      <div class="glass-card obs-explorer-card">
        <div class="obs-explorer-header">
          <div class="obs-explorer-title">
            <h3>
              <i class="fa-solid fa-magnifying-glass-chart text-purple"></i>{" "}
              Model Output & Token Inspector
            </h3>
            <span class="obs-explorer-subtitle">
              Inspect raw model verdicts, generated explanations, and output
              token distributions
            </span>
          </div>

          <form onSubmit={handleSearchSubmit} class="obs-search-form">
            <div class="obs-search-input-wrap">
              <i class="fa-solid fa-magnifying-glass obs-search-icon"></i>
              <input
                type="text"
                class="obs-search-input"
                placeholder="Search job title, company, or reasoning text..."
                value={searchTerm}
                onInput={(e) =>
                  setSearchTerm((e.target as HTMLInputElement).value)
                }
              />
              {searchTerm && (
                <button
                  type="button"
                  class="obs-search-clear"
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
            <span class="obs-filter-label">Fit Verdict:</span>
            <div class="obs-btn-group">
              <button
                class={`obs-filter-btn ${fitFilter === "all" ? "active" : ""}`}
                onClick={() => handleFilterFit("all")}
              >
                All
              </button>
              <button
                class={`obs-filter-btn btn-fit ${fitFilter === "fit" ? "active" : ""}`}
                onClick={() => handleFilterFit("fit")}
              >
                <i class="fa-solid fa-check"></i> Fit
              </button>
              <button
                class={`obs-filter-btn btn-no-fit ${fitFilter === "no_fit" ? "active" : ""}`}
                onClick={() => handleFilterFit("no_fit")}
              >
                <i class="fa-solid fa-xmark"></i> No Fit
              </button>
            </div>
          </div>

          <div class="obs-filter-group">
            <span class="obs-filter-label">Reason:</span>
            <select
              class="obs-select"
              value={reasonFilter}
              onChange={(e) =>
                handleFilterReason((e.target as HTMLSelectElement).value)
              }
            >
              <option value="all">
                All Reasons ({stats?.reasons.length ?? 0})
              </option>
              {stats?.reasons.map((r) => (
                <option key={r.reason_type} value={r.reason_type}>
                  {r.reason_type} ({r.count})
                </option>
              ))}
            </select>
          </div>

          <div class="obs-filter-group">
            <span class="obs-filter-label">Sort:</span>
            <select
              class="obs-select"
              value={sortOrder}
              onChange={(e) =>
                handleSortChange((e.target as HTMLSelectElement).value)
              }
            >
              <option value="tokens_out_desc">
                Outlier Tokens (Highest First)
              </option>
              <option value="tokens_out_asc">
                Fewest Tokens (Lowest First)
              </option>
              <option value="cost_desc">Highest Cost</option>
              <option value="date_desc">Most Recent Verdict</option>
              <option value="date_asc">Oldest Verdict</option>
            </select>
          </div>

          <div class="obs-filter-group">
            <span class="obs-filter-label">Page size:</span>
            <select
              class="obs-select obs-select-sm"
              value={limit}
              onChange={(e) =>
                handleLimitChange(Number((e.target as HTMLSelectElement).value))
              }
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
              <i class="fa-solid fa-circle-notch fa-spin text-purple"></i>{" "}
              Loading model inspection records...
            </div>
          )}

          {!loadingVerdicts && items.length === 0 && (
            <div class="obs-empty-state">
              <i class="fa-solid fa-inbox"></i>
              <h4>No matching verdicts found</h4>
              <p>
                Try adjusting your search query, fit filter, or reason category.
              </p>
            </div>
          )}

          {!loadingVerdicts &&
            items.map((item: ObservabilityVerdictItem) => {
              const isOutlier = item.tokens_out > 250;
              const isElevated = item.tokens_out > 140 && !isOutlier;
              const tokenClass = isOutlier
                ? "token-tag-outlier"
                : isElevated
                  ? "token-tag-elevated"
                  : "token-tag-normal";

              const cacheRate =
                item.tokens_in > 0
                  ? ((item.tokens_cached / item.tokens_in) * 100).toFixed(0)
                  : "0";

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
                        {item.location && (
                          <span class="obs-job-loc">({item.location})</span>
                        )}
                      </div>
                      <div class="obs-verdict-meta-row">
                        <span class="obs-meta-item">Job ID #{item.job_id}</span>
                        <span class="obs-meta-item">
                          Model: <code>{item.model}</code>
                        </span>
                        {item.created_at && (
                          <span class="obs-meta-item">
                            {new Date(item.created_at).toLocaleDateString(
                              "en-US",
                              {
                                month: "short",
                                day: "numeric",
                                hour: "2-digit",
                                minute: "2-digit",
                              },
                            )}
                          </span>
                        )}
                      </div>
                    </div>

                    <div class="obs-verdict-badges">
                      <span
                        class={`obs-badge-verdict ${
                          item.fit ? "is-fit" : "is-no-fit"
                        }`}
                      >
                        <i
                          class={`fa-solid ${
                            item.fit ? "fa-circle-check" : "fa-circle-xmark"
                          }`}
                        ></i>{" "}
                        {item.fit ? `FIT (≥${fitThreshold}%)` : `NO FIT (<${fitThreshold}%)`}
                      </span>
                      <span class="obs-badge-reason">{item.reason_type}</span>
                    </div>
                  </div>

                  <div class="obs-explanation-box">
                    <div class="obs-explanation-label">
                      <i class="fa-solid fa-quote-left"></i> Model Output:
                    </div>
                    <p class="obs-explanation-text">
                      {item.reason_description || (
                        <span class="text-muted italic">
                          (No explanation recorded)
                        </span>
                      )}
                    </p>
                  </div>

                  <div class="obs-verdict-footer">
                    <div class="obs-token-pills">
                      <span class={`obs-token-pill ${tokenClass}`}>
                        <i class="fa-solid fa-arrow-right-from-bracket"></i>{" "}
                        <strong>{item.tokens_out.toLocaleString()}</strong>{" "}
                        output tokens
                        {isOutlier && (
                          <span class="obs-pill-badge">Outlier / Retry</span>
                        )}
                      </span>
                      <span class="obs-token-pill pill-prompt">
                        <i class="fa-solid fa-arrow-right-to-bracket"></i>{" "}
                        {item.tokens_in.toLocaleString()} prompt in (
                        {item.tokens_cached.toLocaleString()} cached,{" "}
                        {cacheRate}%)
                      </span>
                    </div>

                    <div class="obs-cost-tag">
                      <i class="fa-solid fa-coins"></i> $
                      {item.cost_usd.toFixed(6)}
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
              {currentStart}–{currentEnd} of {totalVerdicts.toLocaleString()}{" "}
              verdicts
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
    </section>
  );
}
