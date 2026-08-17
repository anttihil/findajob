// Ported from `rendering.short_date` / `rendering.hostname`.

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** "Jul 15" -- no leading zero, no year. */
export function shortDate(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}`;
}

/** Bare host for a dossier source link, matching `new URL(u).hostname`. */
export function hostname(url: string): string {
  try {
    return new URL(url).hostname || url;
  } catch {
    return url;
  }
}
