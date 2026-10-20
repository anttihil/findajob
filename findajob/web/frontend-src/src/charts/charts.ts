// Inline SVG charts, kept framework-independent so callers control when they re-render.

import { CHART, esc } from "./palette";

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
  options: MeterOptions = {},
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
    </div>`,
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
        <span class="coverage-val">${esc(fact.value)}</span></span>`,
    )
    .join("");
}
