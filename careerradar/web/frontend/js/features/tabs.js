// Tab switching.
//
// The active tab lives in the URL fragment. It has to now: changing a dashboard filter is
// a real navigation, so anything held only in a JS variable would be reset by every filter
// click. The fragment is not sent to the server, which is what makes it the right home for
// a purely presentational choice.

import { loadDigests } from './digests.js';
import { loadMarketTab } from './market.js';
import { loadSkillsTab } from './skills.js';
import { renderResumesTab } from './resumes.js';
import { checkSyncStatus } from './sync.js';
import { startPipelinePolling, stopPipelinePolling } from './pipeline.js';

const TABS = {
    'dashboard':    { title: 'Career Dashboard' },
    'search-links': { title: 'Board Search Generators' },
    'resumes':      { title: 'My Resumes & Skill Profiles', load: renderResumesTab },
    'digests':      { title: 'Daily Job Digests', load: loadDigests },
    'market':       { title: 'Market Supply', load: loadMarketTab },
    'skills':       { title: 'Skill Gap Analysis', load: loadSkillsTab },
    'settings':     {
        title: 'Radar Configurations',
        load: () => { checkSyncStatus(); startPipelinePolling(); },
        unload: stopPipelinePolling,
    },
};

let currentTab = null;

function activate(name) {
    const tab = TABS[name] ? name : 'dashboard';
    const pane = document.getElementById(`tab-${tab}`);
    if (!pane) return;

    if (currentTab && currentTab !== tab) TABS[currentTab].unload?.();
    currentTab = tab;

    document.querySelectorAll('.nav-item').forEach(n =>
        n.classList.toggle('active', n.getAttribute('data-tab') === tab));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    pane.classList.add('active');

    document.getElementById('page-title').textContent = TABS[tab].title;
    TABS[tab].load?.();
}

export function setupTabSwitching() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            // Setting the hash fires hashchange, which does the activation. One path in,
            // so the fragment and the visible tab cannot disagree.
            window.location.hash = item.getAttribute('data-tab');
        });
    });
    window.addEventListener('hashchange', () => activate(window.location.hash.slice(1)));
    activate(window.location.hash.slice(1));
}
