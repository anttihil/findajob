// Skill-gap analysis and the skill detail drawer.

import { getJSON, reportError } from '../api.js';
import { escapeHTML } from '../lib/html.js';


export async function loadSkillsTab() {
    const windowDays = Number(document.getElementById('skills-window').value);
    const weighting = document.getElementById('skills-weighting').value;
    const gapList = document.getElementById('gap-list');
    gapList.innerHTML = '<p class="chart-empty">Loading…</p>';

    let data;
    try {
        const res = await fetch(
            `/api/skills/gap?window_days=${windowDays}&weighting=${weighting}`);
        data = await res.json();
    } catch (e) {
        gapList.innerHTML = '<p class="chart-empty">Could not load skill gaps.</p>';
        return;
    }

    const p = data.provenance;

    Charts.renderStatTiles(document.getElementById('skills-tiles'), [
        { value: p.n_postings ?? 0, label: 'postings analysed',
          sub: `n_eff ${p.n_eff ?? 0}`,
          tooltip: 'Effective sample size after reweighting. Lower than the raw count '
                 + 'because an uneven scrape mix costs precision.' },
        { value: p.n_good_fit ?? 0, label: 'strong matches',
          sub: `score ≥ ${p.good_fit_threshold ?? 60}`,
          tooltip: 'Postings you already match well. Blocking gaps are measured against '
                 + 'these.' },
        { value: data.views.priority_gaps.length, label: 'skills to acquire' },
        { value: data.views.validated_strengths.length, label: 'validated strengths' },
    ]);

    Charts.renderCoverageStrip(document.getElementById('skills-coverage'), [
        { label: 'weighting', value: data.weighting_effective || data.weighting_mode },
        { label: 'window', value: `${data.window_days}d`,
          warn: p.window_below_minimum },
        { label: 'strata', value: `${p.strata_used ?? 0}/${p.strata_available ?? 0}` },
        { label: 'suppressed', value: data.views.suppressed.length,
          warn: data.views.suppressed.length > 0 },
    ]);

    // Banner: the honest caveats, stated up front rather than buried.
    const banner = document.getElementById('skills-banner');
    const notes = [];
    if (data.weighting_fallback_reason) {
        notes.push({ severity: 'warning', text: data.weighting_fallback_reason });
    }
    if (p.cold_start) {
        notes.push({
            severity: 'info',
            text: 'Building baseline — the corpus is still small, so treat these figures as '
                + 'provisional. A full scrape cycle takes about 5 days.',
        });
    }
    if (p.residual_bias_note) {
        notes.push({ severity: 'info', text: p.residual_bias_note });
    }
    banner.innerHTML = notes.length
        ? `<div class="glass-card analysis-banner">${notes.map((n) =>
            `<p class="banner-note is-${n.severity}">
               <i class="fa-solid ${n.severity === 'warning'
                   ? 'fa-triangle-exclamation' : 'fa-circle-info'}"></i>
               ${Charts.esc(n.text)}</p>`).join('')}</div>`
        : '';

    renderGapRows(gapList, data.views.priority_gaps, 'gap');
    renderGapRows(document.getElementById('strengths-list'),
                  data.views.validated_strengths, 'strength');
    renderGapRows(document.getElementById('dead-weight-list'),
                  data.views.dead_weight, 'dead');

    document.getElementById('suppressed-count').textContent =
        `${data.views.suppressed.length} skills`;
    document.getElementById('suppressed-list').innerHTML =
        data.views.suppressed.length
            ? data.views.suppressed.map((s) =>
                `<span class="suppressed-chip" title="${Charts.esc(s.reason)}">
                   ${Charts.esc(s.label)}
                   <em>${Charts.esc(s.reason)}</em> · n=${s.n_raw}</span>`).join('')
            : '<p class="chart-empty">Nothing suppressed.</p>';
}

function renderGapRows(container, rows, kind) {
    container.innerHTML = '';
    if (!rows || !rows.length) {
        container.innerHTML = '<p class="chart-empty">Nothing to show yet.</p>';
        return;
    }

    rows.forEach((row, index) => {
        const item = document.createElement('div');
        item.className = `gap-row is-${kind}`;

        const pct = (v) => `${((v || 0) * 100).toFixed(0)}%`;
        const headline = kind === 'gap'
            ? `<span class="gap-priority" title="Composite acquisition priority">
                 ${(row.priority * 100).toFixed(0)}</span>`
            : `<span class="gap-priority is-demand" title="Share of postings requiring it">
                 ${pct(row.demand)}</span>`;

        item.innerHTML = `
            <div class="gap-row-head">
                <span class="gap-rank">${index + 1}</span>
                ${headline}
                <button class="gap-name" data-skill="${Charts.esc(row.skill)}">
                    ${Charts.esc(row.label)}
                </button>
                <span class="gap-tags">
                    <span class="gap-tag">${Charts.esc(row.category)}</span>
                    ${kind === 'gap'
                        ? `<span class="gap-tag is-effort-${Charts.esc(row.effort)}">
                             ${Charts.esc(row.effort)} effort</span>` : ''}
                    ${row.user_level
                        ? `<span class="gap-tag is-have">level ${row.user_level}</span>` : ''}
                </span>
            </div>
            <div class="gap-row-body" data-meters></div>
            <div class="gap-row-foot">
                <span title="Postings mentioning this skill">n=${row.n_raw}</span>
                <span title="Distinct companies">${row.n_companies} companies</span>
                <span title="Wilson score interval on demand">
                    demand ${pct(row.demand)}
                    [${pct(row.demand_ci_low)}–${pct(row.demand_ci_high)}]</span>
                ${row.salary_lift
                    ? `<span title="Median salary ratio vs the corpus baseline">
                         salary ×${row.salary_lift.toFixed(2)}</span>` : ''}
            </div>`;
        container.appendChild(item);

        if (kind === 'gap') {
            Charts.renderMeters(item.querySelector('[data-meters]'), [
                { label: 'blocking', value: row.blocking_gap,
                  display: pct(row.blocking_gap),
                  tooltip: 'Share of postings you otherwise match well that require this '
                         + 'skill — the reason to learn it next.' },
                { label: 'demand', value: row.demand, display: pct(row.demand),
                  tooltip: 'Share of all in-scope postings requiring it.' },
                { label: 'adjacent', value: row.adjacency, display: pct(row.adjacency),
                  tooltip: 'How much of the surrounding stack you already know — a proxy '
                         + 'for how reachable this skill is from where you are.' },
            ]);
        } else {
            item.querySelector('[data-meters]').remove();
        }
    });

    container.querySelectorAll('.gap-name').forEach((button) => {
        button.addEventListener('click', () => openSkillDetail(button.dataset.skill));
    });
}

async function openSkillDetail(skill) {
    const esc = window.Charts.esc;
    let d;
    try {
        d = await getJSON(`/api/skills/${encodeURIComponent(skill)}`);
    } catch (err) {
        reportError(`Loading detail for "${skill}"`, err);
        return;
    }

    document.getElementById('skill-level').textContent =
        d.user_has ? `Level ${d.user_level}` : 'Not on resume';
    document.getElementById('skill-category').textContent = d.category;
    document.getElementById('skill-title').textContent = d.label;
    document.getElementById('skill-postings').innerHTML =
        `<i class="fa-solid fa-briefcase"></i> ${d.postings.length} postings`;
    document.getElementById('skill-window').innerHTML =
        `<i class="fa-solid fa-clock"></i> last ${d.window_days} days`;

    document.getElementById('skill-evidence').innerHTML = d.evidence.length
        ? `<p class="verdict-reasoning">${escapeHTML(d.evidence.join(' · '))}</p>`
        : '<p class="verdict-empty">No evidence for this skill in your profile.</p>';

    document.getElementById('skill-cooccurring').innerHTML =
        d.cooccurring.map((c) =>
            `<span class="skill-tag ${c.user_has ? '' : 'is-missing'}">
               ${esc(c.label)} <em>${c.n}</em></span>`).join('')
        || '<span class="skill-tag">No co-occurring skills</span>';

    document.getElementById('skill-detail-body').innerHTML = `
        <ul class="detail-list">${d.by_role_family.map((f) =>
            `<li>${esc(f.label)} <strong>${f.n}</strong></li>`).join('')}</ul>
        <h4>Postings requiring it</h4>
        <ul class="detail-list">${d.postings.slice(0, 25).map((j) =>
            `<li><a href="${esc(j.url)}" target="_blank" rel="noopener">
               ${esc(j.title)}</a>
               <span class="detail-meta">${esc(j.company)} ·
               score ${j.match_score}${j.in_title ? ' · in title' : ''}</span></li>`)
            .join('')}</ul>`;

    openSkillDrawer();
}

function openSkillDrawer() {
    document.getElementById('skill-drawer').classList.add('active');
    document.getElementById('skill-drawer-overlay').classList.add('active');
}

function closeSkillDrawer() {
    document.getElementById('skill-drawer').classList.remove('active');
    document.getElementById('skill-drawer-overlay').classList.remove('active');
}

export function setupSkillDrawer() {
    document.getElementById('close-skill-drawer-btn')
        ?.addEventListener('click', closeSkillDrawer);
    document.getElementById('skill-drawer-overlay')
        ?.addEventListener('click', closeSkillDrawer);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeSkillDrawer();
    });
}

/* Control wiring. Each select re-runs its own tab's loader; none of them holds state. */
document.addEventListener('DOMContentLoaded', () => {
    ['skills-window', 'skills-weighting'].forEach((id) => {
        document.getElementById(id)?.addEventListener('change', loadSkillsTab);
    });
});
