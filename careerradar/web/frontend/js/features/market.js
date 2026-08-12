// Market supply panels, heatmap and scrape-coverage table.
//
// Each loader writes its own failure into the panel it owns ('Could not load
// coverage.'), which is why these do not go through api.js -- the message lands
// where the missing chart would have been.

let marketLocations = null;

export async function loadMarketTab() {
    const panels = document.getElementById('market-panels');
    const source = document.getElementById('market-source').value;
    const windowDays = Number(document.getElementById('market-window').value);

    if (!marketLocations) {
        try {
            const res = await fetch('/api/market/locations');
            marketLocations = await res.json();
        } catch (e) {
            panels.innerHTML = '<p class="chart-empty">Could not load locations.</p>';
            return;
        }
    }

    panels.innerHTML = '<p class="chart-empty">Loading…</p>';

    const results = await Promise.all(
        marketLocations.locations.map(async (loc) => {
            try {
                const res = await fetch(
                    `/api/market/supply?location=${encodeURIComponent(loc.id)}`
                    + `&source=${encodeURIComponent(source)}&window_days=${windowDays}`
                );
                if (!res.ok) return { loc, error: `HTTP ${res.status}` };
                return { loc, data: await res.json() };
            } catch (e) {
                return { loc, error: String(e) };
            }
        })
    );

    panels.innerHTML = '';
    let anyData = false;

    results.forEach(({ loc, data, error }) => {
        const panel = document.createElement('div');
        panel.className = 'market-panel';

        const published = data
            ? data.rows.filter((r) => !r.suppressed_reason)
            : [];
        const suppressed = data
            ? data.rows.filter((r) => r.suppressed_reason)
            : [];
        if (published.length) anyData = true;

        panel.innerHTML = `
            <div class="market-panel-head">
                <h4>${Charts.esc(loc.label)}${loc.is_remote ? ' · remote' : ''}</h4>
                <span class="market-panel-meta">${Charts.esc(loc.country)}</span>
            </div>
            <div class="coverage-strip" data-strip></div>
            <div data-chart></div>
            <div class="market-suppressed" data-suppressed></div>`;
        panels.appendChild(panel);

        if (error) {
            panel.querySelector('[data-chart]').innerHTML =
                `<p class="chart-empty">${Charts.esc(error)}</p>`;
            return;
        }

        const p = data.provenance;
        const censoredCount = published.filter((r) => r.censored).length;
        Charts.renderCoverageStrip(panel.querySelector('[data-strip]'), [
            { label: 'shown', value: `${p.published_rows}/${p.total_rows}` },
            {
                label: 'truncated',
                value: censoredCount ? `${censoredCount} lower-bound` : 'none',
                warn: censoredCount > 0,
                tooltip: 'The board cut off the result set for these families, so their '
                       + 'flow is a lower bound rather than a measurement.',
            },
            {
                label: 'window',
                value: `${p.window_days}d`,
                warn: p.window_below_minimum,
                tooltip: p.window_below_minimum
                    ? `Below the ${p.min_window_days}-day minimum: a full scrape cycle `
                      + 'takes about 5 days, so shorter windows have uneven coverage.'
                    : '',
            },
        ]);

        Charts.renderBarChart(panel.querySelector('[data-chart]'), published.map((r) => ({
            label: r.label,
            value: r.flow_per_day || 0,
            censored: r.censored,
            zero_yield: r.zero_yield,
            n_postings: r.n_postings,
            n_companies: r.n_companies,
            coverage_fraction: r.coverage_fraction,
        })), {
            valueKey: 'value',
            unit: '/day',
            formatValue: (v) => v.toFixed(1),
            labelWidth: 170,
            emptyText: 'Nothing published for this location yet.',
            ariaLabel: `Role supply in ${loc.label}`,
        });

        panel.querySelector('[data-suppressed]').innerHTML = suppressed.length
            ? `<span class="suppressed-note">${suppressed.length} suppressed: `
              + suppressed.map((r) =>
                  `${Charts.esc(r.label)} (${Charts.esc(r.suppressed_reason)})`).join(', ')
              + '</span>'
            : '';
    });

    if (!anyData) {
        panels.insertAdjacentHTML('afterbegin',
            '<p class="cold-start-note">No supply figures yet. Run '
            + '<code>uv run python sync.py --backfill</code> a few times, then check back — '
            + 'each family needs enough observed window coverage before a rate can be '
            + 'stated.</p>');
    }

    loadMarketHeatmap(source, windowDays);
    loadCoverageTable();
}

async function loadMarketHeatmap(source, windowDays) {
    const container = document.getElementById('market-heatmap');
    if (!marketLocations) return;

    const locations = marketLocations.locations;
    const perLocation = await Promise.all(locations.map(async (loc) => {
        try {
            const res = await fetch(
                `/api/market/supply?location=${encodeURIComponent(loc.id)}`
                + `&source=${encodeURIComponent(source)}&window_days=${windowDays}`);
            if (!res.ok) return {};
            const data = await res.json();
            const map = {};
            data.rows.forEach((r) => {
                if (!r.suppressed_reason) map[r.role_family] = r.flow_per_day || 0;
            });
            return map;
        } catch (e) { return {}; }
    }));

    // Only families with data somewhere, so the grid does not become mostly dots.
    const families = marketLocations.role_families.filter((f) =>
        perLocation.some((m) => m[f.key] !== undefined));

    if (!families.length) {
        container.innerHTML = '<p class="chart-empty">Not enough coverage yet.</p>';
        return;
    }

    Charts.renderHeatmap(
        container,
        families.map((f) => perLocation.map((m) =>
            m[f.key] === undefined ? null : m[f.key])),
        {
            rowLabels: families.map((f) => f.label),
            colLabels: locations.map((l) => l.id),
            formatValue: (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1)),
        }
    );
}

async function loadCoverageTable() {
    const container = document.getElementById('coverage-table');
    const summary = document.getElementById('coverage-summary');
    try {
        const res = await fetch('/api/market/coverage');
        const { cells } = await res.json();
        const scraped = cells.filter((c) => c.total_scrapes > 0);
        const stale = scraped.filter((c) => (c.hours_since_success ?? 1e6) > 96);
        const erroring = cells.filter((c) => c.consecutive_error > 0);

        summary.textContent = `${scraped.length}/${cells.length} cells visited · `
            + `${stale.length} stale · ${erroring.length} erroring`;

        const rows = scraped
            .sort((a, b) => (b.hours_since_success ?? 1e6) - (a.hours_since_success ?? 1e6))
            .slice(0, 40);

        if (!rows.length) {
            container.innerHTML = '<p class="chart-empty">No cells scraped yet.</p>';
            return;
        }

        container.innerHTML = `
            <table class="data-table">
              <thead><tr>
                <th>Source</th><th>Location</th><th>Role family</th><th>Tier</th>
                <th>Last success</th><th>Returned</th><th>Scrapes</th><th>State</th>
              </tr></thead>
              <tbody>${rows.map((c) => {
                  const hrs = c.hours_since_success;
                  const staleCell = (hrs ?? 1e6) > 96;
                  let state = 'ok';
                  if (c.backoff_until) state = 'backoff';
                  else if (c.consecutive_error > 0) state = `${c.consecutive_error} errors`;
                  else if (c.consecutive_empty > 2) state = `${c.consecutive_empty} empty`;
                  else if (c.last_saturated) state = 'truncated';
                  return `<tr class="${staleCell ? 'row-warn' : ''}">
                    <td>${Charts.esc(c.source)}</td>
                    <td>${Charts.esc(c.location_id)}</td>
                    <td>${Charts.esc(c.role_family)}</td>
                    <td>${Charts.esc(c.tier)}</td>
                    <td>${hrs === null ? 'never' : `${hrs.toFixed(0)}h ago`}</td>
                    <td>${c.last_result_count ?? 0}</td>
                    <td>${c.total_scrapes}</td>
                    <td>${Charts.esc(state)}</td>
                  </tr>`;
              }).join('')}</tbody>
            </table>`;
    } catch (e) {
        container.innerHTML = '<p class="chart-empty">Could not load coverage.</p>';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    ['market-source', 'market-window'].forEach((id) => {
        document.getElementById(id)?.addEventListener('change', loadMarketTab);
    });
});
