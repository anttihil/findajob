// The "Resumes & Skills" tab.
//
// This tab was broken. It fetched `/api/resumes`, an endpoint that does not exist -- the
// server moved to a single LLM-built profile (`/api/profile`) and the client was never
// updated. The 404 landed in a bare `catch` that logged to the console, so `resumesData`
// stayed `{}` and the tab rendered as an empty grid, which is indistinguishable from
// "no resumes configured yet".
//
// There is no longer a plural to render: one active profile, versioned, built from a
// declared corpus. The tab says which version, from which documents.

import { getJSON, guard } from '../api.js';
import { escapeHTML } from '../lib/html.js';

function levelLabel(level) {
    return ['none', 'aware', 'working', 'strong', 'expert'][level] ?? String(level);
}

function skillTags(skills) {
    const entries = Object.entries(skills || {})
        .sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));
    if (!entries.length) return '<span class="skill-tag">No skills recorded</span>';
    return entries.map(([key, level]) =>
        `<span class="skill-tag" title="level ${escapeHTML(level)} — ${escapeHTML(levelLabel(level))}"
         >${escapeHTML(key.replace(/_/g, ' '))} <em>${escapeHTML(levelLabel(level))}</em></span>`
    ).join('');
}

function documentRows(documents) {
    if (!documents?.length) return '<li>No source documents recorded.</li>';
    return documents.map(doc => {
        const name = String(doc.path || '').split('/').pop();
        return `<li><strong>${escapeHTML(name)}</strong>
                <span class="detail-meta">${escapeHTML(doc.kind || '')} ·
                ${escapeHTML(doc.chars ?? 0)} chars</span></li>`;
    }).join('');
}

export async function renderResumesTab() {
    const container = document.getElementById('resumes-list-container');
    if (!container) return;

    const record = await guard('Loading the active profile', async () => {
        try {
            return await getJSON('/api/profile');
        } catch (err) {
            // A 404 here is the documented answer when no profile has been built, and it
            // is worth stating plainly rather than reporting as a fault.
            if (err.status === 404) return null;
            throw err;
        }
    });

    if (record === null) {
        container.innerHTML = `
            <div class="resume-card">
                <div class="resume-card-header"><h3>No active profile</h3></div>
                <p class="card-note">Nothing downstream is meaningful without one. Build it
                   with <code>careerradar profile build</code>.</p>
            </div>`;
        return;
    }
    if (!record) return;  // request failed; `guard` has surfaced it

    const profile = record.profile || {};
    const skills = profile.skills || {};

    container.innerHTML = `
        <div class="resume-card">
            <div class="resume-card-header">
                <span>Profile version ${escapeHTML(record.version)} ·
                      built ${escapeHTML((record.created_at || '').slice(0, 10))} ·
                      ${escapeHTML(record.model || 'unknown model')}</span>
                <h3>${escapeHTML(profile.seniority || 'Active profile')}</h3>
            </div>
            <div class="divider"></div>
            <p class="verdict-summary">${escapeHTML(profile.bio || '')}</p>
            <h4>Skills Vector (${Object.keys(skills).length})</h4>
            <div class="skills-scroll-area">${skillTags(skills)}</div>
            <h4>Built from</h4>
            <ul class="detail-list">${documentRows(record.documents)}</ul>
        </div>`;
}
