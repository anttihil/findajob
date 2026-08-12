
export function convertMarkdownToHTML(md) {
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

