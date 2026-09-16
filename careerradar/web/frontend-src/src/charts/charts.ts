// Inline SVG charts, kept framework-independent so callers control when they re-render.

import { CHART, esc } from "./palette";

function svgEl(tag: string, attrs: Record<string, string | number | undefined> = {}): SVGElement {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) el.setAttribute(key, String(value));
  }
  return el;
}

export interface MeterComponent {
  label: string;
  value: number;
  display?: string;
  tooltip?: string;
}

export interface MeterOptions {
  max?: number;
}

export function renderMeters(
  container: HTMLElement,
  components: MeterComponent[],
  options: MeterOptions = {}
): void {
  const { max = 1 } = options;
  container.innerHTML = "";
  const list = document.createElement("div");
  list.className = "meter-list";

  components.forEach((component, index) => {
    const row = document.createElement("div");
    row.className = "meter-row";
    const fraction = Math.max(0, Math.min(1, (component.value || 0) / max));

    row.innerHTML = `
      <span class="meter-label">${esc(component.label)}</span>
      <span class="meter-track" role="img"
            aria-label="${esc(component.label)} ${(fraction * 100).toFixed(0)} percent">
        <span class="meter-fill" style="width:${(fraction * 100).toFixed(1)}%;
              background:${CHART.ramp[index % CHART.ramp.length]}"></span>
      </span>
      <span class="meter-value">${esc(component.display ?? component.value.toFixed(2))}</span>`;
    row.title = component.tooltip || `${component.label}: ${component.value}`;
    list.appendChild(row);
  });

  container.appendChild(list);
}

export interface StatTile {
  value: string | number;
  label: string;
  sub?: string;
  tooltip?: string;
  muted?: boolean;
}

export function renderStatTiles(container: HTMLElement, tiles: StatTile[]): void {
  container.innerHTML = tiles
    .map(
      (tile) => `
    <div class="stat-tile ${tile.muted ? "is-muted" : ""}" title="${esc(tile.tooltip || "")}">
      <span class="stat-tile-value">${esc(tile.value)}</span>
      <span class="stat-tile-label">${esc(tile.label)}</span>
      ${tile.sub ? `<span class="stat-tile-sub">${esc(tile.sub)}</span>` : ""}
    </div>`
    )
    .join("");
}

export interface CoverageFact {
  label: string;
  value: string | number | null | undefined;
  warn?: boolean;
  tooltip?: string;
}

/* Provenance above every chart. A dashboard that states its own sampling limits gets
 * trusted; one that does not gets abandoned the first time a number looks wrong. */
export function renderCoverageStrip(container: HTMLElement, facts: CoverageFact[]): void {
  container.innerHTML = facts
    .filter((fact) => fact && fact.value !== null && fact.value !== undefined)
    .map(
      (fact) => `<span class="coverage-item ${fact.warn ? "is-warn" : ""}"
        title="${esc(fact.tooltip || "")}">
        <span class="coverage-key">${esc(fact.label)}</span>
        <span class="coverage-val">${esc(fact.value)}</span></span>`
    )
    .join("");
}

export interface YieldBarChartRow {
  label: string;
  subLabel?: string;
  totalPostings: number;
  scoredPostings: number;
  strongFits: number;
  fitRatePct: number;
  yieldCategory?: string;
}

export interface YieldBarChartOptions {
  labelWidth?: number;
  emptyText?: string;
  ariaLabel?: string;
}

export function renderYieldBarChart(
  container: HTMLElement,
  rows: YieldBarChartRow[] | null | undefined,
  options: YieldBarChartOptions = {}
): void {
  const {
    labelWidth = 200,
    emptyText = "No query yield data available",
  } = options;

  container.innerHTML = "";
  if (!rows || !rows.length) {
    container.innerHTML = `<p class="chart-empty">${esc(emptyText)}</p>`;
    return;
  }

  const width = 640;
  const plotWidth = Math.max(width - labelWidth - 140, 100);
  const rowHeight = 28;
  const gap = 8;
  const height = rows.length * (rowHeight + gap) + 12;
  const maxPostings = Math.max(...rows.map((r) => r.totalPostings || 0), 1);

  const svg = svgEl("svg", {
    class: "chart-svg",
    width: "100%",
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": options.ariaLabel || "Query Yield Chart",
  });

  rows.forEach((row, index) => {
    const y = index * (rowHeight + gap) + 6;
    const barWidth = Math.max(
      (row.totalPostings / maxPostings) * plotWidth,
      row.totalPostings > 0 ? 3 : 0
    );
    const fitWidth =
      row.totalPostings > 0 ? (row.strongFits / row.totalPostings) * barWidth : 0;
    const group = svgEl("g", { class: "chart-row" });

    // Label
    const label = svgEl("text", {
      x: labelWidth - 10,
      y: y + rowHeight / 2 + 4,
      "text-anchor": "end",
      class: "chart-label",
      style: "font-weight: 700; font-size: 11px;",
    });
    label.textContent =
      row.label.length > 26 ? row.label.slice(0, 25) + "…" : row.label;
    group.appendChild(label);

    // Full Track background
    group.appendChild(
      svgEl("rect", {
        x: labelWidth,
        y,
        width: plotWidth,
        height: rowHeight,
        rx: CHART.RADIUS,
        fill: CHART.track,
      })
    );

    if (row.totalPostings === 0) {
      const none = svgEl("text", {
        x: labelWidth + 8,
        y: y + rowHeight / 2 + 4,
        class: "chart-none",
      });
      none.textContent = "0 postings found";
      group.appendChild(none);
    } else {
      // Total Postings Bar (light monochrome / accentDim)
      group.appendChild(
        svgEl("rect", {
          x: labelWidth,
          y,
          width: Math.max(barWidth, 2),
          height: rowHeight,
          rx: CHART.RADIUS,
          fill: "rgba(0, 0, 0, 0.20)",
        })
      );

      // Strong Fits Bar overlay (solid dark black / accent)
      if (row.strongFits > 0) {
        group.appendChild(
          svgEl("rect", {
            x: labelWidth,
            y,
            width: Math.max(fitWidth, 4),
            height: rowHeight,
            rx: CHART.RADIUS,
            fill: "#000000",
          })
        );
      }

      // Value label on the right
      const valueLabel = svgEl("text", {
        x: labelWidth + barWidth + 8,
        y: y + rowHeight / 2 + 4,
        class: "chart-value",
        style: "font-size: 11px; font-weight: 700;",
      });
      const fitsText = `${row.strongFits} fit${row.strongFits === 1 ? "" : "s"}`;
      const rateText =
        row.scoredPostings > 0 ? ` (${row.fitRatePct.toFixed(1)}%)` : "";
      valueLabel.textContent = `${fitsText}${rateText} · ${row.totalPostings}p`;
      group.appendChild(valueLabel);
    }

    // Tooltip
    const tooltip = svgEl("title");
    const fitPct =
      row.scoredPostings > 0
        ? `${row.fitRatePct.toFixed(1)}% fit rate`
        : "unscored";
    tooltip.textContent = `${row.label}${
      row.subLabel ? ` [${row.subLabel}]` : ""
    }: ${row.strongFits} strong fits of ${
      row.scoredPostings
    } scored (${fitPct}) · ${row.totalPostings} total postings`;
    group.appendChild(tooltip);

    svg.appendChild(group);
  });

  container.appendChild(svg);
}
