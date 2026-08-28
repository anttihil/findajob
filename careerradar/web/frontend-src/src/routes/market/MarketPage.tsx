import { useEffect, useRef, useState } from "preact/hooks";
import { getJSON } from "../../api/client";
import type {
  CoverageCell,
  MarketCoverageResponse,
  MarketLocation,
  MarketLocationsResponse,
  MarketSupplyResponse,
} from "../../api/types";
import { renderBarChart, renderCoverageStrip, renderHeatmap } from "../../charts/charts";
import { esc } from "../../charts/palette";

interface PanelResult {
  loc: MarketLocation;
  data?: MarketSupplyResponse;
  error?: string;
}

function MarketPanel({ result }: { result: PanelResult }) {
  const stripRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!result.data) {
      if (chartRef.current) {
        chartRef.current.innerHTML = `<p class="chart-empty">${esc(result.error)}</p>`;
      }
      return;
    }
    const published = result.data.rows.filter((r) => !r.suppressed_reason);
    const p = result.data.provenance;
    const censoredCount = published.filter((r) => r.censored).length;

    if (stripRef.current) {
      renderCoverageStrip(stripRef.current, [
        { label: "shown", value: `${p.published_rows}/${p.total_rows}` },
        {
          label: "truncated",
          value: censoredCount ? `${censoredCount} lower-bound` : "none",
          warn: censoredCount > 0,
          tooltip: "Result set truncated by the job board; value represents a lower bound.",
        },
        {
          label: "window",
          value: `${p.window_days}d`,
          warn: p.window_below_minimum,
          tooltip: p.window_below_minimum
            ? `Below ${p.min_window_days}-day minimum; shorter windows have partial scrape coverage.`
            : "",
        },
      ]);
    }

    if (chartRef.current) {
      renderBarChart(
        chartRef.current,
        published.map((r) => ({
          label: r.label,
          value: r.flow_per_day || 0,
          censored: r.censored,
          zero_yield: r.zero_yield,
          n_postings: r.n_postings,
          n_companies: r.n_companies,
          coverage_fraction: r.coverage_fraction,
        })),
        {
          unit: "/day",
          formatValue: (v) => v.toFixed(1),
          labelWidth: 170,
          emptyText: "Nothing published for this location yet.",
          ariaLabel: `Role supply in ${result.loc.label}`,
        }
      );
    }
  }, [result]);

  const suppressed = result.data ? result.data.rows.filter((r) => r.suppressed_reason) : [];

  return (
    <div class="market-panel">
      <div class="market-panel-head">
        <h4>
          {result.loc.label}
          {result.loc.is_remote ? " · remote" : ""}
        </h4>
        <span class="market-panel-meta">{result.loc.country}</span>
      </div>
      <div class="coverage-strip" ref={stripRef}></div>
      <div ref={chartRef}></div>
      {suppressed.length > 0 && (
        <div class="market-suppressed">
          <span class="suppressed-note">
            {suppressed.length} suppressed:{" "}
            {suppressed.map((r) => `${r.label} (${r.suppressed_reason})`).join(", ")}
          </span>
        </div>
      )}
    </div>
  );
}

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
              <th>Role family</th>
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
                  <td>{c.role_family}</td>
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

// Ported from `templates/tabs/market.html` + `frontend/js/features/market.js`.
export function MarketPage() {
  const [source, setSource] = useState("indeed");
  const [windowDays, setWindowDays] = useState(14);
  const [locations, setLocations] = useState<MarketLocationsResponse | null>(null);
  const [results, setResults] = useState<PanelResult[] | null>(null);
  const [coverageCells, setCoverageCells] = useState<CoverageCell[] | null>(null);
  const heatmapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getJSON<MarketLocationsResponse>("/api/market/locations")
      .then(setLocations)
      .catch(() => setLocations({ locations: [], role_families: [] }));
  }, []);

  useEffect(() => {
    getJSON<MarketCoverageResponse>("/api/market/coverage")
      .then((data) => setCoverageCells(data.cells))
      .catch(() => setCoverageCells([]));
  }, []);

  useEffect(() => {
    if (!locations) return;
    let cancelled = false;
    setResults(null);
    Promise.all(
      locations.locations.map(async (loc): Promise<PanelResult> => {
        try {
          const data = await getJSON<MarketSupplyResponse>(
            `/api/market/supply?location=${encodeURIComponent(loc.id)}` +
              `&source=${encodeURIComponent(source)}&window_days=${windowDays}`
          );
          return { loc, data };
        } catch (e) {
          return { loc, error: e instanceof Error ? e.message : String(e) };
        }
      })
    ).then((r) => {
      if (!cancelled) setResults(r);
    });
    return () => {
      cancelled = true;
    };
  }, [locations, source, windowDays]);

  // The heatmap reuses the same per-location fetches the panels above already made,
  // rather than the old JS's second round of identical `/api/market/supply` requests.
  useEffect(() => {
    if (!heatmapRef.current || !locations || !results) return;
    const perLocation = results.map((r) => {
      const map: Record<string, number> = {};
      r.data?.rows.forEach((row) => {
        if (!row.suppressed_reason) map[row.role_family] = row.flow_per_day || 0;
      });
      return map;
    });
    const families = locations.role_families.filter((f) =>
      perLocation.some((m) => m[f.key] !== undefined)
    );
    if (!families.length) {
      heatmapRef.current.innerHTML = '<p class="chart-empty">Not enough coverage yet.</p>';
      return;
    }
    renderHeatmap(
      heatmapRef.current,
      families.map((f) => perLocation.map((m) => (m[f.key] === undefined ? null : m[f.key]))),
      {
        rowLabels: families.map((f) => f.label),
        colLabels: locations.locations.map((l) => l.id),
        formatValue: (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1)),
      }
    );
  }, [locations, results]);

  const anyData = results?.some((r) => r.data?.rows.some((row) => !row.suppressed_reason));

  return (
    <section class="tab-pane active">
      <div class="glass-card">
        <div class="card-header-row">
          <h3>
            <i class="fa-solid fa-chart-simple text-purple"></i> Role supply
          </h3>
          <div class="inline-controls">
            <select
              class="form-select-sm"
              value={source}
              onChange={(e) => setSource((e.target as HTMLSelectElement).value)}
            >
              <option value="indeed">Indeed</option>
              <option value="linkedin">LinkedIn</option>
            </select>
            <select
              class="form-select-sm"
              value={String(windowDays)}
              onChange={(e) => setWindowDays(Number((e.target as HTMLSelectElement).value))}
            >
              <option value="14">Last 14 days</option>
              <option value="30">Last 30 days</option>
              <option value="90">Last 90 days</option>
            </select>
          </div>
        </div>
        <p class="card-note">
          New postings per day. Bars marked "≥" are <strong>lower bounds</strong> due to job board result truncation.
        </p>
        <p class="card-note">
          Flow rates are comparable only within the same location and source.
        </p>
        <div class="market-panels">
          {results === null && <p class="chart-empty">Loading…</p>}
          {results && !anyData && (
            <p class="cold-start-note">
              No supply data yet. Run scraper syncs to populate coverage.
            </p>
          )}
          {results?.map((r) => (
            <MarketPanel key={r.loc.id} result={r} />
          ))}
        </div>
      </div>

      <div class="glass-card">
        <h3>
          <i class="fa-solid fa-table-cells text-purple"></i> Role family × location
        </h3>
        <p class="card-note">
          Postings per day. Dots indicate unscraped cells.
        </p>
        <div class="heatmap-wrap" ref={heatmapRef}></div>
      </div>

      <div class="glass-card">
        <div class="card-header-row">
          <h3>
            <i class="fa-solid fa-satellite-dish text-purple"></i> Scrape coverage
          </h3>
          {coverageCells && (
            <span class="results-count">
              {coverageCells.filter((c) => c.total_scrapes > 0).length}/{coverageCells.length} cells
              visited · {coverageCells.filter((c) => c.total_scrapes > 0 && (c.hours_since_success ?? 1e6) > 96).length}{" "}
              stale · {coverageCells.filter((c) => c.consecutive_error > 0).length} erroring
            </span>
          )}
        </div>
        <p class="card-note">Per-cell scraping status and error history.</p>
        <div class="coverage-table-wrap">
          <CoverageTable cells={coverageCells} />
        </div>
      </div>
    </section>
  );
}
