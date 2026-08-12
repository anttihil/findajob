// Escape a string for interpolation into markup.
//
// Coerces rather than assuming a string: every caller passes a column straight out of the
// API, and a NULL one used to throw a TypeError out of whichever renderer touched it.
//
// The dashboard feed and the job drawer no longer come through here at all -- they are
// rendered by Jinja, which escapes by default. What is left are the analytics views, whose
// markup is still built in the browser.
export function escapeHTML(str) {
    return String(str ?? '').replace(/[&<>'"]/g,
        tag => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            "'": '&#39;',
            '"': '&quot;'
        }[tag] || tag)
    );
}
