// The Settings tab.
//
// The country checkboxes are rendered by the server now (see tabs/settings.html); this
// module only fills in which ones are ticked. The list itself was hardcoded here as a
// fourth copy of a fact that lives in data/roles.yaml.

import { getJSON, postJSON, guard, reportError } from '../api.js';

let configData = {};

export function currentConfig() {
    return configData;
}

const SOURCE_LABELS = {
    indeed: 'Indeed (volume source — full descriptions)',
    linkedin: 'LinkedIn (budgeted supplement)',
};

function populateSettingsFields() {
    const queries = document.getElementById('settings-queries');
    const slider = document.getElementById('settings-score');
    const sliderValue = document.getElementById('settings-score-val');
    const sources = document.getElementById('settings-sources-container');

    queries.value = configData.search_queries?.join('\n') || '';

    // Tier, not a percent. The old slider set min_match_score, which defaulted to the
    // floor of the scale it gated and so never filtered anything.
    slider.value = (configData.matching || {}).max_tier || 10;
    sliderValue.textContent = slider.value;

    const active = configData.countries || [];
    document.querySelectorAll('#settings-countries-container input[name="country"]')
        .forEach(el => { el.checked = active.includes(el.value); });

    // Sources moved under scraper.sources; fall back to the old flat key for an
    // un-migrated config file.
    const activeSources = configData.scraper?.sources || configData.sources || {};
    sources.innerHTML = Object.entries(SOURCE_LABELS).map(([key, label]) => `
        <label class="checkbox-label">
            <input type="checkbox" name="source" value="${key}"
                   ${activeSources[key] ? 'checked' : ''}> ${label}
        </label>`).join('');
}

export async function loadSettings() {
    await guard('Loading configuration', async () => {
        configData = await getJSON('/api/config');
        populateSettingsFields();
    });
}

export function setupSettingsForm() {
    const form = document.getElementById('settings-form');
    const slider = document.getElementById('settings-score');
    const sliderValue = document.getElementById('settings-score-val');
    if (!form) return;

    slider.addEventListener('input', () => { sliderValue.textContent = slider.value; });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();

        const checkedCountries = [...form.querySelectorAll('input[name="country"]:checked')]
            .map(el => el.value);
        const checkedSources = Object.fromEntries(
            [...form.querySelectorAll('input[name="source"]')].map(el => [el.value, el.checked]));
        const queries = document.getElementById('settings-queries').value
            .split('\n').map(q => q.trim()).filter(Boolean);

        // /api/config deep-merges server-side, so omitting a key leaves it untouched.
        // Posting a partial payload used to overwrite everything it did not mention.
        try {
            const data = await postJSON('/api/config', {
                countries: checkedCountries,
                search_queries: queries,
                scraper: { sources: checkedSources },
                matching: { max_tier: parseInt(slider.value, 10) },
            });
            configData = data.config;
            populateSettingsFields();
            alert('Configuration saved successfully!');
        } catch (err) {
            reportError('Saving configuration', err);
        }
    });
}
