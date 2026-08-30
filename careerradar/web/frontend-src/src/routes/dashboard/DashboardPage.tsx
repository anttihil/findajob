import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Link, useLocation, useSearch } from "wouter-preact";
import { getJSON, guard, putJSON, reportError } from "../../api/client";
import type {
  JobContext,
  JobStatus,
  JobsPage,
  Meta,
  Stats,
} from "../../api/types";
import { JobCard } from "../../components/JobCard";
import { Pagination } from "../../components/Pagination";
import { FilterQuery } from "../../lib/filterQuery";
import { setSyncFinishedListener, syncStatus } from "../../state/sync";
import { stats } from "../../state/stats";
import { FilterSidebar } from "./FilterSidebar";
import { JobDrawer } from "./JobDrawer";
import { newJobsPendingCount } from "../../state/liveEvents";
import { SearchTargetsWidget } from "../../components/SearchTargetsWidget";
import { ImportJobModal } from "../../components/ImportJobModal";

const SYNC_ERROR_ICONS: Record<string, string> = {
  error: "fa-circle-exclamation",
  warning: "fa-triangle-exclamation",
  info: "fa-circle-info",
};

// Ported from `templates/tabs/dashboard.html` + `app.py::dashboard/drawer/set_job_status_form`.
//
// The Jinja version rendered the feed, stats and drawer from the request's query string on
// every navigation -- no client-side job list to keep in step with anything. This version
// keeps that same single source of truth (the URL, parsed fresh into a `FilterQuery` on
// every render) but now owns a client-side copy of the feed page and the open drawer's
// content, updated locally after a status change instead of the old `hx-swap-oob` fragments.
export function DashboardPage() {
  const search = useSearch();
  const [, navigate] = useLocation();
  const query = useMemo(() => new FilterQuery(search), [search]);
  const filterKey = query.asApiParams().toString();
  const openJobId = useMemo(() => {
    const id = new URLSearchParams(search).get("job");
    return id ? Number(id) : null;
  }, [search]);

  const [meta, setMeta] = useState<Meta | null>(null);
  const [jobsPage, setJobsPage] = useState<JobsPage | null>(null);
  const [drawerContext, setDrawerContext] = useState<JobContext | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [dismissedErrors, setDismissedErrors] = useState<unknown>(null);
  const [searchTerm, setSearchTerm] = useState(query.q);
  const [isImportModalOpen, setIsImportModalOpen] = useState(false);
  const debounceTimerRef = useRef<number | null>(null);

  // Sync search input if URL changed externally (e.g. navigation or reset)
  useEffect(() => {
    setSearchTerm(query.q);
  }, [query.q]);

  const triggerSearch = (val: string) => {
    if (debounceTimerRef.current !== null) {
      window.clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    const cleanVal = val.trim();
    if (cleanVal !== query.q) {
      navigate(query.url({ q: cleanVal }));
    }
  };

  const handleSearchChange = (val: string) => {
    setSearchTerm(val);
    if (debounceTimerRef.current !== null) {
      window.clearTimeout(debounceTimerRef.current);
    }
    debounceTimerRef.current = window.setTimeout(() => {
      triggerSearch(val);
    }, 250);
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      triggerSearch(searchTerm);
    } else if (e.key === "Escape") {
      handleClearSearch();
    }
  };

  const handleClearSearch = () => {
    if (debounceTimerRef.current !== null) {
      window.clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    setSearchTerm("");
    if (query.q) {
      navigate(query.url({ q: "" }));
    }
  };

  useEffect(() => {
    return () => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    getJSON<Meta>("/api/meta")
      .then(setMeta)
      .catch(() => {});
  }, []);

  useEffect(() => {
    guard("Loading stats", () => getJSON<Stats>("/api/stats")).then((d) => {
      if (d) stats.value = d;
    });
  }, [refreshKey]);

  useEffect(() => {
    // A finished scrape means the feed on screen may be stale -- refetch it rather than
    // the old version's unconditional `window.location.reload()`.
    setSyncFinishedListener(() => setRefreshKey((k) => k + 1));
    return () => setSyncFinishedListener(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setJobsPage(null);
    guard("Loading jobs", () =>
      getJSON<JobsPage>(`/api/jobs?${filterKey}`),
    ).then((data) => {
      if (!cancelled && data) setJobsPage(data);
    });
    return () => {
      cancelled = true;
    };
  }, [filterKey, refreshKey]);

  useEffect(() => {
    if (openJobId == null) {
      setDrawerContext(null);
      return;
    }
    let cancelled = false;
    guard(`Loading job ${openJobId}`, () =>
      getJSON<JobContext>(`/api/jobs/${openJobId}/context?${filterKey}`),
    ).then((data) => {
      if (!cancelled && data) setDrawerContext(data);
    });
    return () => {
      cancelled = true;
    };
  }, [openJobId, filterKey]);

  async function handleStatusChange(jobId: number, newStatus: JobStatus) {
    const prevStatus =
      jobsPage?.jobs.find((j) => j.id === jobId)?.status ??
      drawerContext?.job?.status;
    try {
      await putJSON(`/api/jobs/${jobId}/status`, { status: newStatus });
    } catch (err) {
      reportError("Updating job status", err);
      return;
    }

    if (stats.value && prevStatus) {
      const counts = {
        ...stats.value.status_counts,
        [prevStatus]: stats.value.status_counts[prevStatus] - 1,
      };
      counts[newStatus] = (counts[newStatus] ?? 0) + 1;
      stats.value = { ...stats.value, status_counts: counts };
    }

    // Refresh stats in background to keep all counters (including strong matches) synchronized
    getJSON<Stats>("/api/stats")
      .then((d) => {
        if (d) stats.value = d;
      })
      .catch(() => {});


    setJobsPage((prev) => {
      if (!prev) return prev;
      if (newStatus !== query.status) {
        return {
          ...prev,
          jobs: prev.jobs.filter((j) => j.id !== jobId),
          total: Math.max(0, prev.total - 1),
        };
      }
      return {
        ...prev,
        jobs: prev.jobs.map((j) =>
          j.id === jobId ? { ...j, status: newStatus } : j,
        ),
      };
    });

    const nextId = drawerContext?.next_job_id ?? null;
    navigate(nextId ? query.withJob(nextId) : query.withoutJob());
  }

  const currentSyncErrors = syncStatus.value?.errors;
  const showSyncErrors =
    currentSyncErrors &&
    currentSyncErrors.length > 0 &&
    dismissedErrors !== currentSyncErrors;

  return (
    <section class="tab-pane active">
      {/* Top Stats Cards Grid (Monochrome 1983 Style) */}
      <div class="stats-grid">
        <Link
          href={query.url({ status: "unread", fit: null })}
          class={`stat-card ${query.status === "unread" && query.fit === null ? "active" : ""}`}
        >
          <div class="stat-icon">
            <i class="fa-solid fa-magnifying-glass"></i>
          </div>
          <div class="stat-info">
            <span class="stat-label">Total Postings</span>
            <h3>{stats.value?.total_jobs ?? 0}</h3>
          </div>
        </Link>
        <Link
          href={query.url({ status: "saved", fit: null })}
          class={`stat-card ${query.status === "saved" ? "active" : ""}`}
        >
          <div class="stat-icon">
            <i class="fa-solid fa-bookmark"></i>
          </div>
          <div class="stat-info">
            <span class="stat-label">Saved Matches</span>
            <h3>{stats.value?.status_counts.saved ?? 0}</h3>
          </div>
        </Link>
        <Link
          href={query.url({ status: "applied", fit: null })}
          class={`stat-card ${query.status === "applied" ? "active" : ""}`}
        >
          <div class="stat-icon">
            <i class="fa-solid fa-paper-plane"></i>
          </div>
          <div class="stat-info">
            <span class="stat-label">Applications</span>
            <h3>{stats.value?.status_counts.applied ?? 0}</h3>
          </div>
        </Link>
        <Link
          href={query.url({ fit: query.fit === true ? null : true })}
          class={`stat-card ${query.fit === true ? "active" : ""}`}
        >
          <div class="stat-icon">
            <i class="fa-solid fa-star"></i>
          </div>
          <div class="stat-info">
            <span class="stat-label">Strong Fit</span>
            <h3>{stats.value?.strong_matches ?? 0}</h3>
          </div>
        </Link>
      </div>


      {showSyncErrors && currentSyncErrors && (
        <div class="sync-error-banner glass-card mt-4">
          <div class="banner-title">
            <span>
              <i class="fa-solid fa-triangle-exclamation text-gold"></i> API
              Sync Warnings
            </span>
            <button
              class="close-banner-btn"
              onClick={() => setDismissedErrors(currentSyncErrors)}
            >
              <i class="fa-solid fa-xmark"></i>
            </button>
          </div>
          <ul class="sync-errors-list">
            {currentSyncErrors.map((err, i) => {
              const severity = err.severity || "error";
              const timeStr = err.timestamp
                ? new Date(err.timestamp).toLocaleTimeString()
                : "Unknown";
              return (
                <li key={i} class={`sync-error-${severity}`}>
                  <i
                    class={`fa-solid ${SYNC_ERROR_ICONS[severity] || SYNC_ERROR_ICONS.error}`}
                  ></i>
                  <strong>{err.source}</strong> [{timeStr}]: {err.error}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Search Matrix & Target Queries / Locations Widget */}
      <SearchTargetsWidget onTargetsChanged={() => setRefreshKey((k) => k + 1)} />

      {/* Main Content Grid: Recent Posts (Left) & Connections/Filters (Right) */}
      <div class="feed-layout mt-4">
        {/* Left Column: Recent Posts / Matched Postings */}
        <div class="feed-main">
          <div class="panel-section-box">
            <div class="panel-section-header">RECENT POSTS</div>
            <div class="panel-section-body">
              <div class="feed-search-bar-wrap" style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <div class="feed-search-input-box" style={{ flex: 1 }}>
                  <i class="fa-solid fa-magnifying-glass search-icon"></i>
                  <input
                    type="text"
                    class="feed-search-input"
                    placeholder="Search jobs by title, company, skills, or location..."
                    value={searchTerm}
                    onInput={(e) =>
                      handleSearchChange((e.target as HTMLInputElement).value)
                    }
                    onKeyDown={handleKeyDown}
                    aria-label="Search jobs"
                  />
                  {searchTerm && (
                    <button
                      type="button"
                      class="search-clear-btn"
                      onClick={handleClearSearch}
                      title="Clear search"
                      aria-label="Clear search"
                    >
                      <i class="fa-solid fa-xmark"></i>
                    </button>
                  )}
                </div>
                <button
                  type="button"
                  class="btn btn-primary"
                  onClick={() => setIsImportModalOpen(true)}
                  style={{ whiteSpace: "nowrap", display: "flex", alignItems: "center", gap: "6px" }}
                  title="Import a job posting from any URL"
                >
                  <i class="fa-solid fa-plus"></i> Import URL
                </button>
              </div>

              <div class="feed-header">
                <span class="results-count">
                  {jobsPage
                    ? `DISPLAYING ${jobsPage.jobs.length} OF ${jobsPage.total} MATCHING POSITIONS`
                    : "LOADING INTELLIGENCE..."}
                </span>
              </div>

              {newJobsPendingCount.value > 0 && (
                <div
                  class="sync-error-banner glass-card"
                  style={{ cursor: "pointer", marginBottom: "1rem" }}
                  onClick={() => {
                    newJobsPendingCount.value = 0;
                    setRefreshKey((k) => k + 1);
                  }}
                >
                  <div class="banner-title">
                    <span>
                      <i class="fa-solid fa-bolt text-gold"></i> New updates scored
                      in background. Click to refresh feed.
                    </span>
                    <button type="button" class="btn btn-sm btn-primary">
                      Refresh
                    </button>
                  </div>
                </div>
              )}

              <div class="job-cards-grid">
                {jobsPage === null && <p class="chart-empty">Loading…</p>}
                {jobsPage?.jobs.length === 0 && (
                  <div class="no-jobs-card">
                    <i class="fa-solid fa-binoculars"></i>
                    <h3>No matching jobs found</h3>
                    <p>
                      Try adjusting your filters, triggering a new database sync, or
                      relaxing your match threshold.
                    </p>
                  </div>
                )}
                {jobsPage?.jobs.map((job) => (
                  <JobCard key={job.id} job={job} query={query} />
                ))}
              </div>

              {jobsPage && (
                <div class="feed-footer-actions">
                  <Pagination
                    query={query}
                    total={jobsPage.total}
                    hasMore={jobsPage.has_more}
                  />
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Right Column: Filter Controls / Connections */}
        <div class="feed-sidebar-col">
          <FilterSidebar query={query} meta={meta} />
        </div>
      </div>

      {/* System Message Box (NETLINK 1983 Style) */}
      <div class="retro-system-message-box mt-4">
        <div class="system-message-header">SYSTEM MESSAGE</div>
        <div class="system-message-body">
          NETLINK will be undergoing scheduled maintenance on 04-25-83 from 01:00 to 03:00 EST.
          <br />
          During this time, the network may be unavailable. Thank you.
        </div>
      </div>

      <JobDrawer
        context={drawerContext}
        onClose={() => navigate(query.withoutJob())}
        onStatusChange={handleStatusChange}
      />

      <ImportJobModal
        isOpen={isImportModalOpen}
        onClose={() => setIsImportModalOpen(false)}
        onJobImported={(jobId) => {
          setRefreshKey((k) => k + 1);
          navigate(query.withJob(jobId));
        }}
      />
    </section>
  );
}

