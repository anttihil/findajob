import { useEffect, useMemo, useState } from "preact/hooks";
import { useStoredPreference } from "../lib/preferences";
import { deleteJSON, getJSON, guard, postJSON, putJSON, reportError } from "../api/client";
import type {
  TargetCapacity,
  TargetLocation,
  TargetQuery,
  TargetsResponse,
} from "../api/types";

interface PresetItem {
  id: string;
  label: string;
  search_label: string;
  country: string;
  indeed_country: string;
  is_remote: boolean;
  distance: number;
  region: "Remote" | "North America" | "Europe & UK" | "Asia-Pacific";
}

const LOCATION_PRESETS: PresetItem[] = [
  // Remote Work
  {
    id: "us_remote",
    label: "United States (remote)",
    search_label: "United States",
    country: "US",
    indeed_country: "usa",
    is_remote: true,
    distance: 50,
    region: "Remote",
  },
  {
    id: "eu_remote",
    label: "European Union (remote)",
    search_label: "European Union",
    country: "EU",
    indeed_country: "uk",
    is_remote: true,
    distance: 50,
    region: "Remote",
  },
  {
    id: "global_remote",
    label: "Worldwide / Global (remote)",
    search_label: "Remote",
    country: "US",
    indeed_country: "usa",
    is_remote: true,
    distance: 50,
    region: "Remote",
  },
  // North America
  {
    id: "san_francisco",
    label: "San Francisco Bay Area, CA",
    search_label: "San Francisco, CA",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "new_york",
    label: "New York, NY",
    search_label: "New York, NY",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "seattle",
    label: "Seattle, WA",
    search_label: "Seattle, WA",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "austin",
    label: "Austin, TX",
    search_label: "Austin, TX",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "boston",
    label: "Boston, MA",
    search_label: "Boston, MA",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "los_angeles",
    label: "Los Angeles, CA",
    search_label: "Los Angeles, CA",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "chicago",
    label: "Chicago, IL",
    search_label: "Chicago, IL",
    country: "US",
    indeed_country: "usa",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "toronto",
    label: "Toronto, ON, Canada",
    search_label: "Toronto, ON",
    country: "CA",
    indeed_country: "canada",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  {
    id: "vancouver",
    label: "Vancouver, BC, Canada",
    search_label: "Vancouver, BC",
    country: "CA",
    indeed_country: "canada",
    is_remote: false,
    distance: 50,
    region: "North America",
  },
  // Europe & UK
  {
    id: "london",
    label: "London, United Kingdom",
    search_label: "London, UK",
    country: "GB",
    indeed_country: "uk",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "berlin",
    label: "Berlin, Germany",
    search_label: "Berlin, Germany",
    country: "DE",
    indeed_country: "germany",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "amsterdam",
    label: "Amsterdam, Netherlands",
    search_label: "Amsterdam, Netherlands",
    country: "NL",
    indeed_country: "netherlands",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "paris",
    label: "Paris, France",
    search_label: "Paris, France",
    country: "FR",
    indeed_country: "france",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "dublin",
    label: "Dublin, Ireland",
    search_label: "Dublin, Ireland",
    country: "IE",
    indeed_country: "ireland",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "stockholm",
    label: "Stockholm, Sweden",
    search_label: "Stockholm, Sweden",
    country: "SE",
    indeed_country: "sweden",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "helsinki",
    label: "Helsinki, Finland",
    search_label: "Helsinki, Finland",
    country: "FI",
    indeed_country: "finland",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "oslo",
    label: "Oslo, Norway",
    search_label: "Oslo, Norway",
    country: "NO",
    indeed_country: "norway",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "copenhagen",
    label: "Copenhagen, Denmark",
    search_label: "Copenhagen, Denmark",
    country: "DK",
    indeed_country: "denmark",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  {
    id: "zurich",
    label: "Zurich, Switzerland",
    search_label: "Zurich, Switzerland",
    country: "CH",
    indeed_country: "switzerland",
    is_remote: false,
    distance: 50,
    region: "Europe & UK",
  },
  // Asia-Pacific
  {
    id: "singapore",
    label: "Singapore",
    search_label: "Singapore",
    country: "SG",
    indeed_country: "singapore",
    is_remote: false,
    distance: 50,
    region: "Asia-Pacific",
  },
  {
    id: "sydney",
    label: "Sydney, Australia",
    search_label: "Sydney, Australia",
    country: "AU",
    indeed_country: "australia",
    is_remote: false,
    distance: 50,
    region: "Asia-Pacific",
  },
  {
    id: "tokyo",
    label: "Tokyo, Japan",
    search_label: "Tokyo, Japan",
    country: "JP",
    indeed_country: "japan",
    is_remote: false,
    distance: 50,
    region: "Asia-Pacific",
  },
];

interface SearchTargetsWidgetProps {
  initialExpanded?: boolean;
  onTargetsChanged?: () => void;
}

export function SearchTargetsWidget({
  initialExpanded = false,
  onTargetsChanged,
}: SearchTargetsWidgetProps) {
  const [expanded, setExpanded] = useState(initialExpanded);
  const [activeTab, setActiveTab] = useState<"queries" | "locations" | "capacity">("queries");

  const [queries, setQueries] = useState<TargetQuery[]>([]);
  const [locations, setLocations] = useState<TargetLocation[]>([]);
  const [capacity, setCapacity] = useState<TargetCapacity | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  // Queries Filter & Add Form State
  const [querySearchTerm, setQuerySearchTerm] = useStoredPreference("target-query-search", "");
  const [queryStatusFilter, setQueryStatusFilter] = useStoredPreference<"all" | "active" | "paused">("target-query-status-filter", "all");

  const [showAddQueryModal, setShowAddQueryModal] = useState(false);
  const [newQueryText, setNewQueryText] = useState("");
  const [newQueryEnabled, setNewQueryEnabled] = useState(true);

  // Query Inline Editing State
  const [editingQueryId, setEditingQueryId] = useState<number | null>(null);
  const [editingQueryText, setEditingQueryText] = useState("");

  // Location Form & Editing State
  const [showAddLocationModal, setShowAddLocationModal] = useState(false);
  const [selectedPresetRegion, setSelectedPresetRegion] = useState<string>("All");
  const [locId, setLocId] = useState("");
  const [locLabel, setLocLabel] = useState("");
  const [locSearchLabel, setLocSearchLabel] = useState("");
  const [locCountry, setLocCountry] = useState("US");
  const [locIndeedCountry, setLocIndeedCountry] = useState("usa");
  const [locIsRemote, setLocIsRemote] = useState(false);
  const [locDistance, setLocDistance] = useState(50);
  const [locEnabled, setLocEnabled] = useState(true);
  const [isEditingLoc, setIsEditingLoc] = useState(false);

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
        {}
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

  // --- Search Query Actions ---
  const handleToggleQuery = async (queryId: number, currentEnabled: boolean | number) => {
    const nextEnabled = !currentEnabled;
    try {
      await putJSON(`/api/targets/queries/${queryId}/toggle`, { enabled: nextEnabled });
      setQueries((prev) =>
        prev.map((q) => (q.id === queryId ? { ...q, enabled: nextEnabled ? 1 : 0 } : q))
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

  const handleSaveEditQuery = async (queryId: number) => {
    if (!editingQueryText.trim()) return;
    try {
      await putJSON(`/api/targets/queries/${queryId}`, { query: editingQueryText.trim() });
      setQueries((prev) =>
        prev.map((q) => (q.id === queryId ? { ...q, query: editingQueryText.trim() } : q))
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

    try {
      for (const term of terms) {
        await postJSON("/api/targets/queries", { query: term, enabled: newQueryEnabled });
      }
      setShowAddQueryModal(false);
      setNewQueryText("");
      await loadData();
      showNotification(`Added ${terms.length} new search query term(s).`);
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Adding search query", err);
    }
  };

  // --- Location Actions ---
  const handleToggleLocation = async (locIdVal: string, currentEnabled: boolean | number) => {
    const nextEnabled = !currentEnabled;
    try {
      await putJSON(`/api/targets/locations/${locIdVal}/toggle`, { enabled: nextEnabled });
      setLocations((prev) =>
        prev.map((l) => (l.id === locIdVal ? { ...l, enabled: nextEnabled ? 1 : 0 } : l))
      );
      getJSON<TargetCapacity>("/api/targets/capacity").then((c) => c && setCapacity(c));
      showNotification(`Location "${locIdVal}" marked ${nextEnabled ? "ACTIVE" : "PAUSED"}.`);
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Toggling location", err);
    }
  };

  const handleDeleteLocation = async (locIdVal: string, locName: string) => {
    if (!window.confirm(`Delete search location "${locName}" (${locIdVal})?`)) return;
    try {
      await deleteJSON(`/api/targets/locations/${locIdVal}`);
      setLocations((prev) => prev.filter((l) => l.id !== locIdVal));
      getJSON<TargetCapacity>("/api/targets/capacity").then((c) => c && setCapacity(c));
      showNotification(`Deleted location "${locName}".`);
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Deleting location", err);
    }
  };

  const handleOpenAddLocation = (preset?: PresetItem) => {
    if (preset) {
      setLocId(preset.id);
      setLocLabel(preset.label);
      setLocSearchLabel(preset.search_label);
      setLocCountry(preset.country);
      setLocIndeedCountry(preset.indeed_country);
      setLocIsRemote(preset.is_remote);
      setLocDistance(preset.distance);
      setLocEnabled(true);
      setIsEditingLoc(false);
    } else {
      setLocId("");
      setLocLabel("");
      setLocSearchLabel("");
      setLocCountry("US");
      setLocIndeedCountry("usa");
      setLocIsRemote(false);
      setLocDistance(50);
      setLocEnabled(true);
      setIsEditingLoc(false);
    }
    setShowAddLocationModal(true);
  };

  const handleOpenEditLocation = (loc: TargetLocation) => {
    setLocId(loc.id);
    setLocLabel(loc.label);
    setLocSearchLabel(loc.search_label || loc.label);
    setLocCountry(loc.country || "US");
    setLocIndeedCountry(loc.indeed_country || "usa");
    setLocIsRemote(Boolean(loc.is_remote));
    setLocDistance(loc.distance ?? 50);
    setLocEnabled(Boolean(loc.enabled));
    setIsEditingLoc(true);
    setShowAddLocationModal(true);
  };

  const handleSaveLocation = async (e: Event) => {
    e.preventDefault();
    const cleanId = locId.trim().toLowerCase().replace(/[^a-z0-9_]/g, "_");
    if (!cleanId || !locLabel.trim()) {
      alert("Please provide a location ID and label.");
      return;
    }

    const payload = {
      id: cleanId,
      label: locLabel.trim(),
      search_label: locSearchLabel.trim() || locLabel.trim(),
      country: locCountry.trim().toUpperCase(),
      indeed_country: locIndeedCountry.trim().toLowerCase(),
      is_remote: locIsRemote,
      distance: Number(locDistance) || 50,
      enabled: locEnabled,
    };

    try {
      if (isEditingLoc) {
        await putJSON(`/api/targets/locations/${cleanId}`, payload);
        showNotification(`Location "${locLabel}" updated.`);
      } else {
        await postJSON("/api/targets/locations", payload);
        showNotification(`Location "${locLabel}" added.`);
      }
      setShowAddLocationModal(false);
      await loadData();
      if (onTargetsChanged) onTargetsChanged();
    } catch (err) {
      reportError("Saving location", err);
    }
  };

  // Filtered queries
  const filteredQueries = useMemo(() => {
    return queries.filter((q) => {
      const matchesText =
        !querySearchTerm || q.query.toLowerCase().includes(querySearchTerm.toLowerCase());
      const isEnabled = Boolean(q.enabled);
      const matchesStatus =
        queryStatusFilter === "all" ||
        (queryStatusFilter === "active" && isEnabled) ||
        (queryStatusFilter === "paused" && !isEnabled);
      return matchesText && matchesStatus;
    });
  }, [queries, querySearchTerm, queryStatusFilter]);

  const activeQueriesCount = useMemo(() => queries.filter((q) => Boolean(q.enabled)).length, [queries]);
  const activeLocationsCount = useMemo(
    () => locations.filter((l) => Boolean(l.enabled)).length,
    [locations]
  );

  const filteredPresets = useMemo(() => {
    if (selectedPresetRegion === "All") return LOCATION_PRESETS;
    return LOCATION_PRESETS.filter((p) => p.region === selectedPresetRegion);
  }, [selectedPresetRegion]);

  const zoneBadgeClass =
    capacity?.zone === "optimal"
      ? "badge-success"
      : capacity?.zone === "balanced"
        ? "badge-info"
        : "badge-warning";

  return (
    <div class="panel-section-box search-targets-panel mb-4">
      {/* Top Header Bar */}
      <div class="panel-section-header search-targets-header">
        <div class="search-targets-title-area">
          <span class="matrix-title">
            <i class="fa-solid fa-satellite-dish text-green"></i> SEARCH MATRIX & TARGETS
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
              <i class="fa-solid fa-magnifying-glass"></i>{" "}
              <strong>{activeQueriesCount}</strong> / {queries.length} Queries
            </span>
            <span
              class="matrix-chip clickable"
              onClick={() => {
                setExpanded(true);
                setActiveTab("locations");
              }}
              title="Click to set locations"
            >
              <i class="fa-solid fa-location-dot"></i>{" "}
              <strong>{activeLocationsCount}</strong> / {locations.length} Locations
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
              <strong>{capacity?.search_pairs ?? activeQueriesCount * activeLocationsCount}</strong> Pairs (
              {capacity?.total_cells ?? (activeQueriesCount * activeLocationsCount * 2)} cells)
            </span>
            {capacity && (
              <span class={`matrix-chip-badge ${zoneBadgeClass}`}>
                {capacity.zone.toUpperCase()} (~{capacity.cycle_hours}h sweep)
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

      {/* Expanded Management Body */}
      {expanded && (
        <div class="panel-section-body search-targets-body">
          {/* Sub-Tabs Bar */}
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
                class={`action-pill ${activeTab === "locations" ? "active" : ""}`}
                onClick={() => setActiveTab("locations")}
              >
                <i class="fa-solid fa-location-dot"></i> Search Locations ({locations.length})
              </button>
              <button
                type="button"
                class={`action-pill ${activeTab === "capacity" ? "active" : ""}`}
                onClick={() => setActiveTab("capacity")}
              >
                <i class="fa-solid fa-gauge-high"></i> Matrix Capacity & Sweep Health
              </button>
            </div>
          </div>

          {loading ? (
            <div class="loading-state p-4 text-center font-mono">
              <i class="fa-solid fa-spinner fa-spin"></i> Loading search targets matrix...
            </div>
          ) : (
            <>
              {/* TAB 1: SEARCH QUERY TERMS */}
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
                            (e.target as HTMLSelectElement).value as "all" | "active" | "paused"
                          )
                        }
                      >
                        <option value="all">All Statuses</option>
                        <option value="active">Active Only ({activeQueriesCount})</option>
                        <option value="paused">Paused Only ({queries.length - activeQueriesCount})</option>
                      </select>

                      <button
                        type="button"
                        class="btn btn-primary"
                        onClick={() => setShowAddQueryModal(true)}
                      >
                        <i class="fa-solid fa-plus"></i> Add Query Term
                      </button>
                    </div>
                  </div>

                  {/* Query Items List */}
                  <div class="target-queries-list mt-3">
                    {filteredQueries.length === 0 ? (
                      <div class="empty-queries-box">
                        <i class="fa-solid fa-filter-circle-xmark"></i> No search queries matching filter.
                      </div>
                    ) : (
                      <div class="queries-table-wrap">
                        <table class="retro-table">
                          <thead>
                            <tr>
                              <th style={{ width: "110px" }}>STATUS</th>
                              <th>SEARCH QUERY TERM</th>
                              <th style={{ width: "130px", textAlign: "right" }}>ACTIONS</th>
                            </tr>
                          </thead>
                          <tbody>
                            {filteredQueries.map((q) => {
                              const isEditing = editingQueryId === q.id;
                              const isEnabled = Boolean(q.enabled);

                              return (
                                <tr key={q.id} class={isEnabled ? "row-active" : "row-paused"}>
                                  <td>
                                    <button
                                      type="button"
                                      class={`status-toggle-pill ${isEnabled ? "active" : "paused"}`}
                                      onClick={() => handleToggleQuery(q.id, q.enabled)}
                                      title={isEnabled ? "Click to Pause Query" : "Click to Activate Query"}
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

              {/* TAB 2: SEARCH LOCATIONS */}
              {activeTab === "locations" && (
                <div class="target-locations-tab">
                  {/* Location Presets & Quick Add Bar */}
                  <div class="location-presets-box mb-4">
                    <div class="presets-header">
                      <span>
                        <i class="fa-solid fa-wand-magic-sparkles text-gold"></i> POPULAR TECH HUB PRESETS:
                      </span>
                      <button
                        type="button"
                        class="btn btn-primary btn-sm"
                        onClick={() => handleOpenAddLocation()}
                      >
                        <i class="fa-solid fa-plus"></i> Custom Location
                      </button>
                    </div>

                    {/* Region Selector for Presets */}
                    <div class="preset-regions-bar mt-2">
                      {["All", "Remote", "North America", "Europe & UK", "Asia-Pacific"].map((reg) => (
                        <button
                          key={reg}
                          type="button"
                          class={`preset-region-btn ${selectedPresetRegion === reg ? "active" : ""}`}
                          onClick={() => setSelectedPresetRegion(reg)}
                        >
                          {reg}
                        </button>
                      ))}
                    </div>

                    <div class="presets-chips-grid mt-2">
                      {filteredPresets.map((preset) => {
                        const exists = locations.some((l) => l.id === preset.id);
                        return (
                          <button
                            key={preset.id}
                            type="button"
                            class={`preset-chip ${exists ? "installed" : ""}`}
                            onClick={() => handleOpenAddLocation(preset)}
                            title={
                              exists
                                ? `Already configured (${preset.id}). Click to edit.`
                                : `Click to add ${preset.label}`
                            }
                          >
                            <i
                              class={`fa-solid ${exists ? "fa-check text-green" : "fa-plus"}`}
                            ></i>{" "}
                            {preset.label}
                            {preset.is_remote && <span class="preset-tag-remote">REMOTE</span>}
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {/* Configured Locations Table */}
                  <div class="locations-table-wrap">
                    <table class="retro-table">
                      <thead>
                        <tr>
                          <th style={{ width: "110px" }}>STATUS</th>
                          <th>LOCATION & SEARCH STRING</th>
                          <th style={{ width: "110px" }}>COUNTRY</th>
                          <th style={{ width: "130px" }}>WORKPLACE</th>
                          <th style={{ width: "90px" }}>RADIUS</th>
                          <th style={{ width: "130px", textAlign: "right" }}>ACTIONS</th>
                        </tr>
                      </thead>
                      <tbody>
                        {locations.map((loc) => {
                          const isEnabled = Boolean(loc.enabled);
                          const isRemote = Boolean(loc.is_remote);

                          return (
                            <tr key={loc.id} class={isEnabled ? "row-active" : "row-paused"}>
                              <td>
                                <button
                                  type="button"
                                  class={`status-toggle-pill ${isEnabled ? "active" : "paused"}`}
                                  onClick={() => handleToggleLocation(loc.id, loc.enabled)}
                                  title={isEnabled ? "Click to Pause Location" : "Click to Activate Location"}
                                >
                                  <i
                                    class={`fa-solid ${isEnabled ? "fa-circle-check" : "fa-circle-pause"}`}
                                  ></i>{" "}
                                  {isEnabled ? "ACTIVE" : "PAUSED"}
                                </button>
                              </td>
                              <td>
                                <div class="location-name-cell">
                                  <strong>{loc.label}</strong>
                                  <span class="location-id-sub">
                                    ID: <code>{loc.id}</code> · Board Search: &quot;{loc.search_label || loc.label}&quot;
                                  </span>
                                </div>
                              </td>
                              <td>
                                <span class="country-badge">
                                  {loc.country || "US"} ({loc.indeed_country || "usa"})
                                </span>
                              </td>
                              <td>
                                <span class={`workplace-tag workplace-${isRemote ? "remote" : "onsite"}`}>
                                  {isRemote ? (
                                    <>
                                      <i class="fa-solid fa-wifi"></i> Remote
                                    </>
                                  ) : (
                                    <>
                                      <i class="fa-solid fa-building"></i> Onsite
                                    </>
                                  )}
                                </span>
                              </td>
                              <td>{loc.distance ?? 50} mi</td>
                              <td style={{ textAlign: "right" }}>
                                <div class="row-action-btns">
                                  <button
                                    type="button"
                                    class="btn btn-xs btn-outline"
                                    onClick={() => handleOpenEditLocation(loc)}
                                    title="Edit location parameters"
                                  >
                                    <i class="fa-solid fa-pen-to-square"></i>
                                  </button>
                                  <button
                                    type="button"
                                    class="btn btn-xs btn-outline text-red"
                                    onClick={() => handleDeleteLocation(loc.id, loc.label)}
                                    title="Delete location"
                                  >
                                    <i class="fa-solid fa-trash-can"></i>
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* TAB 3: MATRIX & SWEEP CAPACITY */}
              {activeTab === "capacity" && (
                <div class="target-capacity-tab">
                  <div class="capacity-hero-card">
                    <div class="capacity-metric-row">
                      <div class="capacity-metric-box">
                        <span class="metric-num">{activeQueriesCount}</span>
                        <span class="metric-lbl">Active Search Queries</span>
                      </div>
                      <div class="metric-operator">×</div>
                      <div class="capacity-metric-box">
                        <span class="metric-num">{activeLocationsCount}</span>
                        <span class="metric-lbl">Active Locations</span>
                      </div>
                      <div class="metric-operator">=</div>
                      <div class="capacity-metric-box highlight">
                        <span class="metric-num">
                          {capacity?.search_pairs ?? activeQueriesCount * activeLocationsCount}
                        </span>
                        <span class="metric-lbl">Search Pairs</span>
                      </div>
                      <div class="metric-operator">→</div>
                      <div class="capacity-metric-box">
                        <span class="metric-num">
                          {capacity?.total_cells ?? activeQueriesCount * activeLocationsCount * 2}
                        </span>
                        <span class="metric-lbl">Total Scrape Cells (Indeed + LinkedIn)</span>
                      </div>
                    </div>

                    <div class="capacity-status-banner mt-4">
                      <div class="capacity-banner-left">
                        <span class={`status-zone-pill zone-${capacity?.zone || "optimal"}`}>
                          STATUS: {capacity?.zone?.toUpperCase() || "OPTIMAL"}
                        </span>
                        <h4>Estimated Matrix Sweep Cycle: ~{capacity?.cycle_hours || 0} Hours (~{capacity?.cycle_days || 0} Days)</h4>
                        <p class="capacity-message">{capacity?.message}</p>
                      </div>
                      <div class="capacity-banner-right">
                        <button
                          type="button"
                          class="btn btn-primary"
                          onClick={handleSyncMatrix}
                          disabled={syncing}
                        >
                          <i class={`fa-solid fa-rotate ${syncing ? "fa-spin" : ""}`}></i> Force Matrix Re-sync
                        </button>
                      </div>
                    </div>
                  </div>

                  <div class="capacity-guidance-grid mt-4">
                    <div class="guidance-card">
                      <h5><i class="fa-solid fa-circle-check text-green"></i> Optimal Zone (≤ 20 Pairs)</h5>
                      <p>Full matrix swept in under 24 hours. Every newly posted job across all boards is caught at maximum freshness.</p>
                    </div>
                    <div class="guidance-card">
                      <h5><i class="fa-solid fa-triangle-exclamation text-gold"></i> Balanced Zone (21 - 40 Pairs)</h5>
                      <p>Full matrix swept in 24 - 48 hours. Jobs are found comfortably within the 3-day optimal application window.</p>
                    </div>
                    <div class="guidance-card">
                      <h5><i class="fa-solid fa-skull-crossbones text-red"></i> Overloaded Zone (&gt; 40 Pairs)</h5>
                      <p>Cycle times exceed 2 days. The scraper must rotate through a large cell backlog. Pause low-yield queries or locations.</p>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* MODAL: ADD SEARCH QUERY */}
      {showAddQueryModal && (
        <div class="retro-modal-overlay" onClick={() => setShowAddQueryModal(false)}>
          <div class="retro-modal-box" onClick={(e) => e.stopPropagation()}>
            <div class="retro-modal-header">
              <h3><i class="fa-solid fa-plus"></i> Add Search Query Term</h3>
              <button
                type="button"
                class="close-modal-btn"
                onClick={() => setShowAddQueryModal(false)}
              >
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
            <form onSubmit={handleAddQuery} class="retro-modal-body">
              <div class="form-group mb-3">
                <label class="form-label">Search Query String(s)</label>
                <textarea
                  class="form-textarea"
                  rows={3}
                  placeholder="e.g. AI Engineer, Agentic AI, Cloud Architect (Separate terms with commas or newlines)"
                  value={newQueryText}
                  onInput={(e) => setNewQueryText((e.target as HTMLTextAreaElement).value)}
                  required
                ></textarea>
                <span class="form-hint">
                  These terms are sent directly to job boards (Indeed, LinkedIn) in the scraper rotation.
                </span>
              </div>

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
                  onClick={() => setShowAddQueryModal(false)}
                >
                  Cancel
                </button>
                <button type="submit" class="btn btn-primary">
                  <i class="fa-solid fa-check"></i> Add Query Term(s)
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* MODAL: ADD / EDIT LOCATION */}
      {showAddLocationModal && (
        <div class="retro-modal-overlay" onClick={() => setShowAddLocationModal(false)}>
          <div class="retro-modal-box modal-wide" onClick={(e) => e.stopPropagation()}>
            <div class="retro-modal-header">
              <h3>
                <i class={`fa-solid ${isEditingLoc ? "fa-pen-to-square" : "fa-location-dot"}`}></i>{" "}
                {isEditingLoc ? `Edit Search Location (${locId})` : "Configure New Search Location"}
              </h3>
              <button
                type="button"
                class="close-modal-btn"
                onClick={() => setShowAddLocationModal(false)}
              >
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
            <form onSubmit={handleSaveLocation} class="retro-modal-body">
              <div class="form-grid-2col">
                <div class="form-group mb-3">
                  <label class="form-label">Location ID (Unique Slug)</label>
                  <input
                    type="text"
                    class="form-input"
                    placeholder="e.g. san_francisco or us_remote"
                    value={locId}
                    disabled={isEditingLoc}
                    onInput={(e) => setLocId((e.target as HTMLInputElement).value)}
                    required
                  />
                </div>

                <div class="form-group mb-3">
                  <label class="form-label">Display Label</label>
                  <input
                    type="text"
                    class="form-input"
                    placeholder="e.g. San Francisco, CA or United States (remote)"
                    value={locLabel}
                    onInput={(e) => setLocLabel((e.target as HTMLInputElement).value)}
                    required
                  />
                </div>

                <div class="form-group mb-3">
                  <label class="form-label">Job Board Search String</label>
                  <input
                    type="text"
                    class="form-input"
                    placeholder="e.g. San Francisco, CA (Sent to Indeed/LinkedIn search)"
                    value={locSearchLabel}
                    onInput={(e) => setLocSearchLabel((e.target as HTMLInputElement).value)}
                  />
                </div>

                <div class="form-group mb-3">
                  <label class="form-label">Country Code (ISO 2-letter)</label>
                  <input
                    type="text"
                    class="form-input"
                    placeholder="e.g. US, FI, SE, NO, DK, GB, DE, CA, etc."
                    value={locCountry}
                    maxLength={2}
                    onInput={(e) => setLocCountry((e.target as HTMLInputElement).value.toUpperCase())}
                    required
                  />
                </div>

                <div class="form-group mb-3">
                  <label class="form-label">Indeed Country Domain</label>
                  <input
                    type="text"
                    class="form-input"
                    placeholder="e.g. usa, uk, germany, canada, finland, sweden"
                    value={locIndeedCountry}
                    onInput={(e) => setLocIndeedCountry((e.target as HTMLInputElement).value.toLowerCase())}
                    required
                  />
                </div>

                <div class="form-group mb-3">
                  <label class="form-label">Search Radius Distance (Miles)</label>
                  <input
                    type="number"
                    step="5"
                    min="5"
                    max="250"
                    class="form-input"
                    value={locDistance}
                    onInput={(e) => setLocDistance(Number((e.target as HTMLInputElement).value))}
                  />
                </div>
              </div>

              <div class="form-checkbox-row mb-4">
                <label class="checkbox-label mr-4">
                  <input
                    type="checkbox"
                    checked={locIsRemote}
                    onChange={(e) => setLocIsRemote((e.target as HTMLInputElement).checked)}
                  />{" "}
                  Is Remote Location Flag
                </label>

                <label class="checkbox-label">
                  <input
                    type="checkbox"
                    checked={locEnabled}
                    onChange={(e) => setLocEnabled((e.target as HTMLInputElement).checked)}
                  />{" "}
                  Enable immediately in search matrix
                </label>
              </div>

              <div class="retro-modal-footer">
                <button
                  type="button"
                  class="btn btn-outline"
                  onClick={() => setShowAddLocationModal(false)}
                >
                  Cancel
                </button>
                <button type="submit" class="btn btn-primary">
                  <i class="fa-solid fa-floppy-disk"></i> {isEditingLoc ? "Save Location" : "Add Location"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
