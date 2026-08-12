// Daily digest browser.

import { getJSON, reportError } from '../api.js';
import { convertMarkdownToHTML } from '../lib/markdown.js';

export async function loadDigests() {
    const listContainer = document.getElementById('digests-list-container');
    if (!listContainer) return;
    
    listContainer.innerHTML = `
        <div class="digests-loading" style="padding: 20px; text-align: center; color: var(--text-muted);">
            <i class="fa-solid fa-circle-notch fa-spin text-purple" style="margin-right: 8px;"></i> Loading digests...
        </div>
    `;
    
    try {
        renderDigestsList(await getJSON('/api/digests'));
    } catch (err) {
        reportError('Loading digests', err);
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
        const data = await getJSON(`/api/digests/${encodeURIComponent(filename)}`);
        
        if (titleEl) titleEl.textContent = `Job Search Digest`;
        if (metaEl) metaEl.textContent = `Generated on ${formattedDate} | File: ${filename}`;
        
        bodyEl.innerHTML = convertMarkdownToHTML(data.content);
    } catch (err) {
        reportError(`Loading digest ${filename}`, err);
        bodyEl.innerHTML = '<div class="no-digests-text error" style="color: var(--text-red); padding: 20px;">Error loading digest content.</div>';
    }
}
