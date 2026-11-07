import { useEffect, useMemo, useState } from "preact/hooks";
import type { ComponentChildren } from "preact";
import { useStoredPreference } from "../lib/preferences";
import { LocationPicker } from "./LocationPicker";
import { SOURCE_LABELS } from "../lib/sources";
import {
  ApiError,
  deleteJSON,
  getJSON,
  guard,
  postJSON,
  putJSON,
  reportError,
} from "../api/client";
import type {
  Config,
  TargetCapacity,
  TargetLocation,
  TargetQuery,
  TargetsResponse,
} from "../api/types";

interface SearchTargetsWidgetProps {
  initialExpanded?: boolean;
  onTargetsChanged?: () => void;
  operations?: ComponentChildren;
}

function querySaveError(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const { detail } = JSON.parse(error.body);
      if (typeof detail === "string") return detail;
    } catch {
      // Network and proxy failures may return plain text instead of an API error.
    }
  }
  return "Could not save the query. Your selections are kept; please try again.";
}

export function SearchTargetsWidget({
  initialExpanded = false,
  onTargetsChanged,
  operations,
}: SearchTargetsWidgetProps) {
  const [expanded, setExpanded] = useState(initialExpanded);
  const [activeTab, setActiveTab] = useState<"queries" | "sources" | "capacity" | "operations">(
    "queries",
  );

  const [queries, setQueries] = useState<TargetQuery[]>([]);
  const [locations, setLocations] = useState<TargetLocation[]>([]);
  const [capacity, setCapacity] = useState<TargetCapacity | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [sources, setSources] = useState<Record<string, boolean>>({});
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [savingSources, setSavingSources] = useState(false);

  const [querySearchTerm, setQuerySearchTerm] = useStoredPreference("target-query-search", "");
  const [queryStatusFilter, setQueryStatusFilter] = useStoredPreference<
    "all" | "active" | "paused"
  >("target-query-status-filter", "all");
  const [querySourceFilter, setQuerySourceFilter] = useStoredPreference(
    "target-query-source-filter",
    "all",
  );

  const [showAddQueryModal, setShowAddQueryModal] = useState(false);
  const [newQueryText, setNewQueryText] = useState("");
  const [newQueryEnabled, setNewQueryEnabled] = useState(true);
  const [newQuerySource, setNewQuerySource] = useState("indeed");
  const [newQueryLocations, setNewQueryLocations] = useState<string[]>([]);
  const [locationQuery, setLocationQuery] = useState<TargetQuery | null>(null);
  const [editingLocations, setEditingLocations] = useState<string[]>([]);
  const [savingQuery, setSavingQuery] = useState(false);
  const [savingLocation, setSavingLocation] = useState(false);
  const [queryError, setQueryError] = useState("");

  const [editingQueryId, setEditingQueryId] = useState<number | null>(null);
  const [editingQueryText, setEditingQueryText] = useState("");

  const loadData = async () => {
    setLoading(true);
    try {
      const [targetsData, capData] = await Promise.all([
        guard("Loading target data", () => getJSON<TargetsResponse>("/api/targets")),
        guard("Loading target capacity", () => getJSON<TargetCapacity>("/api/targets/capacity")),
      ]);

      if (targetsData) {
        setQueries(targetsData.queries || []);
        setLocations(targetsData.locations || []);
      }
      if (capData) {
        setCapacity(capData);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    guard("Loading crawler sources", () => getJSON<Config>("/api/config")).then((data) => {
      if (data) {
        const scraper = data.scraper as { sources?: Record<string, boolean> } | undefined;
        setSources(scraper?.sources ?? {});
      }
      setSourcesLoading(false);
    });
  }, []);

  const showNotification = (msg: string) => {
    setActionMessage(msg);
    setTimeout(() => setActionMessage(null), 3500);
  };

  const handleSyncMatrix = async () => {
    setSyncing(true);
    try {
      const result = await postJSON<{ success: boolean; capacity: TargetCapacity }>(
        "/api/targets/sync-cells",
        {},
      );
      if (result && result.capacity) {
        setCapacity(result.capacity);
        showNotification("Search matrix cells synchronized with job board scrapers.");
        if (onTargetsChanged) onTargetsChanged();
      }
    } catch (err) {
      reportError("Syncing search matrix", err);
    } finally {
      setSyncing(false);
    }
  };

  const handleSaveSources = async () => {
    setSavingSources(true);
    try {
      await postJSON("/api/config", { scraper: { sources } });
      const cap = await getJSON<TargetCapacity>("/api/targets/capacity");
      if (cap) setCapacity(cap);
      showNotification("Crawler sources updated.");
    } catch (err) {
      reportError("Saving crawler sources", err);
    } finally {
      setSavingSources(false);
    }
  };

  const handleToggleQuery = async (queryId: number, currentEnabled: boolean | number) => {
    const nextEnabled = !currentEnabled;
    try {
      await putJSON(`/api/targets/queries/${queryId}/toggle`, { enabled: nextEnabled });
      setQueries((prev) =>
        prev.map((q) => (q.id === queryId ? { ...q, enabled: nextEnabled ? 1 : 0 } : q)),
      );
      getJSON<TargetCapacity>("/api/targets/capacity").then((c) => c && setCapacity(c));
      showNotification(`Query marked ${nextEnabled ? "ACTIVE" : "PAUSED"}.`);
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Toggling query", err);
    }
  };

  const handleDeleteQuery = async (queryId: number, queryText: string) => {
    if (!window.confirm(`Delete search query "${queryText}"?`)) return;
    try {
      await deleteJSON(`/api/targets/queries/${queryId}`);
      setQueries((prev) => prev.filter((q) => q.id !== queryId));
      getJSON<TargetCapacity>("/api/targets/capacity").then((c) => c && setCapacity(c));
      showNotification(`Deleted query "${queryText}".`);
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Deleting query", err);
    }
  };

  const handleStartEditQuery = (q: TargetQuery) => {
    setEditingQueryId(q.id);
    setEditingQueryText(q.query);
  };

  const handleSetQuerySource = async (q: TargetQuery, source: string) => {
    const nextSources = [source];
    try {
      await putJSON(`/api/targets/queries/${q.id}`, { sources: nextSources });
      setQueries((prev) =>
        prev.map((item) => (item.id === q.id ? { ...item, sources: nextSources } : item)),
      );
      getJSON<TargetCapacity>("/api/targets/capacity").then((c) => c && setCapacity(c));
      showNotification("Query source scope updated.");
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Updating query sources", err);
    }
  };

  const handleSaveEditQuery = async (queryId: number) => {
    if (!editingQueryText.trim()) return;
    try {
      await putJSON(`/api/targets/queries/${queryId}`, { query: editingQueryText.trim() });
      setQueries((prev) =>
        prev.map((q) => (q.id === queryId ? { ...q, query: editingQueryText.trim() } : q)),
      );
      setEditingQueryId(null);
      showNotification("Query term updated successfully.");
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Updating query", err);
    }
  };

  const handleAddQuery = async (e: Event) => {
    e.preventDefault();
    const terms = newQueryText
      .split(/[\n,]+/)
      .map((t) => t.trim())
      .filter(Boolean);

    if (terms.length === 0) {
      alert("Please enter at least one query term.");
      return;
    }

    if (!newQueryLocations.length || savingQuery || savingLocation) return;
    setQueryError("");
    setSavingQuery(true);
    try {
      for (const term of terms) {
        await postJSON("/api/targets/queries", {
          query: term,
          enabled: newQueryEnabled,
          sources: [newQuerySource],
          location_ids: newQueryLocations,
        });
      }
      setShowAddQueryModal(false);
      setNewQueryText("");
      setNewQueryLocations([]);
      await loadData();
      showNotification(`Added ${terms.length} new search query term(s).`);
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      setQueryError(querySaveError(err));
      reportError("Adding search query", err);
    } finally {
      setSavingQuery(false);
    }
  };

  const handleSaveQueryLocations = async (e: Event) => {
    e.preventDefault();
    if (!locationQuery || !editingLocations.length || savingQuery || savingLocation) return;
    setQueryError("");
    setSavingQuery(true);
    try {
      await putJSON(`/api/targets/queries/${locationQuery.id}`, { location_ids: editingLocations });
      setLocationQuery(null);
      await loadData();
      showNotification("Query locations updated.");
      onTargetsChanged?.();
    } catch (err) {
      setQueryError(querySaveError(err));
      reportError("Updating query locations", err);
    } finally {
      setSavingQuery(false);
    }
  };

  const refreshLocations = async () => {
    const data = await getJSON<TargetsResponse>("/api/targets");
    setLocations(data.locations);
  };

  const filteredQueries = useMemo(() => {
    return queries.filter((q) => {
      const matchesText =
        !querySearchTerm || q.query.toLowerCase().includes(querySearchTerm.toLowerCase());
      const isEnabled = Boolean(q.enabled);
      const matchesStatus =
        queryStatusFilter === "all" ||
        (queryStatusFilter === "active" && isEnabled) ||
        (queryStatusFilter === "paused" && !isEnabled);
      const querySources = q.sources || ["indeed", "linkedin"];
      const matchesSource = querySourceFilter === "all" || querySources.includes(querySourceFilter);
      return matchesText && matchesStatus && matchesSource;
    });
  }, [queries, querySearchTerm, queryStatusFilter, querySourceFilter]);

  const activeQueriesCount = useMemo(
    () => queries.filter((q) => Boolean(q.enabled)).length,
    [queries],
  );
  const locationsInUseCount = new Set(
    queries.filter((q) => q.enabled).flatMap((q) => q.location_ids),
  ).size;
  const assignedPairsCount = queries
    .filter((q) => q.enabled)
    .reduce((sum, q) => sum + q.location_ids.length, 0);
  const scheduledCellsCount = queries
    .filter((q) => q.enabled)
    .reduce(
      (sum, q) =>
        sum + q.location_ids.length * q.sources.filter((source) => sources[source]).length,
      0,
    );

  return (
    <div class="panel-section-box search-targets-panel mb-4">
      <div class="panel-section-header search-targets-header">
        <div class="search-targets-title-area">
          <span class="matrix-title">
            <i class="fa-solid fa-satellite-dish text-green"></i> SEARCH SETTINGS
          </span>
          <div class="matrix-chips-summary">
            <span
              class="matrix-chip clickable"
              onClick={() => {
                setExpanded(true);
                setActiveTab("queries");
              }}
              title="Click to inspect queries"
            >
              <i class="fa-solid fa-magnifying-glass"></i> <strong>{activeQueriesCount}</strong> /{" "}
              {queries.length} Queries
            </span>
            <span
              class="matrix-chip clickable"
              onClick={() => {
                setExpanded(true);
                setActiveTab("queries");
              }}
              title="Click to inspect query locations"
            >
              <i class="fa-solid fa-location-dot"></i> <strong>{locationsInUseCount}</strong>{" "}
              Locations in Use
            </span>
            <span
              class="matrix-chip clickable"
              onClick={() => {
                setExpanded(true);
                setActiveTab("capacity");
              }}
              title="Click to view capacity analysis"
            >
              <i class="fa-solid fa-layer-group"></i>{" "}
              <strong>{capacity?.search_pairs ?? assignedPairsCount}</strong> Query × Location (
              {capacity?.total_cells ?? scheduledCellsCount} cells)
            </span>
            {capacity && (
              <span class="matrix-chip-badge">
                {capacity.cycle_hours === null
                  ? "Sweep unavailable"
                  : `~${capacity.cycle_hours}h sweep`}
              </span>
            )}
          </div>
        </div>

        <div class="search-targets-header-actions">
          <button
            type="button"
            class="btn btn-sm btn-outline"
            onClick={handleSyncMatrix}
            disabled={syncing}
            title="Re-seed scrape cells with current target queries and locations"
          >
            <i class={`fa-solid fa-rotate ${syncing ? "fa-spin" : ""}`}></i>{" "}
            {syncing ? "Syncing..." : "Re-sync"}
          </button>
          <button
            type="button"
            class="btn btn-sm btn-outline"
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
            title={expanded ? "Collapse panel" : "Expand search matrix configuration"}
          >
            <i class={`fa-solid ${expanded ? "fa-chevron-up" : "fa-chevron-down"}`}></i>{" "}
            {expanded ? "Hide" : "Manage"}
          </button>
        </div>
      </div>

      {actionMessage && (
        <div class="search-targets-alert-banner">
          <i class="fa-solid fa-circle-check text-green"></i> {actionMessage}
        </div>
      )}

      {expanded && (
        <div class="panel-section-body search-targets-body">
          <div class="subtab-bar-container mb-4">
            <div class="subtab-bar">
              <button
                type="button"
                class={`action-pill ${activeTab === "queries" ? "active" : ""}`}
                onClick={() => setActiveTab("queries")}
              >
                <i class="fa-solid fa-magnifying-glass"></i> Search Query Terms ({queries.length})
              </button>
              <button
                type="button"
                class={`action-pill ${activeTab === "sources" ? "active" : ""}`}
                onClick={() => setActiveTab("sources")}
              >
                <i class="fa-solid fa-tower-broadcast"></i> Crawler Sources
              </button>
              <button
                type="button"
                class={`action-pill ${activeTab === "capacity" ? "active" : ""}`}
                onClick={() => setActiveTab("capacity")}
              >
                <i class="fa-solid fa-gauge-high"></i> Matrix Capacity & Sweep Health
              </button>
              {operations && (
                <button
                  type="button"
                  class={`action-pill ${activeTab === "operations" ? "active" : ""}`}
                  onClick={() => setActiveTab("operations")}
                >
                  <i class="fa-solid fa-rotate-right"></i> Status & One-off
                </button>
              )}
            </div>
          </div>

          {loading ? (
            <div class="loading-state p-4 text-center font-mono">
              <i class="fa-solid fa-spinner fa-spin"></i> Loading search targets matrix...
            </div>
          ) : (
            <>
              {activeTab === "queries" && (
                <div class="target-queries-tab">
                  <div class="targets-toolbar">
                    <div class="toolbar-search-box">
                      <i class="fa-solid fa-filter search-icon"></i>
                      <input
                        type="text"
                        class="form-input"
                        placeholder="Filter search queries..."
                        value={querySearchTerm}
                        onInput={(e) => setQuerySearchTerm((e.target as HTMLInputElement).value)}
                      />
                      {querySearchTerm && (
                        <button
                          type="button"
                          class="clear-filter-btn"
                          onClick={() => setQuerySearchTerm("")}
                        >
                          <i class="fa-solid fa-xmark"></i>
                        </button>
                      )}
                    </div>

                    <div class="toolbar-filters">
                      <select
                        class="form-select"
                        value={queryStatusFilter}
                        onChange={(e) =>
                          setQueryStatusFilter(
                            (e.target as HTMLSelectElement).value as "all" | "active" | "paused",
                          )
                        }
                      >
                        <option value="all">All Statuses</option>
                        <option value="active">Active Only ({activeQueriesCount})</option>
                        <option value="paused">
                          Paused Only ({queries.length - activeQueriesCount})
                        </option>
                      </select>
                      <select
                        class="form-select"
                        value={querySourceFilter}
                        onChange={(e) =>
                          setQuerySourceFilter((e.target as HTMLSelectElement).value)
                        }
                      >
                        <option value="all">All Sources</option>
                        <option value="indeed">Indeed</option>
                        <option value="linkedin">LinkedIn</option>
                        <option value="google">Google Jobs</option>
                      </select>

                      <button
                        type="button"
                        class="btn btn-primary"
                        onClick={() => {
                          setQueryError("");
                          setNewQueryLocations([]);
                          setShowAddQueryModal(true);
                        }}
                      >
                        <i class="fa-solid fa-plus"></i> Add Query Term
                      </button>
                    </div>
                  </div>

                  <div class="target-queries-list mt-3">
                    {filteredQueries.length === 0 ? (
                      <div class="empty-queries-box">
                        <i class="fa-solid fa-filter-circle-xmark"></i> No search queries matching
                        filter.
                      </div>
                    ) : (
                      <div
                        class="queries-table-wrap"
                        role="region"
                        aria-label="Search query settings"
                        tabIndex={0}
                      >
                        <table class="retro-table">
                          <thead>
                            <tr>
                              <th style={{ width: "110px" }}>STATUS</th>
                              <th>SEARCH QUERY TERM</th>
                              <th>LOCATIONS</th>
                              <th style={{ width: "190px" }}>SCHEDULED SOURCES</th>
                              <th style={{ width: "130px", textAlign: "right" }}>ACTIONS</th>
                            </tr>
                          </thead>
                          <tbody>
                            {filteredQueries.map((q) => {
                              const isEditing = editingQueryId === q.id;
                              const isEnabled = Boolean(q.enabled);
                              const querySources = q.sources || ["indeed", "linkedin"];

                              return (
                                <tr key={q.id} class={isEnabled ? "row-active" : "row-paused"}>
                                  <td>
                                    <button
                                      type="button"
                                      class={`status-toggle-pill ${isEnabled ? "active" : "paused"}`}
                                      onClick={() => handleToggleQuery(q.id, q.enabled)}
                                      title={
                                        isEnabled
                                          ? "Click to Pause Query"
                                          : "Click to Activate Query"
                                      }
                                    >
                                      <i
                                        class={`fa-solid ${isEnabled ? "fa-circle-check" : "fa-circle-pause"}`}
                                      ></i>{" "}
                                      {isEnabled ? "ACTIVE" : "PAUSED"}
                                    </button>
                                  </td>
                                  <td>
                                    {isEditing ? (
                                      <input
                                        type="text"
                                        class="form-input form-input-sm"
                                        value={editingQueryText}
                                        onInput={(e) =>
                                          setEditingQueryText((e.target as HTMLInputElement).value)
                                        }
                                        onKeyDown={(e) => {
                                          if (e.key === "Enter") handleSaveEditQuery(q.id);
                                          if (e.key === "Escape") setEditingQueryId(null);
                                        }}
                                        autoFocus
                                      />
                                    ) : (
                                      <span class="query-text-cell">
                                        <i class="fa-solid fa-quote-left text-faint"></i>{" "}
                                        <strong>{q.query}</strong>
                                      </span>
                                    )}
                                  </td>
                                  <td>
                                    <span class="query-location-labels">
                                      {q.location_ids
                                        .map(
                                          (id) =>
                                            locations.find((loc) => loc.id === id)?.label || id,
                                        )
                                        .join(", ") || "Choose locations"}
                                    </span>
                                    <button
                                      type="button"
                                      class="btn btn-xs btn-outline"
                                      aria-label={`Edit locations for ${q.query}`}
                                      onClick={() => {
                                        setQueryError("");
                                        setLocationQuery(q);
                                        setEditingLocations([...q.location_ids]);
                                      }}
                                    >
                                      Edit locations
                                    </button>
                                  </td>
                                  <td>
                                    {querySources.length > 1 && (
                                      <span
                                        class="text-faint"
                                        style={{ fontSize: "11px", marginRight: "8px" }}
                                      >
                                        Legacy: choose one
                                      </span>
                                    )}
                                    {Object.entries(SOURCE_LABELS).map(([source, label]) => (
                                      <label
                                        class="checkbox-label"
                                        key={source}
                                        style={{ marginRight: "10px" }}
                                      >
                                        <input
                                          type="radio"
                                          name={`query-source-${q.id}`}
                                          checked={
                                            querySources.length === 1 && querySources[0] === source
                                          }
                                          onChange={() => handleSetQuerySource(q, source)}
                                        />{" "}
                                        {label}
                                      </label>
                                    ))}
                                  </td>
                                  <td style={{ textAlign: "right" }}>
                                    {isEditing ? (
                                      <div class="row-action-btns">
                                        <button
                                          type="button"
                                          class="btn btn-xs btn-primary"
                                          onClick={() => handleSaveEditQuery(q.id)}
                                          title="Save changes"
                                        >
                                          <i class="fa-solid fa-check"></i>
                                        </button>
                                        <button
                                          type="button"
                                          class="btn btn-xs btn-outline"
                                          onClick={() => setEditingQueryId(null)}
                                          title="Cancel"
                                        >
                                          <i class="fa-solid fa-xmark"></i>
                                        </button>
                                      </div>
                                    ) : (
                                      <div class="row-action-btns">
                                        <button
                                          type="button"
                                          class="btn btn-xs btn-outline"
                                          onClick={() => handleStartEditQuery(q)}
                                          title="Edit query term"
                                        >
                                          <i class="fa-solid fa-pen-to-square"></i>
                                        </button>
                                        <button
                                          type="button"
                                          class="btn btn-xs btn-outline text-red"
                                          onClick={() => handleDeleteQuery(q.id, q.query)}
                                          title="Delete query"
                                        >
                                          <i class="fa-solid fa-trash-can"></i>
                                        </button>
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {activeTab === "sources" && (
                <div class="target-sources-tab">
                  <h3>Enabled Crawler Sources</h3>
                  <p class="form-hint">
                    Sources determine which job boards the scraper visits for every active search
                    pair.
                  </p>
                  <div class="checkbox-grid mt-4">
                    {Object.entries(SOURCE_LABELS).map(([key, label]) => (
                      <label key={key} class="checkbox-label">
                        <input
                          type="checkbox"
                          checked={Boolean(sources[key])}
                          disabled={sourcesLoading || savingSources}
                          onChange={(e) =>
                            setSources((previous) => ({
                              ...previous,
                              [key]: (e.target as HTMLInputElement).checked,
                            }))
                          }
                        />{" "}
                        {label}
                      </label>
                    ))}
                  </div>
                  <button
                    type="button"
                    class="btn btn-primary mt-4"
                    disabled={sourcesLoading || savingSources}
                    onClick={handleSaveSources}
                  >
                    <i
                      class={`fa-solid ${savingSources ? "fa-spinner fa-spin" : "fa-floppy-disk"}`}
                    ></i>{" "}
                    {savingSources ? "Saving..." : "Save Crawler Sources"}
                  </button>
                </div>
              )}

              {activeTab === "capacity" && (
                <div class="target-capacity-tab">
                  <div class="capacity-hero-card">
                    <div class="capacity-metric-row">
                      <div class="capacity-metric-box">
                        <span class="metric-num">{activeQueriesCount}</span>
                        <span class="metric-lbl">Active Search Queries</span>
                      </div>
                      <div class="capacity-metric-box">
                        <span class="metric-num">{locationsInUseCount}</span>
                        <span class="metric-lbl">Locations in Use</span>
                      </div>
                      <div class="capacity-metric-box highlight">
                        <span class="metric-num">
                          {capacity?.search_pairs ?? assignedPairsCount}
                        </span>
                        <span class="metric-lbl">Query × Location Combinations</span>
                      </div>
                      <div class="capacity-metric-box">
                        <span class="metric-num">
                          {capacity?.total_cells ?? scheduledCellsCount}
                        </span>
                        <span class="metric-lbl">Scheduled Scrape Cells</span>
                      </div>
                    </div>

                    <div class="capacity-status-banner mt-4">
                      <div class="capacity-banner-left">
                        <h4>
                          Estimated sweep:{" "}
                          {capacity?.cycle_hours == null
                            ? "unavailable"
                            : `~${capacity.cycle_hours} hours`}
                        </h4>
                        {Object.entries(capacity?.sources ?? {}).map(([source, estimate]) => (
                          <p class="card-note" key={source}>
                            {SOURCE_LABELS[source] || source}: {estimate.cells} cells,{" "}
                            {estimate.runs_per_day} runs/day,{" "}
                            {estimate.cycle_hours === null
                              ? "budget cannot fit a cell"
                              : `~${estimate.cycle_hours}h sweep`}
                          </p>
                        ))}
                        <p class="card-note">
                          Estimate of visits with no cached descriptions. A visit can stop at the
                          request budget before all results are retrieved.
                        </p>
                      </div>
                      <div class="capacity-banner-right">
                        <button
                          type="button"
                          class="btn btn-primary"
                          onClick={handleSyncMatrix}
                          disabled={syncing}
                        >
                          <i class={`fa-solid fa-rotate ${syncing ? "fa-spin" : ""}`}></i> Force
                          Matrix Re-sync
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === "operations" && operations}
            </>
          )}
        </div>
      )}

      {showAddQueryModal && (
        <div
          class="retro-modal-overlay"
          onClick={() => !savingQuery && !savingLocation && setShowAddQueryModal(false)}
        >
          <div
            class="retro-modal-box"
            role="dialog"
            aria-modal="true"
            aria-label="Add search query"
            onClick={(e) => e.stopPropagation()}
          >
            <div class="retro-modal-header">
              <h3>
                <i class="fa-solid fa-plus"></i> Add Search Query Term
              </h3>
              <button
                type="button"
                class="close-modal-btn"
                aria-label="Close"
                disabled={savingQuery || savingLocation}
                onClick={() => !savingQuery && !savingLocation && setShowAddQueryModal(false)}
              >
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
            <form onSubmit={handleAddQuery} class="retro-modal-body">
              <div class="form-group mb-3">
                <label class="form-label" for="new-query-text">
                  Search Query String(s)
                </label>
                <textarea
                  id="new-query-text"
                  class="form-textarea"
                  rows={3}
                  placeholder="e.g. AI Engineer, Agentic AI, Cloud Architect (Separate terms with commas or newlines)"
                  value={newQueryText}
                  onInput={(e) => setNewQueryText((e.target as HTMLTextAreaElement).value)}
                  required
                ></textarea>
                <span class="form-hint">
                  Each term uses the board and locations selected below.
                </span>
              </div>

              <div class="form-group mb-3">
                <label class="form-label">Job Board</label>
                {Object.entries(SOURCE_LABELS).map(([source, label]) => (
                  <label class="checkbox-label" key={source} style={{ marginRight: "16px" }}>
                    <input
                      type="radio"
                      name="new-query-source"
                      checked={newQuerySource === source}
                      onChange={() => setNewQuerySource(source)}
                    />{" "}
                    {label}
                  </label>
                ))}
                {newQuerySource === "indeed" && (
                  <span class="form-hint" style={{ display: "block", marginTop: "8px" }}>
                    <strong>Indeed search tips:</strong> Indeed also searches descriptions. Use
                    <code>"quoted phrases"</code> for exact matches, <code>-word</code> to exclude
                    noise, and <code>(python OR go)</code> for alternatives. Example:{" "}
                    <code>"platform engineer" (python OR go) -marketing</code>.
                  </span>
                )}
                {newQuerySource === "google" && (
                  <span class="form-hint" style={{ display: "block", marginTop: "8px" }}>
                    Google Jobs combines your term with location, remote status, and an age bucket.
                    The configured radius is not enforced. Google is opt in for each query.
                  </span>
                )}
              </div>

              <LocationPicker
                locations={locations}
                selectedIds={newQueryLocations}
                onChange={setNewQueryLocations}
                onLocationSaved={refreshLocations}
                disabled={savingQuery}
                onBusyChange={setSavingLocation}
              />

              {queryError && <p role="alert">{queryError}</p>}
              <div class="form-group mb-4">
                <label class="checkbox-label">
                  <input
                    type="checkbox"
                    checked={newQueryEnabled}
                    onChange={(e) => setNewQueryEnabled((e.target as HTMLInputElement).checked)}
                  />{" "}
                  Activate immediately in search rotation
                </label>
              </div>

              <div class="retro-modal-footer">
                <button
                  type="button"
                  class="btn btn-outline"
                  disabled={savingQuery || savingLocation}
                  onClick={() => !savingQuery && !savingLocation && setShowAddQueryModal(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  class="btn btn-primary"
                  disabled={!newQueryLocations.length || savingQuery || savingLocation}
                >
                  <i class="fa-solid fa-check"></i> {savingQuery ? "Saving…" : "Add Query Term(s)"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {locationQuery && (
        <div
          class="retro-modal-overlay"
          onClick={() => !savingQuery && !savingLocation && setLocationQuery(null)}
        >
          <div
            class="retro-modal-box"
            role="dialog"
            aria-modal="true"
            aria-label="Edit query locations"
            onClick={(e) => e.stopPropagation()}
          >
            <div class="retro-modal-header">
              <h3>Locations for {locationQuery.query}</h3>
              <button
                type="button"
                class="close-modal-btn"
                aria-label="Close"
                disabled={savingQuery || savingLocation}
                onClick={() => setLocationQuery(null)}
              >
                ×
              </button>
            </div>
            <form class="retro-modal-body" onSubmit={handleSaveQueryLocations}>
              <LocationPicker
                locations={locations}
                selectedIds={editingLocations}
                onChange={setEditingLocations}
                onLocationSaved={refreshLocations}
                disabled={savingQuery}
                onBusyChange={setSavingLocation}
              />
              {queryError && <p role="alert">{queryError}</p>}
              <div class="retro-modal-footer">
                <button
                  type="button"
                  class="btn btn-outline"
                  disabled={savingQuery || savingLocation}
                  onClick={() => setLocationQuery(null)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  class="btn btn-primary"
                  disabled={!editingLocations.length || savingQuery || savingLocation}
                >
                  {savingQuery ? "Saving…" : "Save locations"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
