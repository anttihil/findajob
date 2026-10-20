import { useEffect, useMemo, useState } from "preact/hooks";
import { getJSON } from "../../api/client";
import type {
  MarketLocationsResponse,
  MarketProgressionResponse,
  MarketYieldResponse,
  QueryYieldTuple,
} from "../../api/types";
import { useStoredPreference } from "../../lib/preferences";
import { ProgressionChart } from "./ProgressionChart";

const PAGE_SIZE = 25;

function TupleProgression({ cellId, windowDays }: { cellId: number; windowDays: number | null }) {
  const [data, setData] = useState<MarketProgressionResponse | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let current = true;
    setData(null);
    setError(false);
    const params = windowDays ? `?window_days=${windowDays}` : "";
    getJSON<MarketProgressionResponse>(`/api/market/tuples/${cellId}/progression${params}`)
      .then((result) => {
        if (current) setData(result);
      })
      .catch(() => {
        if (current) setError(true);
      });
    return () => {
      current = false;
    };
  }, [cellId, windowDays, attempt]);

  if (error) {
    return (
      <p role="alert">
        Could not load time progression.{" "}
        <button onClick={() => setAttempt(attempt + 1)}>Retry</button>
      </p>
    );
  }
  if (!data) return <p role="status">Loading time progression…</p>;
  const { totals } = data;
  return (
    <div>
      <div class="market-totals" aria-label="Tuple totals">
        <span>
          <strong>{totals.total_postings.toLocaleString()}</strong> jobs found
        </span>
        <span>
          <strong>{totals.scored_postings.toLocaleString()}</strong> scored
        </span>
        <span>
          <strong>{totals.strong_fits.toLocaleString()}</strong> strong fits
        </span>
        <span>
          {data.start_date} – {data.end_date} (UTC)
        </span>
      </div>
      <p class="card-note">
        Jobs are grouped by the day they were found. Strong fits use each job’s latest profile
        verdict, so earlier days can change as scoring completes. Today is partial. Days with no
        jobs found are shown as zero; this does not indicate whether a search ran.
      </p>
      {totals.total_postings === 0 ? (
        <p class="chart-empty">No jobs found for this tuple in this time window.</p>
      ) : (
        <ProgressionChart points={data.points} />
      )}
      <details class="market-daily-details">
        <summary>Daily counts</summary>
        <div class="market-table-wrap">
          <table class="data-table market-table">
            <caption class="sr-only">Exact daily counts by discovery date (UTC)</caption>
            <thead>
              <tr>
                <th>Date (UTC)</th>
                <th>Jobs found</th>
                <th>Scored</th>
                <th>Strong fits</th>
              </tr>
            </thead>
            <tbody>
              {data.points
                .slice()
                .reverse()
                .map((point) => (
                  <tr key={point.date}>
                    <th scope="row">{point.date}</th>
                    <td>{point.total_postings.toLocaleString()}</td>
                    <td>{point.scored_postings.toLocaleString()}</td>
                    <td>{point.strong_fits.toLocaleString()}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

export function MarketPage() {
  const [data, setData] = useState<MarketYieldResponse | null>(null);
  const [locations, setLocations] = useState<MarketLocationsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<QueryYieldTuple | null>(null);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("fits");
  const [page, setPage] = useState(0);
  const [windowDays, setWindowDays] = useStoredPreference<number | null>("market-window-days", 30);

  useEffect(() => {
    getJSON<MarketLocationsResponse>("/api/market/locations")
      .then(setLocations)
      .catch(() => setLocations(null));
  }, []);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(false);
    setData(null);
    const params = windowDays ? `?window_days=${windowDays}` : "";
    getJSON<MarketYieldResponse>(`/api/market/yield${params}`)
      .then((result) => {
        if (current) setData(result);
      })
      .catch(() => {
        if (current) setError(true);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [windowDays, attempt]);

  const locationLabel = (id: string) =>
    locations?.locations.find((loc) => loc.id === id)?.label ?? id;
  const tuples = useMemo(() => {
    const terms = search.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const labels = new Map(locations?.locations.map((loc) => [loc.id, loc.label]));
    return (data?.tuples ?? [])
      .filter((tuple) => {
        const text =
          `${tuple.query} ${tuple.source} ${tuple.location_id} ${labels.get(tuple.location_id) ?? ""}`.toLowerCase();
        return terms.every((term) => text.includes(term));
      })
      .sort((a, b) => {
        const difference =
          sort === "jobs"
            ? b.total_postings - a.total_postings
            : sort === "fits"
              ? b.strong_fits - a.strong_fits || b.total_postings - a.total_postings
              : 0;
        return (
          difference ||
          a.query.localeCompare(b.query) ||
          a.source.localeCompare(b.source) ||
          a.location_id.localeCompare(b.location_id)
        );
      });
  }, [data, search, sort, locations]);
  const lastPage = Math.max(0, Math.ceil(tuples.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const visibleTuples = tuples.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  return (
    <section class="tab-pane active market-yield-page">
      <div class="glass-card">
        <div class="card-header-row">
          <div>
            <h3>Search performance</h3>
            <p class="card-note">
              Compare jobs and strong fits for each (query term, provider, location). Select a tuple
              to see its time progression.
            </p>
          </div>
          <a class="yield-action-btn" href="/settings">
            Edit search targets
          </a>
        </div>
        <div class="yield-filter-bar market-controls">
          <div class="yield-filter-item">
            <label for="market-window">Time window</label>
            <select
              id="market-window"
              class="form-select-sm"
              value={windowDays ?? "all"}
              onChange={(event) => {
                const value = event.currentTarget.value;
                setWindowDays(value === "all" ? null : Number(value));
                setPage(0);
              }}
            >
              <option value="7">Last 7 days</option>
              <option value="14">Last 14 days</option>
              <option value="30">Last 30 days</option>
              <option value="90">Last 90 days</option>
              <option value="365">Last 365 days</option>
              <option value="all">All time</option>
            </select>
          </div>
          {!selected && (
            <>
              <div class="yield-filter-item market-search">
                <label for="market-search">Find tuples</label>
                <input
                  id="market-search"
                  class="form-control-sm"
                  type="search"
                  placeholder="Query, provider or location"
                  value={search}
                  onInput={(event) => {
                    setSearch(event.currentTarget.value);
                    setPage(0);
                  }}
                />
              </div>
              <div class="yield-filter-item">
                <label for="market-sort">Sort by</label>
                <select
                  id="market-sort"
                  class="form-select-sm"
                  value={sort}
                  onChange={(event) => {
                    setSort(event.currentTarget.value);
                    setPage(0);
                  }}
                >
                  <option value="fits">Most strong fits</option>
                  <option value="jobs">Most jobs</option>
                  <option value="query">Query term</option>
                </select>
              </div>
            </>
          )}
        </div>
        {selected ? (
          <div>
            <button class="yield-action-btn" type="button" onClick={() => setSelected(null)}>
              ← All search tuples
            </button>
            <h4 class="market-tuple-title">
              {selected.query} · {selected.source} · {locationLabel(selected.location_id)}
            </h4>
            <TupleProgression
              key={`${selected.cell_id}:${windowDays}`}
              cellId={selected.cell_id}
              windowDays={windowDays}
            />
          </div>
        ) : loading ? (
          <p role="status">Loading search tuples…</p>
        ) : error ? (
          <p role="alert">
            Could not load search tuples.{" "}
            <button onClick={() => setAttempt(attempt + 1)}>Retry</button>
          </p>
        ) : tuples.length === 0 ? (
          <p class="chart-empty">
            {search ? "No search tuples match your search." : "No search tuples available yet."}
          </p>
        ) : (
          <>
            <p class="card-note">
              {tuples.length.toLocaleString()} tuples · Jobs are counted by discovery date (UTC).
              Strong fits use the latest verdict; unscored jobs are pending.
            </p>
            <div class="market-table-wrap">
              <table class="data-table market-table market-tuple-table">
                <caption class="sr-only">Search tuples for the selected time window</caption>
                <thead>
                  <tr>
                    <th>Query term</th>
                    <th>Provider</th>
                    <th>Location</th>
                    <th>Jobs found</th>
                    <th>Scored</th>
                    <th>Strong fits</th>
                    <th>Fit rate</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleTuples.map((tuple) => (
                    <tr
                      key={tuple.cell_id}
                      class="market-tuple-row"
                      onClick={() => setSelected(tuple)}
                    >
                      <th scope="row">
                        <button
                          class="market-tuple-link"
                          type="button"
                          aria-label={`View progression: ${tuple.query}, ${tuple.source}, ${locationLabel(tuple.location_id)}`}
                          onClick={() => setSelected(tuple)}
                        >
                          {tuple.query} <span aria-hidden="true">→</span>
                        </button>
                      </th>
                      <td data-label="Provider">{tuple.source}</td>
                      <td data-label="Location">{locationLabel(tuple.location_id)}</td>
                      <td data-label="Jobs found">{tuple.total_postings.toLocaleString()}</td>
                      <td data-label="Scored">{tuple.scored_postings.toLocaleString()}</td>
                      <td data-label="Strong fits">
                        <strong>{tuple.strong_fits.toLocaleString()}</strong>
                      </td>
                      <td data-label="Fit rate">
                        {tuple.scored_postings ? `${tuple.fit_rate_pct}%` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div class="market-list-footer">
              <span class="card-note">
                Fit rate = strong fits ÷ scored jobs. A job is attributed to the tuple that found
                it.
              </span>
              {tuples.length > PAGE_SIZE && (
                <nav class="market-pagination" aria-label="Search tuple pages">
                  <button
                    class="yield-action-btn"
                    disabled={currentPage === 0}
                    onClick={() => setPage(currentPage - 1)}
                  >
                    Previous
                  </button>
                  <span>
                    {currentPage * PAGE_SIZE + 1}–
                    {Math.min((currentPage + 1) * PAGE_SIZE, tuples.length)} of {tuples.length}
                  </span>
                  <button
                    class="yield-action-btn"
                    disabled={currentPage === lastPage}
                    onClick={() => setPage(currentPage + 1)}
                  >
                    Next
                  </button>
                </nav>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
