// Entry point.
//
// What is *not* here is the point of the reorganization. The dashboard feed, its filters,
// the sort control, pagination and the job drawer are rendered by the server from the
// request's query string, so there is no `jobsData`, no `activeStatusFilter`, no
// `currentAccess`, and no fetch-and-rerender cycle to keep in step with them.
//
// The bug that prompted this: the reachability pills and the status pills shared the
// `.status-pill` selector, so `setupFilters` bound its handler to both. Every reachability
// click fired two `fetchJobs()` calls -- the first carrying the previous reachability
// value, because it ran before `currentAccess` was assigned -- and neither call sequenced
// or aborted the other. Whichever response landed last won, so the list showed the old
// filter about half the time and clicking again re-rolled it.
//
// What remains in the browser is the analytics views, whose markup is genuinely built from
// JSON (charts.js draws SVG), plus the controls that have no server state to speak of.

import { setupTabSwitching } from './features/tabs.js';
import { setupSync, checkSyncStatus } from './features/sync.js';
import { setupSettingsForm, loadSettings } from './features/settings.js';
import { setupLinkGenerator } from './features/link-generator.js';
import { setupSkillDrawer } from './features/skills.js';
import { reportError } from './api.js';

document.addEventListener('DOMContentLoaded', () => {
    setupTabSwitching();
    setupSync();
    setupSettingsForm();
    setupSkillDrawer();

    document.getElementById('close-app-error-btn')?.addEventListener('click', () => {
        document.getElementById('app-error-banner').classList.add('hide');
    });

    // Config feeds both the Settings tab and the link generator's role-title list.
    loadSettings().then(setupLinkGenerator);
    checkSyncStatus();
});

// Nothing should fail silently any more.
window.addEventListener('unhandledrejection', (event) => {
    reportError('Unhandled error', event.reason);
});
