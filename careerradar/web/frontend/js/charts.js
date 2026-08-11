/* Hand-rolled inline SVG charts.
 *
 * No chart library: every form needed here (horizontal bars, meters, a heatmap grid) is a
 * handful of <rect> elements, and a vendored library would be ~200KB to fight for control of
 * the glassmorphism styling.
 *
 * Palette validated against the dark surface with the six checks -- lightness band, chroma
 * floor, CVD separation, normal-vision floor, contrast:
 *   #a855f7 (accent) + #c08217 (censored/warning): all PASS, deutan dE 32.3
 * The gap-component meters use a single-hue SEQUENTIAL ramp rather than categorical hues,
 * because the three components are parts of one composite score and each is directly
 * labelled -- so color is not carrying identity, and a purple/blue categorical pair would
 * have been indistinguishable under deuteranopia (dE 0.9).
 */

const CHART = {
  accent: '#a855f7',
  accentDim: 'rgba(168, 85, 247, 0.28)',
  censored: '#c08217',
  // Sequential steps, monotonically decreasing lightness.
  ramp: ['#c4a5fb', '#a855f7', '#7c3aed'],
  track: 'rgba(255,255,255,0.06)',
  grid: 'rgba(255,255,255,0.07)',
  BAR_H: 18,
  BAR_GAP: 12,
  RADIUS: 4,
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) el.setAttribute(key, value);
  }
  return el;
}

/* Rounded only on the data end, anchored to the baseline. */
function barPath(x, y, width, height, radius) {
  const r = Math.max(0, Math.min(radius, width, height / 2));
  if (r === 0 || width <= 0) return `M${x},${y}h${Math.max(width, 0)}v${height}h${-Math.max(width, 0)}z`;
  return `M${x},${y}h${width - r}a${r},${r} 0 0 1 ${r},${r}v${height - 2 * r}`
       + `a${r},${r} 0 0 1 ${-r},${r}h${-(width - r)}z`;
}

/* ------------------------------------------------------------------ horizontal bars */
/* Role supply. One series, so no legend -- the title names it.
 * A censored value is a LOWER BOUND (the board truncated the result set), so it gets an
 * arrow cap and a ">=" label instead of an ordinary bar end. Drawing it as a normal bar
 * would state a measurement we do not have. */
function renderBarChart(container, rows, options = {}) {
  const {
    valueKey = 'value', labelKey = 'label', formatValue = (v) => v.toFixed(1),
    labelWidth = 190, unit = '', emptyText = 'No data yet',
  } = options;

  container.innerHTML = '';
  if (!rows || !rows.length) {
    container.innerHTML = `<p class="chart-empty">${esc(emptyText)}</p>`;
    return;
  }

  // Fixed design width rather than container.clientWidth: measuring before CSS grid has
  // settled gave the first panel a viewBox far wider than its box, scaling its text down to
  // unreadable while every later panel measured correctly. The SVG is width:100%, so a
  // constant viewBox scales cleanly and renders every panel identically.
  const width = 560;
  const plotWidth = Math.max(width - labelWidth - 92, 80);
  const height = rows.length * (CHART.BAR_H + CHART.BAR_GAP) + 8;
  const max = Math.max(...rows.map((r) => Number(r[valueKey]) || 0), 0.0001);

  const svg = svgEl('svg', {
    class: 'chart-svg', width: '100%', height,
    viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': options.ariaLabel || 'Bar chart',
  });

  rows.forEach((row, index) => {
    const y = index * (CHART.BAR_H + CHART.BAR_GAP) + 4;
    const value = Number(row[valueKey]) || 0;
    const barWidth = Math.max((value / max) * plotWidth, value > 0 ? 3 : 0);
    const censored = Boolean(row.censored);
    const zeroYield = Boolean(row.zero_yield);
    const group = svgEl('g', { class: 'chart-row' });

    const label = svgEl('text', {
      x: labelWidth - 10, y: y + CHART.BAR_H / 2 + 4,
      'text-anchor': 'end', class: 'chart-label',
    });
    label.textContent = row[labelKey];
    group.appendChild(label);

    group.appendChild(svgEl('rect', {
      x: labelWidth, y, width: plotWidth, height: CHART.BAR_H,
      rx: CHART.RADIUS, fill: CHART.track,
    }));

    if (zeroYield) {
      // Scraped, nothing on-topic returned. Distinct from a measured low value: a
      // zero-length bar would read as "low demand" rather than "nothing found".
      const none = svgEl('text', {
        x: labelWidth + 8, y: y + CHART.BAR_H / 2 + 4, class: 'chart-none',
      });
      none.textContent = 'none observed';
      group.appendChild(none);
    } else {
      group.appendChild(svgEl('path', {
        d: barPath(labelWidth, y, barWidth, CHART.BAR_H, CHART.RADIUS),
        fill: censored ? CHART.censored : CHART.accent,
      }));
      if (censored) {
        // Open arrow cap: the true value lies beyond this point.
        const tip = labelWidth + barWidth;
        group.appendChild(svgEl('path', {
          d: `M${tip + 2},${y + 2}L${tip + 10},${y + CHART.BAR_H / 2}`
           + `L${tip + 2},${y + CHART.BAR_H - 2}`,
          fill: 'none', stroke: CHART.censored, 'stroke-width': 2,
          'stroke-linecap': 'round', 'stroke-linejoin': 'round',
        }));
      }
      const valueLabel = svgEl('text', {
        x: labelWidth + barWidth + (censored ? 18 : 8),
        y: y + CHART.BAR_H / 2 + 4, class: 'chart-value',
      });
      valueLabel.textContent = `${censored ? '≥' : ''}${formatValue(value)}${unit}`;
      group.appendChild(valueLabel);
    }

    const tooltip = svgEl('title');
    tooltip.textContent = buildTooltip(row, formatValue, unit);
    group.appendChild(tooltip);

    svg.appendChild(group);
  });

  container.appendChild(svg);
}

function buildTooltip(row, formatValue, unit) {
  const parts = [row.label];
  if (row.zero_yield) parts.push('no on-topic postings observed');
  else if (row.value !== undefined || row.flow_per_day !== undefined) {
    const value = row.value ?? row.flow_per_day;
    parts.push(`${row.censored ? 'at least ' : ''}${formatValue(value)}${unit}`);
  }
  if (row.n_postings !== undefined) parts.push(`${row.n_postings} postings`);
  if (row.n_companies !== undefined) parts.push(`${row.n_companies} companies`);
  if (row.coverage_fraction !== undefined) {
    parts.push(`window coverage ${Math.round(row.coverage_fraction * 100)}%`);
  }
  if (row.censored) parts.push('result set truncated — lower bound only');
  return parts.filter(Boolean).join(' · ');
}

/* ------------------------------------------------------------------------- meters */
/* Component meters for one skill. Sequential ramp + a direct label per row, so identity
 * never rests on color alone. */
function renderMeters(container, components, options = {}) {
  const { max = 1 } = options;
  container.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'meter-list';

  components.forEach((component, index) => {
    const row = document.createElement('div');
    row.className = 'meter-row';
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

/* --------------------------------------------------------------------- stat tiles */
function renderStatTiles(container, tiles) {
  container.innerHTML = tiles.map((tile) => `
    <div class="stat-tile ${tile.muted ? 'is-muted' : ''}" title="${esc(tile.tooltip || '')}">
      <span class="stat-tile-value">${esc(tile.value)}</span>
      <span class="stat-tile-label">${esc(tile.label)}</span>
      ${tile.sub ? `<span class="stat-tile-sub">${esc(tile.sub)}</span>` : ''}
    </div>`).join('');
}

/* ------------------------------------------------------------------ heatmap grid */
/* Role family x location. Sequential single hue: magnitude, not identity. */
function renderHeatmap(container, matrix, options = {}) {
  const { rowLabels = [], colLabels = [], formatValue = (v) => v.toFixed(1) } = options;
  container.innerHTML = '';
  if (!matrix.length) {
    container.innerHTML = '<p class="chart-empty">No data yet</p>';
    return;
  }

  const values = matrix.flat().filter((v) => v !== null && v !== undefined);
  const max = Math.max(...values, 0.0001);

  const table = document.createElement('table');
  table.className = 'heatmap';
  const head = document.createElement('thead');
  head.innerHTML = `<tr><th></th>${colLabels
    .map((c) => `<th>${esc(c)}</th>`).join('')}</tr>`;
  table.appendChild(head);

  const body = document.createElement('tbody');
  matrix.forEach((row, rowIndex) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<th scope="row">${esc(rowLabels[rowIndex] || '')}</th>`;
    row.forEach((value, colIndex) => {
      const cell = document.createElement('td');
      if (value === null || value === undefined) {
        cell.className = 'heat-empty';
        cell.title = `${rowLabels[rowIndex]} / ${colLabels[colIndex]}: not scraped`;
        cell.textContent = '·';
      } else {
        // Alpha carries magnitude; the number is also printed, so the encoding is not
        // color-alone.
        const alpha = 0.12 + 0.78 * (value / max);
        cell.style.background = `rgba(168, 85, 247, ${alpha.toFixed(3)})`;
        cell.textContent = value > 0 ? formatValue(value) : '0';
        cell.title = `${rowLabels[rowIndex]} / ${colLabels[colIndex]}: `
                   + `${formatValue(value)}/day`;
      }
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  container.appendChild(table);
}

/* ------------------------------------------------------------- coverage strip */
/* Provenance above every chart. A dashboard that states its own sampling limits gets
 * trusted; one that does not gets abandoned the first time a number looks wrong. */
function renderCoverageStrip(container, facts) {
  container.innerHTML = facts
    .filter((fact) => fact && fact.value !== null && fact.value !== undefined)
    .map((fact) => `<span class="coverage-item ${fact.warn ? 'is-warn' : ''}"
        title="${esc(fact.tooltip || '')}">
        <span class="coverage-key">${esc(fact.label)}</span>
        <span class="coverage-val">${esc(fact.value)}</span></span>`)
    .join('');
}

window.Charts = {
  renderBarChart, renderMeters, renderStatTiles, renderHeatmap, renderCoverageStrip,
  palette: CHART, esc,
};
