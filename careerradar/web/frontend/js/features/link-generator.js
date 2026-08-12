// Pre-filled job-board search links.
//
// The resume selector is gone. `/api/search-links` takes a country and a query and builds
// the boolean string from the *active profile's* strongest skills -- it has no `resume`
// parameter and never had one. The old client sent `resume=<filename>` anyway; FastAPI
// drops unknown query parameters, so the control appeared to work while changing nothing.
// It was also populated from `/api/resumes`, which 404s, so it was empty regardless.

import { getJSON, guard } from '../api.js';

export async function setupLinkGenerator() {
    const button = document.getElementById('generate-links-btn');
    const countrySelect = document.getElementById('link-country-select');
    const querySelect = document.getElementById('link-query-select');
    if (!button || !countrySelect || !querySelect) return;

    await guard('Loading search queries', async () => {
        const config = await getJSON('/api/config');
        querySelect.innerHTML = (config.queries || [])
            .map(q => `<option value="${q}">${q}</option>`).join('');
    });

    button.addEventListener('click', async () => {
        const country = countrySelect.value;
        const query = querySelect.value;
        if (!country || !query) return;

        button.disabled = true;
        button.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Generating...';
        try {
            const links = await getJSON(
                `/api/search-links?country=${encodeURIComponent(country)}`
                + `&query=${encodeURIComponent(query)}`);

            document.getElementById('gen-role-title').textContent = query;
            document.getElementById('gen-country').textContent =
                countrySelect.options[countrySelect.selectedIndex].text;
            document.getElementById('gen-query-code').textContent = links.search_query_used;
            document.getElementById('linkedin-search-link').href = links.linkedin;
            document.getElementById('indeed-search-link').href = links.indeed;
            document.getElementById('generated-links-results').classList.remove('hide');
        } catch (err) {
            // A 404 here means no active profile, which is a different problem from a
            // broken request and deserves to say so.
            const { reportError } = await import('../api.js');
            reportError(err.status === 404
                ? 'No active profile — build one with `careerradar profile build`'
                : 'Generating search links', err);
        } finally {
            button.disabled = false;
            button.innerHTML =
                '<i class="fa-solid fa-wand-magic-sparkles"></i> Generate Custom Search Links';
        }
    });
}
