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
const drawerMatchedResume = document.getElementById('drawer-matched-resume');
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
        if (resume) queryParams.push(`resume_match=${resume}`);
        if (score) queryParams.push(`min_score=${score}`);
        
        const url = `/api/jobs?${queryParams.join('&')}`;
        const res = await fetch(url);
        jobsData = await res.json();
        
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
        statAvgScoreEl.textContent = `${stats.avg_match_score || 0}%`;
    } catch (e) {
        console.error("Error fetching stats:", e);
    }
}

// --- UI RENDERING ---

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
            return b.match_score - a.match_score;
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
                        <h4>${job.title}</h4>
                    </div>
                    <div class="job-company">${job.company}</div>
                    <div class="job-meta-row">
                        <span><i class="fa-solid fa-location-dot"></i> ${job.location || 'Remote'}</span>
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
                        <span class="match-badge">${job.match_score}% Match</span>
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
    drawerCompany.innerHTML = `<i class="fa-solid fa-building"></i> ${selectedJob.company}`;
    drawerLocation.innerHTML = `<i class="fa-solid fa-location-dot"></i> ${selectedJob.location || selectedJob.country}`;
    drawerScore.textContent = `${selectedJob.match_score}% Match`;
    drawerSource.textContent = selectedJob.source;
    drawerMatchedResume.textContent = selectedJob.resume_match;
    drawerApplyLink.href = selectedJob.url;
    
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
    settingsScoreSlider.value = configData.min_match_score || 15;
    settingsScoreValEl.textContent = `${settingsScoreSlider.value}%`;
    
    // Fill credentials

    
    const gmailEmailInput = document.getElementById('gmail-email');
    if (gmailEmailInput) {
        gmailEmailInput.value = configData.gmail_imap?.email || '';
    }
    
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
        gmail_imap: "Gmail IMAP (Job Alerts)"
    };
    const activeSources = configData.sources || {};
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
        settingsScoreValEl.textContent = `${settingsScoreSlider.value}%`;
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
            .map(q => q.strip ? q.strip() : q.trim())
            .filter(q => q.length > 0);
            
        const gmailEmailInput = document.getElementById('gmail-email');
        const payload = {
            countries: checkedCountries,
            search_queries: queries,
            min_match_score: parseInt(settingsScoreSlider.value),
            sources: checkedSources,

            gmail_imap: {
                enabled: checkedSources.gmail_imap || false,
                email: gmailEmailInput ? gmailEmailInput.value.trim() : '',
                password_env_var: "GMAIL_APP_PASSWORD",
                imap_server: "imap.gmail.com",
                imap_port: 993
            }
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
    errors.forEach(err => {
        const timeStr = err.timestamp ? new Date(err.timestamp).toLocaleTimeString() : 'Unknown';
        const liHTML = `
            <li>
                <strong>${err.source}</strong> [${timeStr}]: ${err.error}
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
