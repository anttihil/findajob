import { Link, useLocation } from "wouter-preact";
import type { Meta } from "../../api/types";
import type { FilterQuery } from "../../lib/filterQuery";

const STATUS_OPTIONS: [string, string][] = [
  ["unread", "Unread"],
  ["saved", "Saved"],
  ["applied", "Applied"],
  ["rejected", "Rejected"],
];

const ACCESS_OPTIONS: [string, string, string][] = [
  ["", "All", "Every posting"],
  ["commutable", "LA area", "Within commuting distance of Los Angeles — no relocation, no remote arrangement needed"],
  ["remote", "Remote", "Remote, so location is not a constraint"],
  ["relocation", "Relocate", "Onsite somewhere you would have to move to"],
];

const LIVENESS_OPTIONS: [string, string][] = [
  ["live", "Confirmed live"],
  ["stale", "Not re-checked recently"],
  ["unknown", "Unknown"],
  ["likely_closed", "Probably closed"],
];

// Ported from the `<form class="filter-sidebar">` in `templates/tabs/dashboard.html`. Every
// control here is a link/select carrying the *whole* current filter plus one change --
// `query.url()` already merges in the rest, so unlike the Jinja version there is no need
// for hidden fields to keep a `<select>`'s own `<form>` from dropping the other filters.
export function FilterSidebar({ query, meta }: { query: FilterQuery; meta: Meta | null }) {
  const [, navigate] = useLocation();

  return (
    <div class="filter-sidebar">
      <div class="filter-header">
        <h3>
          <i class="fa-solid fa-filter"></i> Filters
        </h3>
        <Link class="text-btn" href="/">
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
        <label>Reachability</label>
        <div class="status-toggle-grid access-toggle-grid">
          {ACCESS_OPTIONS.map(([value, label, tip]) => (
            <Link
              key={value}
              class={`status-pill ${query.isActive("access", value) ? "active" : ""}`}
              href={query.url({ access: value })}
              title={tip}
            >
              {label}
            </Link>
          ))}
        </div>
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
        <label for="filter-tier">Pareto tier</label>
        <select
          id="filter-tier"
          class="form-select"
          value={query.max_tier ?? ""}
          onChange={(e) => {
            const v = (e.target as HTMLSelectElement).value;
            navigate(query.url({ max_tier: v ? Number(v) : null }));
          }}
        >
          <option value="">Any tier</option>
          {Array.from({ length: 10 }, (_, i) => i + 1).map((tier) => (
            <option key={tier} value={tier}>
              Tier {tier} and better
            </option>
          ))}
        </select>
      </div>

      <div class="filter-group">
        <label for="filter-verdict">Verdict</label>
        <select
          id="filter-verdict"
          class="form-select"
          value={query.verdict}
          onChange={(e) => navigate(query.url({ verdict: (e.target as HTMLSelectElement).value }))}
        >
          <option value="">Any verdict</option>
          {meta?.verdicts.map(([value, label]) => (
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
