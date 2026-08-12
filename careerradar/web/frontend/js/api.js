// One place where the browser talks to the API.
//
// Every fetch in the old app.js was wrapped in `try { ... } catch (e) { console.error(e) }`.
// That is why `/api/resumes` could 404 on every page load without anyone noticing: the
// resume dropdowns and the whole Resumes tab rendered empty, which looks exactly like
// "no resumes yet". A request that fails now says so on the page.

import { escapeHTML } from './lib/html.js';

export class ApiError extends Error {
    constructor(url, status, body) {
        super(`${status} from ${url}`);
        this.url = url;
        this.status = status;
        this.body = body;
    }
}

export async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) {
        throw new ApiError(url, res.status, await res.text().catch(() => ''));
    }
    return res.json();
}

export async function postJSON(url, payload) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!res.ok) {
        throw new ApiError(url, res.status, await res.text().catch(() => ''));
    }
    return res.json();
}

// A 404 that is a legitimate answer rather than a fault -- "this company has no dossier",
// "this profile has no versions". Callers that use this are saying the absence is expected;
// anything else still surfaces.
export async function getJSONOrNull(url) {
    try {
        return await getJSON(url);
    } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
    }
}

// Failures are shown on the page, because an analytics panel that silently renders
// nothing is indistinguishable from one with nothing to render.
export function reportError(what, err) {
    console.error(what, err);
    // Its own banner, not the sync one: sync polling rewrites that list every two
    // seconds and would erase these.
    const banner = document.getElementById('app-error-banner');
    const list = document.getElementById('app-errors-list');
    if (!banner || !list) return;
    const detail = err instanceof ApiError ? `HTTP ${err.status}` : (err?.message || err);
    list.insertAdjacentHTML('beforeend',
        `<li class="sync-error-item severity-error">
            <strong>${escapeHTML(what)}</strong>
            <span>${escapeHTML(detail)}</span>
         </li>`);
    banner.classList.remove('hide');
}

// Wrap a loader so one broken panel cannot take the rest of the page with it.
export async function guard(what, fn) {
    try {
        return await fn();
    } catch (err) {
        reportError(what, err);
        return null;
    }
}
