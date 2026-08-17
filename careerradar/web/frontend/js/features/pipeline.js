// Live progress for the two background pipeline stages: scraping and scoring.
//
// Both run on their own systemd timer as well as on demand from this dashboard, so a run
// can already be in progress when this tab opens. Polling on a plain interval -- rather
// than only while this session's own Sync button happens to be spinning, which is all
// sync.js tracks -- is what makes an externally-started run visible at all.

import { getJSON, reportError } from '../api.js';

const POLL_MS = 5000;
let pollingInterval = null;

const el = {
    get scrapeDot()    { return document.getElementById('scrape-stage-dot'); },
    get scrapeStatus() { return document.getElementById('scrape-stage-status'); },
    get scrapeMeter()  { return document.getElementById('scrape-progress-meter'); },
    get scrapeDetail() { return document.getElementById('scrape-stage-detail'); },
    get scoreDot()     { return document.getElementById('score-stage-dot'); },
    get scoreStatus()  { return document.getElementById('score-stage-status'); },
    get scoreMeter()   { return document.getElementById('score-progress-meter'); },
    get scoreDetail()  { return document.getElementById('score-stage-detail'); },
};

function ago(hours) {
    if (hours === null || hours === undefined) return 'never';
    if (hours < 1) return `${Math.round(hours * 60)}m ago`;
    if (hours < 48) return `${hours.toFixed(1)}h ago`;
    return `${(hours / 24).toFixed(1)}d ago`;
}

function renderScrape(scrape) {
    const running = scrape.in_progress;
    el.scrapeDot.className = `status-dot ${running ? 'orange' : 'green'}`;
    el.scrapeStatus.textContent = running ? 'running' : 'idle';

    if (running && scrape.cells_planned) {
        window.Charts.renderMeters(el.scrapeMeter, [{
            label: 'cells',
            value: scrape.cells_done,
            display: `${scrape.cells_done}/${scrape.cells_planned}`,
        }], { max: scrape.cells_planned });
        el.scrapeDetail.textContent =
            `${scrape.cells_done} of ${scrape.cells_planned} planned cells scraped this run`;
        return;
    }

    el.scrapeMeter.innerHTML = '';
    if (running) {
        el.scrapeDetail.textContent = 'starting up...';
        return;
    }

    const prev = scrape.previous_run;
    if (!prev?.last_run) {
        el.scrapeDetail.textContent = 'never run';
        return;
    }
    const [succeeded, planned] = prev.cells || [0, 0];
    el.scrapeDetail.textContent =
        `last run ${ago(prev.hours_since)} (${prev.status}): ${succeeded}/${planned} cells, `
        + `${prev.postings_new} new postings`;
}

function renderScore(score) {
    const active = score.recent_verdicts_5min > 0;
    el.scoreDot.className = `status-dot ${active ? 'orange' : 'green'}`;
    el.scoreStatus.textContent = active ? 'scoring' : 'idle';

    const scoredFraction = 1 - (score.backlog_share || 0);
    window.Charts.renderMeters(el.scoreMeter, [{
        label: 'scored',
        value: scoredFraction,
        display: `${(scoredFraction * 100).toFixed(0)}%`,
    }], { max: 1 });

    const detail = [`${score.backlog.toLocaleString()} unscored`];
    detail.push(score.last_verdict ? `last verdict ${ago(score.hours_since)}` : 'no verdicts yet');
    if (active) detail.push(`${score.recent_verdicts_5min} scored in the last 5 minutes`);
    el.scoreDetail.textContent = detail.join(' · ');
}

async function poll() {
    let data;
    try {
        data = await getJSON('/api/pipeline/status');
    } catch (err) {
        reportError('Loading pipeline status', err);
        stopPipelinePolling();
        return;
    }
    renderScrape(data.scrape);
    renderScore(data.score);
}

export function startPipelinePolling() {
    poll();
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(poll, POLL_MS);
}

export function stopPipelinePolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
}
