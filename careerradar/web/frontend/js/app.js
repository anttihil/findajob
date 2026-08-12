// Global State
let currentTab = 'dashboard';
let jobsData = [];
let resumesData = {};
let configData = {};
let activeStatusFilter = 'unread';
let selectedJob = null;
let syncPollingInterval = null;

// DOM Elements
const navItems = document.querySelectorAll('.nav-item');
const tabPanes = document.querySelectorAll('.tab-pane');
const headerDate = document.getElementById('header-date');
const pageTitle = document.getElementById('page-title');
let jobsTotal = 0;
let currentAccess = '';

// Stats Elements
const statTotalEl = document.getElementById('stat-total');
const statSavedEl = document.getElementById('stat-saved');
const statAppliedEl = document.getElementById('stat-applied');
const statAvgScoreEl = document.getElementById('stat-avg-score');

// Feed & Filter Elements
const jobCardsContainer = document.getElementById('job-cards-container');
const resultsCountEl = document.getElementById('results-count');
const filterResumeSelect = document.getElementById('filter-resume');
const filterCountrySelect = document.getElementById('filter-country');
const filterScoreSlider = document.getElementById('filter-score');
const scoreValEl = document.getElementById('score-val');
const sortBySelect = document.getElementById('sort-by');
const clearFiltersBtn = document.getElementById('clear-filters-btn');
const statusPills = document.querySelectorAll('.status-pill');

// Link Generator Elements
const linkResumeSelect = document.getElementById('link-resume-select');
const linkCountrySelect = document.getElementById('link-country-select');
const linkQuerySelect = document.getElementById('link-query-select');
const generateLinksBtn = document.getElementById('generate-links-btn');
const generatedLinksResults = document.getElementById('generated-links-results');
const genRoleTitleEl = document.getElementById('gen-role-title');
const genCountryEl = document.getElementById('gen-country');
const genQueryCode = document.getElementById('gen-query-code');
const linkedinSearchLink = document.getElementById('linkedin-search-link');
const indeedSearchLink = document.getElementById('indeed-search-link');

// Resume Tab Elements
const resumesListContainer = document.getElementById('resumes-list-container');

// Settings Tab Elements
const triggerSyncBtn = document.getElementById('trigger-sync-btn');
const quickSyncBtn = document.getElementById('quick-sync-btn');
const syncProgressIndicator = document.getElementById('sync-progress-indicator');
const syncStatsInfoEl = document.getElementById('sync-stats-info');
const settingsForm = document.getElementById('settings-form');
const settingsCountriesContainer = document.getElementById('settings-countries-container');
const settingsSourcesContainer = document.getElementById('settings-sources-container');
const settingsQueriesTextarea = document.getElementById('settings-queries');
const settingsScoreSlider = document.getElementById('settings-score');
const settingsScoreValEl = document.getElementById('settings-score-val');

const syncWidgetStatusText = document.querySelector('.sync-indicator .status-text');
const syncWidgetStatusDot = document.querySelector('.sync-indicator .status-dot');

// Drawer Elements
const jobDrawer = document.getElementById('job-drawer');
const drawerOverlay = document.getElementById('drawer-overlay');
const closeDrawerBtn = document.getElementById('close-drawer-btn');
const drawerScore = document.getElementById('drawer-score');
const drawerSource = document.getElementById('drawer-source');
const drawerTitle = document.getElementById('drawer-title');
const drawerCompany = document.getElementById('drawer-company');
const drawerLocation = document.getElementById('drawer-location');
const drawerVerdict = document.getElementById('drawer-verdict');
const drawerDossier = document.getElementById('drawer-dossier');
const drawerDossierSection = document.getElementById('drawer-dossier-section');
const drawerSkillsList = document.getElementById('drawer-skills-list');
const drawerDescription = document.getElementById('drawer-description');
const drawerApplyLink = document.getElementById('drawer-apply-link');
const actionSave = document.getElementById('action-save');
const actionApply = document.getElementById('action-apply');
const actionReject = document.getElementById('action-reject');

// Sync Error Elements
const syncErrorBanner = document.getElementById('sync-error-banner');
const syncErrorsList = document.getElementById('sync-errors-list');
const closeBannerBtn = document.getElementById('close-banner-btn');

// Set Date in Header
const formatDate = () => {
    const options = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };
    headerDate.textContent = new Date().toLocaleDateString('en-US', options);
};

// --- INITIALIZATION ---
document.addEventListener('DOMContentLoaded', () => {
    formatDate();
    setupTabSwitching();
    setupFilters();
    
    // Close error banner listener
    if (closeBannerBtn) {
        closeBannerBtn.addEventListener('click', () => {
            syncErrorBanner.classList.add('hide');
        });
    }
    setupDrawer();
    setupLinkGenerator();
    setupSettingsForm();
    setupSync();
    
    // Fetch initial data
    loadAllData();
    checkSyncStatus();
});

// Load everything
async function loadAllData() {
    await fetchResumes();
    await fetchConfig();
    await fetchStats();
    await fetchJobs();
}

// --- DATA FETCHING ---

async function fetchJobs() {
    try {
        const status = activeStatusFilter;
        const country = filterCountrySelect.value;
        const resume = filterResumeSelect.value;
        const score = filterScoreSlider.value;
        
        let queryParams = [];
        if (status) queryParams.push(`status=${status}`);
        if (country) queryParams.push(`country=${country}`);
        if (score) queryParams.push(`min_score=${score}`);
        
        if (currentAccess) queryParams.push(`access=${currentAccess}`);
        
        const url = `/api/jobs?${queryParams.join('&')}`;
        const res = await fetch(url);
        const payload = await res.json();
        // /api/jobs is paginated now, so it returns {jobs, total, has_more} rather than a
        // bare array.
        jobsData = Array.isArray(payload) ? payload : (payload.jobs || []);
        jobsTotal = Array.isArray(payload) ? payload.length : (payload.total ?? 0);

        renderJobCards();
    } catch (e) {
        console.error("Error fetching jobs:", e);
    }
}

async function fetchResumes() {
    try {
        const res = await fetch('/api/resumes');
        resumesData = await res.json();
        
        // Populate filters and generator selections
        populateResumeDropdowns();
        renderResumesTab();
    } catch (e) {
        console.error("Error fetching resumes:", e);
    }
}

async function fetchConfig() {
    try {
        const res = await fetch('/api/config');
        configData = await res.json();
        
        // Fill form fields
        populateSettingsFields();
    } catch (e) {
        console.error("Error fetching config:", e);
    }
}

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        const stats = await res.json();
        
        // Update overview counts
        statTotalEl.textContent = stats.total_jobs || 0;
        statSavedEl.textContent = stats.status_counts?.saved || 0;
        statAppliedEl.textContent = stats.status_counts?.applied || 0;
        // Coverage with its denominator, not an average of a scale that bottoms out
        // near 15. `strong_matches` is a predicate now (eligible + same/adjacent role +
        // meets or better), so it states what it counted.
        const cov = stats.skill_coverage;
        statAvgScoreEl.textContent = cov && cov.ratio !== null
            ? `${Math.round(cov.ratio * 100)}%` : '--';
    } catch (e) {
        console.error("Error fetching stats:", e);
    }
}

// --- UI RENDERING ---

// The badge says where the posting sits in the partial order, not a percent. Tier 1
// dominates everything below it; equal tiers are genuinely incomparable rather than equal.
function tierBadge(job) {
    if (job.eligibility === 'blocked') {
        return `<span class="match-badge badge-blocked" title="A requirement you cannot meet">blocked</span>`;
    }
    if (job.pareto_tier == null) {
        return `<span class="match-badge badge-unscored">unscored</span>`;
    }
    const cls = job.pareto_tier <= 2 ? 'badge-top'
        : job.pareto_tier <= 4 ? 'badge-good' : 'badge-far';
    const cond = job.eligibility === 'conditional' ? ' *' : '';
    return `<span class="match-badge ${cls}" title="${tierTitle(job)}">tier ${job.pareto_tier}${cond}</span>`;
}

function tierTitle(job) {
    return [job.role_match, job.capability_match, job.seniority_gap]
        .filter(Boolean).join(' · ').replace(/_/g, ' ');
}

// Matched over required, with the denominator visible. `match_score` was never a percent:
// its floor was ~15 and its ceiling ~80.
function coverageBadge(job) {
    if (!job.required_count) return '';
    return `<span class="coverage-badge" title="skills this posting names that you can evidence">${job.matched_count || 0}/${job.required_count} skills</span>`;
}

function populateResumeDropdowns() {
    // Save selections
    const currentFilterVal = filterResumeSelect.value;
    const currentLinkVal = linkResumeSelect.value;
    
    // Clear
    filterResumeSelect.innerHTML = '<option value="">All Profiles</option>';
    linkResumeSelect.innerHTML = '';
    
    Object.keys(resumesData).forEach(filename => {
        const resume = resumesData[filename];
        const optionHTML = `<option value="${filename}">${resume.title}</option>`;
        filterResumeSelect.insertAdjacentHTML('beforeend', optionHTML);
        linkResumeSelect.insertAdjacentHTML('beforeend', optionHTML);
    });
    
    // Restore selections
    if (currentFilterVal && resumesData[currentFilterVal]) {
        filterResumeSelect.value = currentFilterVal;
    }
    if (currentLinkVal && resumesData[currentLinkVal]) {
        linkResumeSelect.value = currentLinkVal;
    }
}

function renderJobCards() {
    jobCardsContainer.innerHTML = '';
    
    // Sorting logic
    const sortBy = sortBySelect.value;
    const sortedJobs = [...jobsData].sort((a, b) => {
        if (sortBy === 'score') {
            // Same order the API and the drawer use: eligibility partitions, then Pareto
            // tier. The card list used to sort by match_score while the drawer led with
            // fit_score, so the same corpus had two different ideas of "best".
            const rank = j => (
                {eligible: 0, conditional: 1, blocked: 2}[j.eligibility] ?? 3);
            return (rank(a) - rank(b))
                || ((a.pareto_tier ?? 99) - (b.pareto_tier ?? 99))
                || (new Date(b.date_found) - new Date(a.date_found));
        } else {
            return new Date(b.date_found) - new Date(a.date_found);
        }
    });

    resultsCountEl.textContent = `${sortedJobs.length} matching positions found`;
    
    if (sortedJobs.length === 0) {
        jobCardsContainer.innerHTML = `
            <div class="no-jobs-card">
                <i class="fa-solid fa-binoculars"></i>
                <h3>No matching jobs found</h3>
                <p>Try adjusting your filters, triggering a new database sync, or relaxing your match threshold.</p>
            </div>
        `;
        return;
    }
    
    sortedJobs.forEach(job => {
        // Skill pills preview (limit to 5)
        const skillsHTML = job.matched_skills.slice(0, 5).map(skill => 
            `<span class="skill-tag-sm">${skill}</span>`
        ).join('');
        
        const dateObj = new Date(job.date_found);
        const dateStr = dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        
        let statusBadge = '';
        if (job.status !== 'unread') {
            statusBadge = `<span class="job-status-indicator ${job.status}">${job.status}</span>`;
        }

        const cardHTML = `
            <div class="job-card" data-id="${job.id}">
                <div class="job-card-details">
                    <div class="job-card-header">
                        <h4>${escapeHTML(job.title)}</h4>
                    </div>
                    <div class="job-company">${escapeHTML(job.company)}</div>
                    <div class="job-meta-row">
                        <span><i class="fa-solid fa-location-dot"></i> ${escapeHTML(job.location || 'Remote')}</span>
                        ${accessBadge(job.access)}
                        <span><i class="fa-solid fa-clock"></i> Found ${dateStr}</span>
                    </div>
                    <div class="matched-skills-preview">
                        ${skillsHTML}
                        ${job.matched_skills.length > 5 ? `<span class="skill-tag-sm">+${job.matched_skills.length - 5} more</span>` : ''}
                    </div>
                </div>
                <div class="job-card-right">
                    <div class="match-badge-wrap">
                        ${statusBadge}
                        ${tierBadge(job)}
                        ${coverageBadge(job)}
                    </div>
                    <span class="source-tag">${job.source}</span>
                </div>
            </div>
        `;
        jobCardsContainer.insertAdjacentHTML('beforeend', cardHTML);
    });
    
    // Add Click Listeners to cards
    document.querySelectorAll('.job-card').forEach(card => {
        card.addEventListener('click', () => {
            const id = parseInt(card.getAttribute('data-id'));
            openJobDrawer(id);
        });
    });
}

function renderResumesTab() {
    resumesListContainer.innerHTML = '';
    
    Object.keys(resumesData).forEach(filename => {
        const resume = resumesData[filename];
        
        const skillsHTML = resume.skills.map(skill => 
            `<span class="skill-tag">${skill}</span>`
        ).join('');
        
        const cardHTML = `
            <div class="resume-card">
                <div class="resume-card-header">
                    <span>Profile File: ${filename}</span>
                    <h3>${resume.title}</h3>
                </div>
                <div class="divider"></div>
                <h4>Skills Vector (${resume.skills.length})</h4>
                <div class="skills-scroll-area">
                    ${skillsHTML}
                </div>
            </div>
        `;
        resumesListContainer.insertAdjacentHTML('beforeend', cardHTML);
    });
}

// --- FILTER CONTROLLERS ---

function setupFilters() {
    filterCountrySelect.addEventListener('change', fetchJobs);
    filterResumeSelect.addEventListener('change', fetchJobs);
    sortBySelect.addEventListener('change', fetchJobs);
    
    filterScoreSlider.addEventListener('input', () => {
        scoreValEl.textContent = `${filterScoreSlider.value}%`;
    });
    filterScoreSlider.addEventListener('change', fetchJobs);
    
    clearFiltersBtn.addEventListener('click', () => {
        filterCountrySelect.value = '';
        filterResumeSelect.value = '';
        filterScoreSlider.value = 15;
        scoreValEl.textContent = '15%';
        fetchJobs();
    });
    
    statusPills.forEach(pill => {
        pill.addEventListener('click', () => {
            statusPills.forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            activeStatusFilter = pill.getAttribute('data-status');
            fetchJobs();
        });
    });
}

// --- SLIDE-OUT DRAWER ---

function setupDrawer() {
    closeDrawerBtn.addEventListener('click', closeJobDrawer);
    drawerOverlay.addEventListener('click', closeJobDrawer);
    
    // Drawer Status Operations
    actionSave.addEventListener('click', () => updateStatus('saved'));
    actionApply.addEventListener('click', () => updateStatus('applied'));
    actionReject.addEventListener('click', () => updateStatus('rejected'));
}

function openJobDrawer(jobId) {
    selectedJob = jobsData.find(j => j.id === jobId);
    if (!selectedJob) return;
    
    drawerTitle.textContent = selectedJob.title;
    drawerCompany.innerHTML = `<i class="fa-solid fa-building"></i> ${escapeHTML(selectedJob.company)}`;
    drawerLocation.innerHTML = `<i class="fa-solid fa-location-dot"></i> ${escapeHTML(selectedJob.location || selectedJob.country)}`;
    // The tier leads, because it is the honest ranking: it orders only where one posting
    // dominates another on every dimension. The score beside it is a projection through a
    // weighting nothing validates, kept because some views need one number.
    drawerScore.textContent = selectedJob.pareto_tier != null
        ? `tier ${selectedJob.pareto_tier}`
        : (selectedJob.fit_score != null ? `${selectedJob.fit_score} fit` : 'Not yet scored');
    drawerSource.textContent = selectedJob.source;
    drawerApplyLink.href = selectedJob.url;
    renderVerdict(selectedJob);
    renderDossier(selectedJob);
    
    // Skills lists
    drawerSkillsList.innerHTML = selectedJob.matched_skills.map(skill => 
        `<span class="skill-tag text-purple">${skill}</span>`
    ).join('');
    if (selectedJob.matched_skills.length === 0) {
        drawerSkillsList.innerHTML = '<span class="skill-tag">No direct keyword overlap</span>';
    }
    
    // Highlight matched words in job description
    let highlightedDesc = escapeHTML(selectedJob.description);
    
    // Escape regex characters
    const escapeRegExp = (str) => str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    
    selectedJob.matched_skills.forEach(skill => {
        // Build regex with word boundary matching
        let regex;
        if (skill.includes('.') || skill.includes('/') || skill.includes('-')) {
            regex = new RegExp(escapeRegExp(skill), 'gi');
        } else {
            regex = new RegExp('\\b' + escapeRegExp(skill) + '\\b', 'gi');
        }
        
        highlightedDesc = highlightedDesc.replace(regex, (match) => 
            `<span class="highlight-term">${match}</span>`
        );
    });
    
    drawerDescription.innerHTML = highlightedDesc;
    
    // Active buttons state
    updatePillsState(selectedJob.status);
    
    // Show drawer
    jobDrawer.classList.add('active');
    drawerOverlay.classList.add('active');
}

function closeJobDrawer() {
    jobDrawer.classList.remove('active');
    drawerOverlay.classList.remove('active');
    selectedJob = null;
}

function updatePillsState(status) {
    actionSave.className = 'action-pill text-green';
    actionApply.className = 'action-pill text-blue';
    actionReject.className = 'action-pill text-red';
    
    if (status === 'saved') actionSave.className = 'action-pill text-green active';
    if (status === 'applied') actionApply.className = 'action-pill text-blue active';
    if (status === 'rejected') actionReject.className = 'action-pill text-red active';
}

async function updateStatus(newStatus) {
    if (!selectedJob) return;
    try {
        const res = await fetch(`/api/jobs/${selectedJob.id}/status`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus })
        });
        const data = await res.json();
        if (data.success) {
            selectedJob.status = newStatus;
            updatePillsState(newStatus);
            fetchStats();
            fetchJobs();
        }
    } catch (e) {
        console.error("Error updating status:", e);
    }
}

// Utility to escape HTML strings safely
function escapeHTML(str) {
    return str.replace(/[&<>'"]/g, 
        tag => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            "'": '&#39;',
            '"': '&quot;'
        }[tag] || tag)
    );
}

// --- LINK GENERATOR VIEW ---

function setupLinkGenerator() {
    generateLinksBtn.addEventListener('click', async () => {
        const resume = linkResumeSelect.value;
        const country = linkCountrySelect.value;
        const query = linkQuerySelect.value;
        
        if (!resume || !country || !query) return;
        
        generateLinksBtn.disabled = true;
        generateLinksBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Generating...';
        
        try {
            const res = await fetch(`/api/search-links?resume=${encodeURIComponent(resume)}&country=${country}&query=${encodeURIComponent(query)}`);
            if (res.status !== 200) {
                throw new Error("API error");
            }
            const links = await res.json();
            
            genRoleTitleEl.textContent = query;
            genCountryEl.textContent = linkCountrySelect.options[linkCountrySelect.selectedIndex].text;
            genQueryCode.textContent = links.search_query_used;
            
            linkedinSearchLink.href = links.linkedin;
            indeedSearchLink.href = links.indeed;
            
            generatedLinksResults.classList.remove('hide');
        } catch (e) {
            console.error("Failed to generate links:", e);
            alert("Failed to generate search links. Verify backend connection.");
        } finally {
            generateLinksBtn.disabled = false;
            generateLinksBtn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> Generate Custom Search Links';
        }
    });
}

// --- SETTINGS VIEW ---

function populateSettingsFields() {
    // Fill Target Roles queries
    settingsQueriesTextarea.value = configData.search_queries?.join('\n') || '';
    
    // Fill slider
    // Tier, not a percent. The old slider set min_match_score, which defaulted to the
    // floor of the scale it gated and so never filtered anything.
    settingsScoreSlider.value = (configData.matching || {}).max_tier || 10;
    settingsScoreValEl.textContent = settingsScoreSlider.value;
    
    // Fill credentials

    
    
    // Generate Countries checkboxes
    const allCountries = ["US", "FI", "SE", "NO", "DK"];
    const activeCountries = configData.countries || [];
    settingsCountriesContainer.innerHTML = '';
    
    allCountries.forEach(c => {
        const checked = activeCountries.includes(c) ? 'checked' : '';
        const name = c === 'US' ? 'USA' : c === 'FI' ? 'Finland' : c === 'SE' ? 'Sweden' : c === 'NO' ? 'Norway' : 'Denmark';
        const checkboxHTML = `
            <label class="checkbox-label">
                <input type="checkbox" name="country" value="${c}" ${checked}> ${name} (${c})
            </label>
        `;
        settingsCountriesContainer.insertAdjacentHTML('beforeend', checkboxHTML);
    });

    // Populate Query selection dropdown in link generator
    linkQuerySelect.innerHTML = '';
    configData.search_queries?.forEach(q => {
        linkQuerySelect.insertAdjacentHTML('beforeend', `<option value="${q}">${q}</option>`);
    });
    
    // Generate Source checkboxes
    const allSources = {
        indeed: "Indeed (volume source — full descriptions)",
        linkedin: "LinkedIn (budgeted supplement)",
    };
    // Sources moved under scraper.sources; fall back to the old flat key for an
    // un-migrated config file.
    const activeSources = (configData.scraper && configData.scraper.sources)
        || configData.sources || {};
    settingsSourcesContainer.innerHTML = '';
    
    Object.keys(allSources).forEach(key => {
        const checked = activeSources[key] ? 'checked' : '';
        const checkboxHTML = `
            <label class="checkbox-label">
                <input type="checkbox" name="source" value="${key}" ${checked}> ${allSources[key]}
            </label>
        `;
        settingsSourcesContainer.insertAdjacentHTML('beforeend', checkboxHTML);
    });
}

function setupSettingsForm() {
    settingsScoreSlider.addEventListener('input', () => {
        settingsScoreValEl.textContent = settingsScoreSlider.value;
    });
    
    settingsForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        // Collect checked countries
        const checkedCountries = Array.from(
            settingsForm.querySelectorAll('input[name="country"]:checked')
        ).map(el => el.value);
        
        // Collect checked sources
        const checkedSources = {};
        Array.from(
            settingsForm.querySelectorAll('input[name="source"]')
        ).forEach(el => {
            checkedSources[el.value] = el.checked;
        });
        
        // Collect queries
        const queries = settingsQueriesTextarea.value.split('\n')
            .map(q => q.trim())
            .filter(q => q.length > 0);
            
        // /api/config deep-merges server-side, so omitting a key leaves it untouched.
        // Posting a partial payload used to overwrite everything it did not mention.
        const payload = {
            countries: checkedCountries,
            search_queries: queries,

            scraper: { sources: checkedSources },
            matching: { max_tier: parseInt(settingsScoreSlider.value) },
        };
        
        try {
            const res = await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                alert("Configuration saved successfully!");
                configData = data.config;
                
                // Refresh query select dropdowns
                linkQuerySelect.innerHTML = '';
                configData.search_queries?.forEach(q => {
                    linkQuerySelect.insertAdjacentHTML('beforeend', `<option value="${q}">${q}</option>`);
                });
            }
        } catch (e) {
            console.error("Error saving settings:", e);
            alert("Error saving configurations.");
        }
    });
}

// --- SYNC ACTIONS & POLLING ---

function setupSync() {
    triggerSyncBtn.addEventListener('click', triggerSync);
    quickSyncBtn.addEventListener('click', triggerSync);
}

async function triggerSync() {
    // Disable buttons
    triggerSyncBtn.disabled = true;
    quickSyncBtn.disabled = true;
    
    // Set widget to active crawling
    syncWidgetStatusDot.className = 'status-dot orange';
    syncWidgetStatusText.textContent = 'Syncing Database...';
    
    // Show settings sync status banner
    syncProgressIndicator.classList.remove('hide');
    
    try {
        const res = await fetch('/api/sync', { method: 'POST' });
        if (res.status === 409) {
            console.log("Sync already in progress.");
        }
        
        // Start polling
        startSyncPolling();
    } catch (e) {
        console.error("Error triggering sync:", e);
        resetSyncUI();
    }
}

function startSyncPolling() {
    if (syncPollingInterval) clearInterval(syncPollingInterval);
    
    syncPollingInterval = setInterval(checkSyncStatus, 2000);
}

async function checkSyncStatus() {
    try {
        const res = await fetch('/api/sync/status');
        const data = await res.json();
        
        if (data.sync_in_progress) {
            // Disable sync controls if we refreshed tab and sync is active
            triggerSyncBtn.disabled = true;
            quickSyncBtn.disabled = true;
            syncWidgetStatusDot.className = 'status-dot orange';
            syncWidgetStatusText.textContent = 'Syncing Database...';
            if (currentTab === 'settings') {
                syncProgressIndicator.classList.remove('hide');
            }
        } else {
            // Not running
            if (syncPollingInterval) {
                clearInterval(syncPollingInterval);
                syncPollingInterval = null;
                // Sync just finished! Load fresh data
                loadAllData();
            }
            resetSyncUI();
            
            // Render last run stats if available
            if (data.last_run_stats && data.last_run_stats.total_fetched > 0) {
                syncStatsInfoEl.innerHTML = `
                    Last Sync Scanned: <strong>${data.last_run_stats.total_fetched.toLocaleString()}</strong> posts<br>
                    Evaluated: <strong>${data.last_run_stats.total_evaluated.toLocaleString()}</strong> fits<br>
                    Saved: <strong>${data.last_run_stats.total_new}</strong> new
                `;
                syncStatsInfoEl.classList.remove('hide');
            } else {
                syncStatsInfoEl.classList.add('hide');
            }
            
            // Check for errors to show in UI
            if (data.errors && data.errors.length > 0) {
                renderSyncErrors(data.errors);
            } else {
                if (syncErrorBanner) syncErrorBanner.classList.add('hide');
            }
        }
    } catch (e) {
        console.error("Error polling sync status:", e);
        if (syncPollingInterval) {
            clearInterval(syncPollingInterval);
            syncPollingInterval = null;
        }
        resetSyncUI();
    }
}

function renderSyncErrors(errors) {
    if (!syncErrorsList || !syncErrorBanner) return;
    
    syncErrorsList.innerHTML = '';
    // Severity distinguishes a hard failure from a coverage caveat or a notice. Rendering
    // "supply for these families is suppressed this window" in the same red as a rate-limit
    // trip trains the reader to ignore the banner.
    const ICONS = {
        error: 'fa-circle-exclamation',
        warning: 'fa-triangle-exclamation',
        info: 'fa-circle-info',
    };
    errors.forEach(err => {
        const timeStr = err.timestamp ? new Date(err.timestamp).toLocaleTimeString() : 'Unknown';
        const severity = err.severity || 'error';
        const liHTML = `
            <li class="sync-error-${escapeHTML(severity)}">
                <i class="fa-solid ${ICONS[severity] || ICONS.error}"></i>
                <strong>${escapeHTML(err.source)}</strong> [${escapeHTML(timeStr)}]:
                ${escapeHTML(err.error)}
            </li>
        `;
        syncErrorsList.insertAdjacentHTML('beforeend', liHTML);
    });
    syncErrorBanner.classList.remove('hide');
}

function resetSyncUI() {
    triggerSyncBtn.disabled = false;
    quickSyncBtn.disabled = false;
    syncProgressIndicator.classList.add('hide');
    
    syncWidgetStatusDot.className = 'status-dot green';
    syncWidgetStatusText.textContent = 'Database Idle';
}

// --- NAVIGATION TABS SWITCH ---

function setupTabSwitching() {
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const targetTab = item.getAttribute('data-tab');
            
            navItems.forEach(n => n.classList.remove('active'));
            tabPanes.forEach(p => p.classList.remove('active'));
            
            item.classList.add('active');
            document.getElementById(`tab-${targetTab}`).classList.add('active');
            
            currentTab = targetTab;
            
            // Format titles dynamically
            if (targetTab === 'dashboard') {
                pageTitle.textContent = 'Career Dashboard';
            } else if (targetTab === 'search-links') {
                pageTitle.textContent = 'Board Search Generators';
            } else if (targetTab === 'resumes') {
                pageTitle.textContent = 'My Resumes & Skill Profiles';
                renderResumesTab();
            } else if (targetTab === 'digests') {
                pageTitle.textContent = 'Daily Job Digests';
                loadDigests();
            } else if (targetTab === 'market') {
                pageTitle.textContent = 'Market Supply';
                loadMarketTab();
            } else if (targetTab === 'skills') {
                pageTitle.textContent = 'Skill Gap Analysis';
                loadSkillsTab();
            } else if (targetTab === 'settings') {
                pageTitle.textContent = 'Radar Configurations';
                // Double check sync visual banner
                checkSyncStatus();
            }
        });
    });
}

// --- DIGESTS SECTION ---

async function loadDigests() {
    const listContainer = document.getElementById('digests-list-container');
    if (!listContainer) return;
    
    listContainer.innerHTML = `
        <div class="digests-loading" style="padding: 20px; text-align: center; color: var(--text-muted);">
            <i class="fa-solid fa-circle-notch fa-spin text-purple" style="margin-right: 8px;"></i> Loading digests...
        </div>
    `;
    
    try {
        const res = await fetch('/api/digests');
        const digests = await res.json();
        renderDigestsList(digests);
    } catch (e) {
        console.error("Error loading digests:", e);
        listContainer.innerHTML = '<div class="no-digests-text error" style="color: var(--text-red); padding: 15px;">Error loading digests.</div>';
    }
}

function renderDigestsList(digests) {
    const listContainer = document.getElementById('digests-list-container');
    if (!listContainer) return;
    
    if (!digests || digests.length === 0) {
        listContainer.innerHTML = '<div class="no-digests-text" style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.9rem;">No digests found yet. Run the scraper sync to generate a digest of matches!</div>';
        return;
    }
    
    listContainer.innerHTML = '';
    digests.forEach(d => {
        const sizeKB = (d.size_bytes / 1024).toFixed(1);
        const itemHTML = `
            <div class="digest-list-item" data-filename="${d.filename}" style="padding: 12px 16px; border-radius: 8px; margin-bottom: 8px; cursor: pointer; transition: all 0.2s ease; border: 1px solid var(--border-color); background: rgba(255, 255, 255, 0.02); display: flex; align-items: center; gap: 12px;">
                <div class="digest-item-icon" style="background: rgba(147, 51, 234, 0.1); width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
                    <i class="fa-solid fa-envelope-open-text" style="color: #c084fc;"></i>
                </div>
                <div class="digest-item-details" style="display: flex; flex-direction: column; overflow: hidden;">
                    <span class="digest-item-date" style="font-weight: 500; font-size: 0.85rem; color: var(--text-main); white-space: nowrap; text-overflow: ellipsis; overflow: hidden;">${d.date_created}</span>
                    <span class="digest-item-size" style="font-size: 0.75rem; color: var(--text-muted); margin-top: 2px;">${sizeKB} KB • Markdown Report</span>
                </div>
            </div>
        `;
        listContainer.insertAdjacentHTML('beforeend', itemHTML);
    });
    
    // Add event listeners
    const items = listContainer.querySelectorAll('.digest-list-item');
    items.forEach(item => {
        item.addEventListener('click', () => {
            items.forEach(i => {
                i.style.borderColor = 'var(--border-color)';
                i.style.background = 'rgba(255, 255, 255, 0.02)';
            });
            item.style.borderColor = 'var(--purple-main, #a855f7)';
            item.style.background = 'rgba(147, 51, 234, 0.05)';
            
            const filename = item.getAttribute('data-filename');
            const dateStr = item.querySelector('.digest-item-date').textContent;
            selectDigest(filename, dateStr);
        });
    });
}

async function selectDigest(filename, formattedDate) {
    const titleEl = document.getElementById('digest-title');
    const metaEl = document.getElementById('digest-meta');
    const bodyEl = document.getElementById('digest-view-body');
    
    if (!bodyEl) return;
    
    bodyEl.innerHTML = `
        <div class="digests-loading" style="padding: 40px; text-align: center; color: var(--text-muted);">
            <i class="fa-solid fa-circle-notch fa-spin text-purple" style="margin-right: 8px;"></i> Loading digest content...
        </div>
    `;
    
    try {
        const res = await fetch(`/api/digests/${filename}`);
        const data = await res.json();
        
        if (titleEl) titleEl.textContent = `Job Search Digest`;
        if (metaEl) metaEl.textContent = `Generated on ${formattedDate} | File: ${filename}`;
        
        bodyEl.innerHTML = convertMarkdownToHTML(data.content);
    } catch (e) {
        console.error("Error loading digest content:", e);
        bodyEl.innerHTML = '<div class="no-digests-text error" style="color: var(--text-red); padding: 20px;">Error loading digest content.</div>';
    }
}

function convertMarkdownToHTML(md) {
    if (!md) return '';
    let html = md;
    
    // Escape HTML tags to prevent XSS
    html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    
    // Replace headers
    html = html.replace(/^# (.*?)$/gm, '<h2 style="font-size: 1.5rem; margin-top: 0; margin-bottom: 8px; color: var(--text-main); font-weight: 600;">$1</h2>');
    html = html.replace(/^## (.*?)$/gm, '<h3 style="font-size: 1.25rem; margin-top: 20px; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">$1</h3>');
    html = html.replace(/^### (.*?)$/gm, '<h4 style="font-size: 1.1rem; margin-top: 15px; margin-bottom: 6px; color: var(--text-main);">$1</h4>');
    
    // Replace bold
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    
    // Replace links (need to handle &amp; from escaping earlier)
    html = html.replace(/\[(.*?)\]\((.*?)\)/g, '<a href="$2" target="_blank" style="color: var(--link-color, #c084fc); text-decoration: none; font-weight: 500; display: inline-flex; align-items: center; gap: 4px;">$1 <i class="fa-solid fa-up-right-from-square" style="font-size: 0.7rem;"></i></a>');
    
    // Parse tables
    const lines = html.split('\n');
    let inTable = false;
    let tableHTML = '<div style="overflow-x: auto; margin-top: 15px; margin-bottom: 15px;"><table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem;">';
    let newLines = [];
    
    for (let line of lines) {
        line = line.trim();
        if (line.startsWith('|')) {
            if (!inTable) {
                inTable = true;
                tableHTML += '<thead>';
            }
            
            const cells = line.split('|').slice(1, -1).map(c => c.trim());
            
            if (line.includes(':---') || line.includes('---:')) {
                continue;
            }
            
            const isHeaderRow = inTable && !tableHTML.includes('<tbody>');
            const rowStyle = isHeaderRow 
                ? 'border-bottom: 2px solid rgba(255, 255, 255, 0.1); padding: 10px 12px; font-weight: 600; color: var(--text-main);'
                : 'border-bottom: 1px solid rgba(255, 255, 255, 0.05); padding: 12px 12px; color: var(--text-muted);';
            
            tableHTML += '<tr>';
            for (let cell of cells) {
                const tag = isHeaderRow ? 'th' : 'td';
                tableHTML += `<${tag} style="${rowStyle}">${cell}</${tag}>`;
            }
            tableHTML += '</tr>';
            
            if (isHeaderRow) {
                tableHTML += '</thead><tbody>';
            }
        } else {
            if (inTable) {
                inTable = false;
                tableHTML += '</tbody></table></div>';
                newLines.push(tableHTML);
                tableHTML = '<div style="overflow-x: auto; margin-top: 15px; margin-bottom: 15px;"><table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem;">';
            }
            if (line) {
                if (line.startsWith('*') && line.endsWith('*')) {
                    newLines.push(`<p style="font-style: italic; color: var(--text-muted); font-size: 0.85rem; margin-top: 15px;">${line.replace(/\*/g, '')}</p>`);
                } else {
                    newLines.push(`<p style="color: var(--text-muted); line-height: 1.6; margin-bottom: 10px;">${line}</p>`);
                }
            } else {
                newLines.push('<div style="height: 8px;"></div>');
            }
        }
    }
    
    if (inTable) {
        tableHTML += '</tbody></table></div>';
        newLines.push(tableHTML);
    }
    
    return newLines.join('\n');
}

/* ==========================================================================
 * MARKET SUPPLY TAB
 *
 * Small multiples, one panel per location. There is deliberately no pooled
 * cross-location ranking: flow is comparable only within a single location and
 * source, so pooling would be the one comparison that isn't valid.
 * ========================================================================== */

let marketLocations = null;

async function loadMarketTab() {
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

/* ==========================================================================
 * SKILL GAP TAB
 * ========================================================================== */

async function loadSkillsTab() {
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
    try {
        const res = await fetch(`/api/skills/${encodeURIComponent(skill)}`);
        if (!res.ok) return;
        const d = await res.json();

        drawerScore.textContent = d.user_has ? `Level ${d.user_level}` : 'Not on resume';
        drawerSource.textContent = d.category;
        drawerTitle.textContent = d.label;
        drawerCompany.innerHTML =
            `<i class="fa-solid fa-briefcase"></i> ${d.postings.length} postings`;
        drawerLocation.innerHTML = `<i class="fa-solid fa-clock"></i> last ${d.window_days} days`;
        // The skill drawer reuses the job drawer's shell, so it writes into the verdict
        // slot rather than a resume tag that no longer exists.
        drawerVerdict.innerHTML = d.evidence.length
            ? `<p class="verdict-reasoning">${escapeHTML(d.evidence.join(' · '))}</p>`
            : '<p class="verdict-empty">No evidence for this skill in your profile.</p>';
        drawerDossierSection.style.display = 'none';

        drawerSkillsList.innerHTML = d.cooccurring.map((c) =>
            `<span class="skill-tag ${c.user_has ? '' : 'is-missing'}">
               ${Charts.esc(c.label)} <em>${c.n}</em></span>`).join('')
            || '<span class="skill-tag">No co-occurring skills</span>';

        drawerDescription.innerHTML = `
            <h4>Where it appears</h4>
            <ul class="detail-list">${d.by_role_family.map((f) =>
                `<li>${Charts.esc(f.label)} <strong>${f.n}</strong></li>`).join('')}</ul>
            <h4>Postings requiring it</h4>
            <ul class="detail-list">${d.postings.slice(0, 25).map((j) =>
                `<li><a href="${Charts.esc(j.url)}" target="_blank" rel="noopener">
                   ${Charts.esc(j.title)}</a>
                   <span class="detail-meta">${Charts.esc(j.company)} ·
                   score ${j.match_score}${j.in_title ? ' · in title' : ''}</span></li>`)
                .join('')}</ul>`;

        // Reuse the existing job drawer rather than adding a second overlay component.
        drawerApplyLink.style.display = 'none';
        jobDrawer.classList.add('active');
        drawerOverlay.classList.add('active');
    } catch (e) {
        console.error('skill detail failed', e);
    }
}

/* Control wiring */
document.addEventListener('DOMContentLoaded', () => {
    ['market-source', 'market-window'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', loadMarketTab);
    });
    ['skills-window', 'skills-weighting'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', loadSkillsTab);
    });
});


/* Reachability filter. candidates have local or remote preferences, so "can I take this job without
 * moving?" is the first question about any posting -- ahead of score. */
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.access-toggle-grid .status-pill').forEach((pill) => {
        pill.addEventListener('click', () => {
            document.querySelectorAll('.access-toggle-grid .status-pill')
                .forEach((p) => p.classList.remove('active'));
            pill.classList.add('active');
            currentAccess = pill.dataset.access || '';
            fetchJobs();
        });
    });
});


/* Reachability badge. Answers "could I take this without moving?" at a glance, which for a
 * Los Angeles-based search matters before the match score does. */
function accessBadge(access) {
    const BADGES = {
        commutable: ['fa-house', 'LA area', 'is-commutable',
                     'Within commuting distance — no relocation or remote arrangement needed'],
        remote: ['fa-wifi', 'Remote', 'is-remote', 'Remote, so location is not a constraint'],
        relocation: ['fa-plane', 'Relocate', 'is-relocation',
                     'Onsite somewhere you would have to move to'],
    };
    const badge = BADGES[access];
    if (!badge) return '';
    const [icon, label, cls, tip] = badge;
    return `<span class="access-badge ${cls}" title="${escapeHTML(tip)}">`
         + `<i class="fa-solid ${icon}"></i> ${escapeHTML(label)}</span>`;
}

// --- Verdict + dossier ------------------------------------------------------------------

const VERDICT_LABELS = {
    strong: 'Strong match',
    worth_applying: 'Worth applying',
    stretch: 'Stretch',
    poor_fit: 'Poor fit',
    mismatch: 'Mismatch',
};

function bulletList(items, className) {
    if (!items || !items.length) return '';
    return `<ul class="verdict-list ${className || ''}">` +
        items.map(i => `<li>${escapeHTML(i)}</li>`).join('') + '</ul>';
}

function renderVerdict(job) {
    if (job.fit_score == null || !job.verdict) {
        drawerVerdict.innerHTML =
            `<p class="verdict-empty">Not scored yet. This posting is queued
             (<code>${escapeHTML(job.pipeline_state || 'new')}</code>); the scoring stage
             will pick it up on its next run.</p>
             <p class="verdict-empty">${job.required_count ? `${job.matched_count || 0} of ${job.required_count} named skills evidenced` : 'No recognised skills in this posting'}</p>`;
        return;
    }

    const blockers = job.hard_blockers || [];
    const parts = [];

    parts.push(`
        <div class="verdict-head">
            <span class="verdict-badge verdict-${escapeHTML(job.verdict)}">
                ${escapeHTML(VERDICT_LABELS[job.verdict] || job.verdict)}
            </span>
            ${job.pareto_tier != null
                ? `<span class="verdict-score" title="1 dominates everything below it">tier ${job.pareto_tier}</span>`
                : `<span class="verdict-score">${job.fit_score}/100</span>`}
        </div>`);

    // What the model understood the job to BE. The single most useful line in the drawer:
    // it is where a posting judged on shared vocabulary rather than shared work gives
    // itself away.
    if (job.role_summary) {
        parts.push(`<p class="verdict-summary">${escapeHTML(job.role_summary)}</p>`);
    }

    // The five answers the score was computed from. Showing them rather than the number
    // alone is the point of the whole schema: every score decomposes into named
    // judgements, each of which can be disagreed with individually.
    const ordinals = [
        ['eligibility', job.eligibility],
        ['role', job.role_match],
        ['capability', job.capability_match],
        ['seniority', job.seniority_gap],
        ['evidence', job.evidence_quality],
    ].filter(([, v]) => v);
    if (ordinals.length) {
        parts.push('<div class="verdict-ordinals">' + ordinals.map(([k, v]) =>
            `<span class="ordinal"><b>${k}</b> ${escapeHTML(v.replace(/_/g, ' '))}</span>`
        ).join('') + '</div>');
    }

    if (job.liveness === 'likely_closed') {
        parts.push(`<p class="verdict-stale">This posting did not reappear the last time
            its search was run, so it has probably closed.</p>`);
    }

    if (job.reasoning) {
        parts.push(`<p class="verdict-reasoning">${escapeHTML(job.reasoning)}</p>`);
    }

    // Blockers first and quoted: each one is a phrase lifted from the posting, so the
    // claim can be checked against the source rather than taken on faith. The reasoning
    // sits under the quote rather than inside it -- the two were one string until the
    // scorer started losing verdicts over which half the auditor was reading.
    if (blockers.length) {
        parts.push(`<h5 class="verdict-h5 text-red">Hard blockers (${blockers.length})</h5>`);
        parts.push('<ul class="verdict-list verdict-blockers">' + blockers.map(b => {
            // Verdicts written before the field was split are plain strings.
            const quote = typeof b === 'string' ? b : (b.quote || '');
            const why = typeof b === 'string' ? '' : (b.why || '');
            return `<li><q>${escapeHTML(quote)}</q>` +
                (why ? `<span class="blocker-why">${escapeHTML(why)}</span>` : '') +
                '</li>';
        }).join('') + '</ul>');
    }
    if ((job.key_gaps || []).length) {
        parts.push('<h5 class="verdict-h5">Gaps</h5>' + bulletList(job.key_gaps));
    }
    if ((job.strengths || []).length) {
        parts.push('<h5 class="verdict-h5 text-green">You bring</h5>' + bulletList(job.strengths));
    }
    parts.push(`<p class="verdict-foot">${job.required_count ? `${job.matched_count || 0} of ${job.required_count} named skills evidenced` : 'No recognised skills in this posting'}</p>`);

    drawerVerdict.innerHTML = parts.join('');
}

async function renderDossier(job) {
    drawerDossierSection.style.display = 'none';
    if (!job.company) return;
    try {
        const res = await fetch(`/api/companies/${encodeURIComponent(job.company)}/dossier`);
        if (!res.ok) return;
        const d = await res.json();
        const intel = d.intel || {};
        const parts = [];

        if (intel.summary) parts.push(`<p>${escapeHTML(intel.summary)}</p>`);
        const facts = [
            intel.size && `size ${intel.size}`,
            intel.stage && `stage ${intel.stage}`,
            intel.funding && `funding ${intel.funding}`,
        ].filter(Boolean);
        if (facts.length) {
            parts.push(`<p class="dossier-facts">${escapeHTML(facts.join(' · '))}</p>`);
        }
        if ((intel.concerns || []).length) {
            parts.push('<h5 class="verdict-h5 text-red">Concerns</h5>' + bulletList(intel.concerns));
        }
        if (intel.application_angle) {
            parts.push('<h5 class="verdict-h5">Angle</h5>' +
                `<p>${escapeHTML(intel.application_angle)}</p>`);
        }
        if ((d.contacts || []).length) {
            parts.push('<h5 class="verdict-h5">People</h5><ul class="verdict-list">' +
                d.contacts.map(c => {
                    const who = `${escapeHTML(c.name)}${c.role ? ' — ' + escapeHTML(c.role) : ''}`;
                    const link = c.public_url
                        ? ` <a href="${escapeHTML(c.public_url)}" target="_blank" rel="noopener">↗</a>` : '';
                    return `<li>${who}${link}<br><small>${escapeHTML(c.relevance || '')}</small></li>`;
                }).join('') + '</ul>');
        }
        if ((d.nearby_jobs || []).length) {
            parts.push(`<h5 class="verdict-h5">Other openings (${d.nearby_jobs.length})</h5>` +
                '<ul class="verdict-list">' + d.nearby_jobs.slice(0, 8).map(n => {
                    const link = n.url
                        ? `<a href="${escapeHTML(n.url)}" target="_blank" rel="noopener">${escapeHTML(n.title)}</a>`
                        : escapeHTML(n.title);
                    return `<li>${link} — ${escapeHTML(n.company)} <small>(${escapeHTML(n.source)})</small></li>`;
                }).join('') + '</ul>');
        }
        // Sources come from what was actually fetched. An empty list means no web research
        // happened, and saying so is the point -- see careerradar/research/graph.py.
        parts.push((d.sources || []).length
            ? `<p class="dossier-sources">${d.sources.length} source(s): ` +
              d.sources.slice(0, 6).map(u =>
                  `<a href="${escapeHTML(u)}" target="_blank" rel="noopener">${escapeHTML(new URL(u).hostname)}</a>`
              ).join(', ') + '</p>'
            : '<p class="dossier-sources">No web sources — company intel was not gathered.</p>');

        drawerDossier.innerHTML = parts.join('');
        drawerDossierSection.style.display = '';
    } catch (e) {
        /* no dossier for this company */
    }
}
