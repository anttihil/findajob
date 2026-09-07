import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { getJSON, guard, putJSON, reportError } from "../../api/client";
import type {
  CoverageCell,
  MarketCoverageResponse,
  MarketLocationsResponse,
  MarketYieldResponse,
} from "../../api/types";
import { renderCoverageStrip, renderYieldBarChart } from "../../charts/charts";

function CoverageTable({ cells }: { cells: CoverageCell[] | null }) {
  if (cells === null) return null;
  const scraped = cells.filter((c) => c.total_scrapes > 0);
  const rows = [...scraped]
    .sort((a, b) => (b.hours_since_success ?? 1e6) - (a.hours_since_success ?? 1e6))
    .slice(0, 40);

  return (
    <>
      {rows.length === 0 ? (
        <p class="chart-empty">No cells scraped yet.</p>
      ) : (
        <table class="data-table">
          <thead>
            <tr>
              <th>Source</th>
              <th>Location</th>
              <th>Query</th>
              <th>Tier</th>
              <th>Last success</th>
              <th>Returned</th>
              <th>Scrapes</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c, i) => {
              const hrs = c.hours_since_success;
              const staleCell = (hrs ?? 1e6) > 96;
              let state = "ok";
              if (c.backoff_until) state = "backoff";
              else if (c.consecutive_error > 0) state = `${c.consecutive_error} errors`;
              else if (c.consecutive_empty > 2) state = `${c.consecutive_empty} empty`;
              else if (c.last_saturated) state = "truncated";
              return (
                <tr key={i} class={staleCell ? "row-warn" : ""}>
                  <td>{c.source}</td>
                  <td>{c.location_id}</td>
                  <td>{c.query}</td>
                  <td>{c.tier}</td>
                  <td>{hrs === null ? "never" : `${hrs.toFixed(0)}h ago`}</td>
                  <td>{c.last_result_count ?? 0}</td>
                  <td>{c.total_scrapes}</td>
                  <td>{state}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

function YieldBadge({ category, scored }: { category: string; scored: number }) {
  if (category === "high_yield") {
    return <span class="yield-badge yield-badge-high">High Yield</span>;
  }
  if (category === "moderate_yield") {
    return <span class="yield-badge yield-badge-moderate">Moderate</span>;
  }
  if (category === "zero_yield") {
    return <span class="yield-badge yield-badge-zero">Zero Fits ({scored} scored)</span>;
  }
  if (category === "low_yield") {
    return <span class="yield-badge yield-badge-zero">Low Yield</span>;
  }
  if (category === "unscored") {
    return <span class="yield-badge yield-badge-unscored">Pending Scoring</span>;
  }
  return <span class="yield-badge yield-badge-unscored">Unscraped</span>;
}

export function MarketPage() {
  const [yieldData, setYieldData] = useState<MarketYieldResponse | null>(null);
  const [locations, setLocations] = useState<MarketLocationsResponse | null>(null);
  const [coverageCells, setCoverageCells] = useState<CoverageCell[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [showCoverage, setShowCoverage] = useState(false);

  // Filters state
  const [viewMode, setViewMode] = useState<"query" | "tuple">("query");
  const [windowDays, setWindowDays] = useState<number | null>(null);
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [locationFilter, setLocationFilter] = useState<string>("all");
  const [queryFilter, setQueryFilter] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [togglingQueryId, setTogglingQueryId] = useState<number | null>(null);

  const chartRef = useRef<HTMLDivElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);

  // Load locations metadata on mount
  useEffect(() => {
    getJSON<MarketLocationsResponse>("/api/market/locations")
      .then(setLocations)
      .catch(() => setLocations({ locations: [], queries: [] }));
  }, []);

  // Load coverage on mount
  useEffect(() => {
    getJSON<MarketCoverageResponse>("/api/market/coverage")
      .then((data) => setCoverageCells(data.cells))
      .catch(() => setCoverageCells([]));
  }, []);

  // Fetch yield analytics data when filters change
  const fetchYieldData = () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (windowDays) params.set("window_days", String(windowDays));
    if (sourceFilter !== "all") params.set("source", sourceFilter);
    if (locationFilter !== "all") params.set("location", locationFilter);
    if (queryFilter !== "all") params.set("query", queryFilter);

    guard("Loading query yield analytics", () =>
      getJSON<MarketYieldResponse>(`/api/market/yield?${params.toString()}`)
    )
      .then((data) => {
        if (data) setYieldData(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  };

  useEffect(() => {
    fetchYieldData();
  }, [windowDays, sourceFilter, locationFilter, queryFilter]);

  // Render Yield Bar Chart
  useEffect(() => {
    if (!chartRef.current || !yieldData) return;

    const topQueries = yieldData.top_queries.slice(0, 15);
    if (topQueries.length === 0) {
      chartRef.current.innerHTML = '<p class="chart-empty">No query yield data available for current filters.</p>';
      return;
    }

    renderYieldBarChart(
      chartRef.current,
      topQueries.map((q) => ({
        label: q.query,
        totalPostings: q.total_postings,
        scoredPostings: q.scored_postings,
        strongFits: q.strong_fits,
        fitRatePct: q.fit_rate_pct,
        yieldCategory: q.yield_category,
      })),
      {
        labelWidth: 190,
        emptyText: "No yield data to render.",
        ariaLabel: "Query term strong fit yield chart",
      }
    );
  }, [yieldData]);

  // Render Provenance Strip
  useEffect(() => {
    if (!stripRef.current || !yieldData) return;
    const s = yieldData.summary;
    renderCoverageStrip(stripRef.current, [
      { label: "postings", value: s.total_postings.toLocaleString() },
      { label: "unique", value: s.total_unique_postings.toLocaleString() },
      { label: "scored", value: s.total_scored.toLocaleString() },
      { label: "strong fits", value: `${s.total_strong_fits} (${s.overall_fit_rate_pct}%)` },
      { label: "window", value: yieldData.window_days ? `${yieldData.window_days}d` : "all time" },
    ]);
  }, [yieldData]);

  // Toggle Query enabled/disabled status
  const handleToggleQuery = async (queryId: number) => {
    setTogglingQueryId(queryId);
    try {
      await putJSON(`/api/targets/queries/${queryId}/toggle`, {});
      fetchYieldData();
    } catch (e) {
      reportError("Failed to toggle target query", e);
    } finally {
      setTogglingQueryId(null);
    }
  };

  // Filtered queries for the table
  const filteredQueries = useMemo(() => {
    if (!yieldData) return [];
    return yieldData.top_queries.filter((q) => {
      if (searchQuery.trim()) {
        const needle = searchQuery.toLowerCase();
        if (!q.query.toLowerCase().includes(needle)) return false;
      }
      if (categoryFilter !== "all" && q.yield_category !== categoryFilter) {
        return false;
      }
      return true;
    });
  }, [yieldData, searchQuery, categoryFilter]);

  // Filtered tuples for the table
  const filteredTuples = useMemo(() => {
    if (!yieldData) return [];
    return yieldData.tuples.filter((t) => {
      if (searchQuery.trim()) {
        const needle = searchQuery.toLowerCase();
        const matchesQuery = t.query.toLowerCase().includes(needle);
        const matchesLoc = t.location_id.toLowerCase().includes(needle);
        const matchesSource = t.source.toLowerCase().includes(needle);
        if (!matchesQuery && !matchesLoc && !matchesSource) return false;
      }
      if (categoryFilter !== "all" && t.yield_category !== categoryFilter) {
        return false;
      }
      return true;
    });
  }, [yieldData, searchQuery, categoryFilter]);

  const summary = yieldData?.summary;

  return (
    <section class="tab-pane active market-yield-page">
      {/* Header */}
      <div class="glass-card">
        <div class="card-header-row">
          <div>
            <h3>
              <i class="fa-solid fa-crosshairs text-purple"></i> Query Yield & Search Optimization
            </h3>
            <p class="card-note">
              Tracking posting volume and scoring agent strong fits across <strong>(job source, query term, location)</strong> search tuples to identify high-yield queries and prune unproductive ones over time.
            </p>
          </div>
          <a href="/settings" class="yield-action-btn" title="Manage Target Queries">
            <i class="fa-solid fa-sliders"></i> Edit Search Targets
          </a>
        </div>

        {/* Top Summary Stat Grid */}
        <div class="stats-grid obs-stats-grid" style="margin-top: 14px;">
          <div class="stat-card obs-stat-card">
            <div class="stat-info">
              <span class="stat-label">Total Postings Found</span>
              <h3>{summary ? summary.total_postings.toLocaleString() : "..."}</h3>
              <div class="obs-card-sub">
                <span>Unique: <strong>{summary?.total_unique_postings.toLocaleString() ?? "..."}</strong></span>
              </div>
            </div>
          </div>

          <div class="stat-card obs-stat-card">
            <div class="stat-info">
              <span class="stat-label">Evaluated by Scorer</span>
              <h3>{summary ? summary.total_scored.toLocaleString() : "..."}</h3>
              <div class="obs-card-sub">
                <span>Coverage: <strong>{summary && summary.total_postings ? Math.round((summary.total_scored / summary.total_postings) * 100) : 0}%</strong></span>
              </div>
            </div>
          </div>

          <div class="stat-card obs-stat-card">
            <div class="stat-info">
              <span class="stat-label">Strong Fits (fit = 1)</span>
              <h3>{summary ? summary.total_strong_fits.toLocaleString() : "..."}</h3>
              <div class="obs-card-sub">
                <span>Overall Fit Rate: <strong>{summary?.overall_fit_rate_pct ?? 0}%</strong></span>
              </div>
            </div>
          </div>

          <div class="stat-card obs-stat-card">
            <div class="stat-info">
              <span class="stat-label">Query Efficiency</span>
              <h3>{summary ? `${summary.high_yield_queries_count} High Yield` : "..."}</h3>
              <div class="obs-card-sub">
                <span style="color: var(--ink-muted);">{summary?.zero_yield_queries_count ?? 0} zero-yield candidates</span>
              </div>
            </div>
          </div>
        </div>

        {/* Provenance strip */}
        <div class="coverage-strip" ref={stripRef}></div>
      </div>

      {/* Filter Toolbar */}
      <div class="glass-card">
        <div class="yield-filter-bar">
          {/* View Mode Toggle */}
          <div class="yield-view-toggle">
            <button
              type="button"
              class={`yield-toggle-btn ${viewMode === "query" ? "is-active" : ""}`}
              onClick={() => setViewMode("query")}
            >
              <i class="fa-solid fa-list-ul"></i> By Query Term
            </button>
            <button
              type="button"
              class={`yield-toggle-btn ${viewMode === "tuple" ? "is-active" : ""}`}
              onClick={() => setViewMode("tuple")}
            >
              <i class="fa-solid fa-table-cells"></i> By Search Tuple
            </button>
          </div>

          {/* Time Window */}
          <div class="yield-filter-item">
            <label>Window:</label>
            <select
              class="form-select-sm"
              value={windowDays === null ? "all" : String(windowDays)}
              onChange={(e) => {
                const val = (e.target as HTMLSelectElement).value;
                setWindowDays(val === "all" ? null : Number(val));
              }}
            >
              <option value="all">All Time</option>
              <option value="90">Last 90 days</option>
              <option value="30">Last 30 days</option>
              <option value="14">Last 14 days</option>
            </select>
          </div>

          {/* Job Source */}
          <div class="yield-filter-item">
            <label>Source:</label>
            <select
              class="form-select-sm"
              value={sourceFilter}
              onChange={(e) => setSourceFilter((e.target as HTMLSelectElement).value)}
            >
              <option value="all">All Sources</option>
              <option value="indeed">Indeed</option>
              <option value="linkedin">LinkedIn</option>
            </select>
          </div>

          {/* Location */}
          <div class="yield-filter-item">
            <label>Location:</label>
            <select
              class="form-select-sm"
              value={locationFilter}
              onChange={(e) => setLocationFilter((e.target as HTMLSelectElement).value)}
            >
              <option value="all">All Locations</option>
              {locations?.locations.map((loc) => (
                <option key={loc.id} value={loc.id}>
                  {loc.label}
                </option>
              ))}
            </select>
          </div>

          {/* Query Term */}
          <div class="yield-filter-item">
            <label>Query:</label>
            <select
              class="form-select-sm"
              value={queryFilter}
              onChange={(e) => setQueryFilter((e.target as HTMLSelectElement).value)}
            >
              <option value="all">All Queries</option>
              {locations?.queries.map((q) => (
                <option key={q} value={q}>
                  {q}
                </option>
              ))}
            </select>
          </div>

          {/* Category Filter */}
          <div class="yield-filter-item">
            <label>Yield Tier:</label>
            <select
              class="form-select-sm"
              value={categoryFilter}
              onChange={(e) => setCategoryFilter((e.target as HTMLSelectElement).value)}
            >
              <option value="all">All Tiers</option>
              <option value="high_yield">High Yield (≥2 fits or ≥25%)</option>
              <option value="moderate_yield">Moderate Yield (≥1 fit)</option>
              <option value="zero_yield">Zero Yield (0 fits, ≥3 scored)</option>
            </select>
          </div>

          {/* Text Search Filter */}
          <div class="yield-filter-item" style="flex: 1; min-width: 160px;">
            <input
              type="text"
              class="form-control-sm"
              placeholder="Search query term..."
              value={searchQuery}
              onInput={(e) => setSearchQuery((e.target as HTMLInputElement).value)}
              style="width: 100%;"
            />
          </div>
        </div>

        {/* Visual Charts Row */}
        <div class="yield-grid-2">
          {/* Left Chart: Top High-Yield Query Terms */}
          <div>
            <div class="card-header-row" style="margin-bottom: 8px;">
              <h4>
                <i class="fa-solid fa-chart-bar text-purple"></i> Top Query Terms by Strong Fit Yield
              </h4>
              <span class="card-note" style="margin: 0;">
                Solid bar = <strong>Strong Fits</strong> · Light track = <strong>Total Postings</strong>
              </span>
            </div>
            <div ref={chartRef}></div>
          </div>

          {/* Right Cards: Distribution by Source & Location */}
          <div>
            <div class="card-header-row" style="margin-bottom: 8px;">
              <h4>
                <i class="fa-solid fa-chart-pie text-purple"></i> Strong Fits Distribution
              </h4>
            </div>

            {/* By Source */}
            <div style="margin-bottom: 14px;">
              <span class="stat-label" style="font-size: 11px;">BY JOB SOURCE</span>
              <div class="yield-dist-list">
                {yieldData?.by_source.map((s) => (
                  <div key={s.source} class="yield-dist-row">
                    <span class="yield-dist-label" style="text-transform: capitalize;">{s.source}</span>
                    <span class="yield-dist-val">
                      {s.strong_fits} fits ({s.fit_rate_pct}%) · {s.total_postings} posts
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* By Location */}
            <div>
              <span class="stat-label" style="font-size: 11px;">BY TARGET LOCATION</span>
              <div class="yield-dist-list">
                {yieldData?.by_location.slice(0, 6).map((l) => (
                  <div key={l.location_id} class="yield-dist-row">
                    <span class="yield-dist-label">{l.location_label}</span>
                    <span class="yield-dist-val">
                      {l.strong_fits} fits ({l.fit_rate_pct}%) · {l.total_postings} posts
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Zero-Yield / Pruning Candidates Callout */}
        {yieldData && yieldData.zero_yield_queries.length > 0 && (
          <div class="yield-prune-callout">
            <div class="yield-prune-head">
              <h4>
                <i class="fa-solid fa-triangle-exclamation"></i> Low Yield / Pruning Candidates
              </h4>
              <a href="/settings" class="yield-action-btn">
                <i class="fa-solid fa-sliders"></i> Edit in Search Targets
              </a>
            </div>
            <p class="card-note" style="margin-bottom: 6px;">
              These queries returned postings and consumed LLM scoring budget but yielded <strong>0 strong fits</strong>. Consider disabling or narrowing their phrasing:
            </p>
            <div class="yield-prune-tags">
              {yieldData.zero_yield_queries.slice(0, 10).map((zq) => (
                <span key={zq.query} class="yield-prune-tag">
                  <strong>{zq.query}</strong>
                  <span class="yield-tag-badge">{zq.scored_postings} scored · 0 fits</span>
                  {zq.target_query_id && (
                    <button
                      type="button"
                      class="yield-action-btn"
                      disabled={togglingQueryId === zq.target_query_id}
                      onClick={() => handleToggleQuery(zq.target_query_id!)}
                      style="padding: 1px 5px; font-size: 10px;"
                    >
                      {togglingQueryId === zq.target_query_id ? "..." : "Disable"}
                    </button>
                  )}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Main Data Table */}
        <div style="margin-top: 18px;">
          <div class="card-header-row" style="margin-bottom: 10px;">
            <h4>
              <i class="fa-solid fa-table text-purple"></i> {viewMode === "query" ? "Query Terms Yield Matrix" : "Search Tuple Yield Matrix (Source × Query × Location)"}
            </h4>
            <span class="results-count">
              Showing {viewMode === "query" ? filteredQueries.length : filteredTuples.length} entries
            </span>
          </div>

          {loading ? (
            <p class="chart-empty">Loading query yield analytics…</p>
          ) : viewMode === "query" ? (
            /* Grouped by Query Term Table */
            <div class="table-responsive">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>Query Term</th>
                    <th>Sources</th>
                    <th>Locations</th>
                    <th style="text-align: right;">Postings</th>
                    <th style="text-align: right;">Scored</th>
                    <th style="text-align: right;">Strong Fits</th>
                    <th style="text-align: right;">Fit Rate</th>
                    <th>Yield Tier</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredQueries.length === 0 ? (
                    <tr>
                      <td colSpan={9} class="text-center">No query terms match the current filters.</td>
                    </tr>
                  ) : (
                    filteredQueries.map((q) => (
                      <tr key={q.query} class={q.yield_category === "high_yield" ? "row-highlight" : ""}>
                        <td>
                          <strong>{q.query}</strong>
                        </td>
                        <td>
                          <span style="font-size: 11.5px;">{q.sources.join(", ") || "—"}</span>
                        </td>
                        <td>
                          <span style="font-size: 11.5px;">{q.locations.length} locations</span>
                        </td>
                        <td style="text-align: right;">
                          <strong>{q.total_postings}</strong>
                          {q.unique_postings !== q.total_postings && (
                            <span style="font-size: 10.5px; color: var(--ink-muted);"> ({q.unique_postings}u)</span>
                          )}
                        </td>
                        <td style="text-align: right;">{q.scored_postings}</td>
                        <td style="text-align: right; font-weight: 800;">
                          {q.strong_fits > 0 ? (
                            <span style="border-bottom: 2px solid var(--ink-primary);">{q.strong_fits}</span>
                          ) : (
                            <span style="color: var(--ink-faint);">0</span>
                          )}
                        </td>
                        <td style="text-align: right; font-weight: 700;">
                          {q.scored_postings > 0 ? `${q.fit_rate_pct.toFixed(1)}%` : "—"}
                        </td>
                        <td>
                          <YieldBadge category={q.yield_category} scored={q.scored_postings} />
                        </td>
                        <td>
                          <a href="/settings" class="yield-action-btn" title="Edit query term in settings">
                            <i class="fa-solid fa-pen-to-square"></i> Edit
                          </a>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            /* Granular Tuple Table (Source, Query, Location) */
            <div class="table-responsive">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Query Term</th>
                    <th>Location</th>
                    <th style="text-align: right;">Postings</th>
                    <th style="text-align: right;">Scored</th>
                    <th style="text-align: right;">Strong Fits</th>
                    <th style="text-align: right;">Fit Rate</th>
                    <th>Scrapes</th>
                    <th>Yield Tier</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTuples.length === 0 ? (
                    <tr>
                      <td colSpan={9} class="text-center">No search tuples match the current filters.</td>
                    </tr>
                  ) : (
                    filteredTuples.map((t) => (
                      <tr key={`${t.source}-${t.query}-${t.location_id}`} class={t.yield_category === "high_yield" ? "row-highlight" : ""}>
                        <td style="text-transform: capitalize; font-weight: 700;">{t.source}</td>
                        <td>
                          <strong>{t.query}</strong>
                        </td>
                        <td>{t.location_id}</td>
                        <td style="text-align: right;">
                          <strong>{t.total_postings}</strong>
                        </td>
                        <td style="text-align: right;">{t.scored_postings}</td>
                        <td style="text-align: right; font-weight: 800;">
                          {t.strong_fits > 0 ? (
                            <span style="border-bottom: 2px solid var(--ink-primary);">{t.strong_fits}</span>
                          ) : (
                            <span style="color: var(--ink-faint);">0</span>
                          )}
                        </td>
                        <td style="text-align: right; font-weight: 700;">
                          {t.scored_postings > 0 ? `${t.fit_rate_pct.toFixed(1)}%` : "—"}
                        </td>
                        <td style="text-align: right;">{t.total_scrapes}</td>
                        <td>
                          <YieldBadge category={t.yield_category} scored={t.scored_postings} />
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Collapsible Scrape Coverage & Cell Status */}
      <div class="glass-card">
        <div class="card-header-row" style="cursor: pointer;" onClick={() => setShowCoverage(!showCoverage)}>
          <h3>
            <i class="fa-solid fa-satellite-dish text-purple"></i> Scraper Health & Coverage Details
          </h3>
          <div style="display: flex; align-items: center; gap: 12px;">
            {coverageCells && (
              <span class="results-count">
                {coverageCells.filter((c) => c.total_scrapes > 0).length}/{coverageCells.length} cells visited
              </span>
            )}
            <button type="button" class="yield-action-btn">
              <i class={`fa-solid fa-chevron-${showCoverage ? "up" : "down"}`}></i> {showCoverage ? "Hide" : "Show"}
            </button>
          </div>
        </div>
        {showCoverage && (
          <div style="margin-top: 14px;">
            <p class="card-note">Per-cell scraping frequency, staleness, and error status.</p>
            <div class="coverage-table-wrap">
              <CoverageTable cells={coverageCells} />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
