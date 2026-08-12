// Scrape trigger and status polling.

import { getJSON, reportError } from '../api.js';
import { escapeHTML } from '../lib/html.js';

let pollingInterval = null;

const el = {
    get trigger()  { return document.getElementById('trigger-sync-btn'); },
    get quick()    { return document.getElementById('quick-sync-btn'); },
    get progress() { return document.getElementById('sync-progress-indicator'); },
    get stats()    { return document.getElementById('sync-stats-info'); },
    get dot()      { return document.querySelector('.sync-indicator .status-dot'); },
    get text()     { return document.querySelector('.sync-indicator .status-text'); },
    get banner()   { return document.getElementById('sync-error-banner'); },
    get errors()   { return document.getElementById('sync-errors-list'); },
};

export function setupSync() {
    el.trigger?.addEventListener('click', triggerSync);
    el.quick?.addEventListener('click', triggerSync);
    document.getElementById('close-banner-btn')?.addEventListener('click', () => {
        el.banner?.classList.add('hide');
    });
}

async function triggerSync() {
    el.trigger.disabled = true;
    el.quick.disabled = true;
    el.dot.className = 'status-dot orange';
    el.text.textContent = 'Syncing Database...';
    el.progress.classList.remove('hide');

    try {
        const res = await fetch('/api/sync', { method: 'POST' });
        if (!res.ok && res.status !== 409) {
            throw new Error(`sync returned ${res.status}`);
        }
        startSyncPolling();
    } catch (err) {
        reportError('Triggering a scrape', err);
        resetSyncUI();
    }
}

function startSyncPolling() {
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(checkSyncStatus, 2000);
}

export async function checkSyncStatus() {
    let data;
    try {
        data = await getJSON('/api/sync/status');
    } catch (err) {
        reportError('Polling sync status', err);
        stopPolling();
        resetSyncUI();
        return;
    }

    if (data.sync_in_progress) {
        el.trigger.disabled = true;
        el.quick.disabled = true;
        el.dot.className = 'status-dot orange';
        el.text.textContent = 'Syncing Database...';
        if (window.location.hash === '#settings') {
            el.progress.classList.remove('hide');
        }
        return;
    }

    const wasPolling = pollingInterval !== null;
    stopPolling();
    resetSyncUI();

    if (data.last_run_stats?.total_fetched > 0) {
        const s = data.last_run_stats;
        el.stats.innerHTML = `
            Last Sync Scanned: <strong>${s.total_fetched.toLocaleString()}</strong> posts<br>
            Evaluated: <strong>${s.total_evaluated.toLocaleString()}</strong> fits<br>
            Saved: <strong>${s.total_new}</strong> new`;
        el.stats.classList.remove('hide');
    } else {
        el.stats.classList.add('hide');
    }

    if (data.errors?.length) {
        renderSyncErrors(data.errors);
    } else {
        el.banner?.classList.add('hide');
    }

    // A finished scrape means the feed on screen is stale, and the feed is rendered by
    // the server now -- there is no client-side list to refresh in place.
    if (wasPolling) window.location.reload();
}

function stopPolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
}

function renderSyncErrors(errors) {
    if (!el.errors || !el.banner) return;

    // Severity distinguishes a hard failure from a coverage caveat or a notice. Rendering
    // "supply for these families is suppressed this window" in the same red as a rate-limit
    // trip trains the reader to ignore the banner.
    const ICONS = {
        error: 'fa-circle-exclamation',
        warning: 'fa-triangle-exclamation',
        info: 'fa-circle-info',
    };
    el.errors.innerHTML = errors.map(err => {
        const timeStr = err.timestamp
            ? new Date(err.timestamp).toLocaleTimeString() : 'Unknown';
        const severity = err.severity || 'error';
        return `
            <li class="sync-error-${escapeHTML(severity)}">
                <i class="fa-solid ${ICONS[severity] || ICONS.error}"></i>
                <strong>${escapeHTML(err.source)}</strong> [${escapeHTML(timeStr)}]:
                ${escapeHTML(err.error)}
            </li>`;
    }).join('');
    el.banner.classList.remove('hide');
}

function resetSyncUI() {
    el.trigger.disabled = false;
    el.quick.disabled = false;
    el.progress.classList.add('hide');
    el.dot.className = 'status-dot green';
    el.text.textContent = 'Database Idle';
}
