import { Link, useLocation } from "wouter-preact";
import type { Meta } from "../../api/types";
import type { FilterQuery } from "../../lib/filterQuery";

const STATUS_OPTIONS: [string, string][] = [
  ["unread", "Unread"],
  ["saved", "Saved"],
  ["applied", "Applied"],
  ["rejected", "Rejected"],
];

const LIVENESS_OPTIONS: [string, string][] = [
  ["live", "Confirmed live"],
  ["stale", "Not re-checked recently"],
  ["unknown", "Unknown"],
  ["likely_closed", "Probably closed"],
];

const DATE_POSTED_OPTIONS: [string, string][] = [
  ["", "Any time"],
  ["24h", "Past 24 hours"],
  ["3d", "Past 3 days"],
  ["7d", "Past week"],
  ["14d", "Past 14 days"],
  ["30d", "Past month"],
];

// Ported from the `<form class="filter-sidebar">` in `templates/tabs/dashboard.html`. Every
// control here is a link/select carrying the *whole* current filter plus one change --
// `query.url()` already merges in the rest, so unlike the Jinja version there is no need
// for hidden fields to keep a `<select>`'s own `<form>` from dropping the other filters.
export function FilterSidebar({
  query,
  meta,
  fitThreshold,
  onReset,
}: {
  query: FilterQuery;
  meta: Meta | null;
  fitThreshold: number;
  onReset: () => void;
}) {
  const [, navigate] = useLocation();

  return (
    <div class="filter-sidebar">
      <div class="filter-header">
        <h3>
          <i class="fa-solid fa-filter"></i> Filters
        </h3>
        <Link class="text-btn" href="/" onClick={onReset}>
          Reset
        </Link>
      </div>

      <div class="filter-group">
        <label>Application Status</label>
        <div class="status-toggle-grid">
          {STATUS_OPTIONS.map(([value, label]) => (
            <Link
              key={value}
              class={`status-pill ${query.isActive("status", value) ? "active" : ""}`}
              href={query.url({ status: value })}
            >
              {label}
            </Link>
          ))}
        </div>
      </div>

      <div class="filter-group">
        <label for="filter-date-posted">Date Posted</label>
        <select
          id="filter-date-posted"
          class="form-select"
          value={query.date_posted}
          onChange={(e) => navigate(query.url({ date_posted: (e.target as HTMLSelectElement).value }))}
        >
          {DATE_POSTED_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>

      <div class="filter-group">
        <label for="filter-country">Country</label>
        <select
          id="filter-country"
          class="form-select"
          value={query.country}
          onChange={(e) => navigate(query.url({ country: (e.target as HTMLSelectElement).value }))}
        >
          <option value="">All Countries</option>
          {meta?.countries.map(([code, label]) => (
            <option key={code} value={code}>
              {label}
            </option>
          ))}
        </select>
      </div>

      <div class="filter-group">
        <label for="filter-location">Search Location</label>
        <select
          id="filter-location"
          class="form-select"
          value={query.location}
          onChange={(e) =>
            navigate(query.url({ location: (e.target as HTMLSelectElement).value }))
          }
        >
          <option value="">All Search Locations</option>
          {meta?.locations.map((location) => (
            <option key={location.id} value={location.id}>
              {location.label}{location.enabled ? "" : " (disabled)"}
            </option>
          ))}
        </select>
      </div>

      <div class="filter-group">
        <label for="filter-fit">Job Fit</label>
        <select
          id="filter-fit"
          class="form-select"
          value={query.fit === null || query.fit === undefined ? "" : query.fit ? "true" : "false"}
          onChange={(e) => {
            const v = (e.target as HTMLSelectElement).value;
            navigate(query.url({ fit: v === "true" ? true : v === "false" ? false : null }));
          }}
        >
          <option value="">All fits</option>
          <option value="true">Fit only (≥{fitThreshold}%)</option>
          <option value="false">No fit (&lt;{fitThreshold}%)</option>
        </select>
      </div>

      <div class="filter-group">
        <label for="filter-reason">Reason Type</label>
        <select
          id="filter-reason"
          class="form-select"
          value={query.reason_type || ""}
          onChange={(e) => navigate(query.url({ reason_type: (e.target as HTMLSelectElement).value }))}
        >
          <option value="">All reason types</option>
          {meta?.reason_types?.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>



      <div class="filter-group">
        <label for="filter-liveness">Liveness</label>
        <select
          id="filter-liveness"
          class="form-select"
          value={query.liveness}
          onChange={(e) => navigate(query.url({ liveness: (e.target as HTMLSelectElement).value }))}
        >
          <option value="">Any</option>
          {LIVENESS_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
