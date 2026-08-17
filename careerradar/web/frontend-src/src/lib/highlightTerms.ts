// Ported from `rendering.highlight_terms`, but restructured for Preact: instead of building
// an HTML string and injecting it via `dangerouslySetInnerHTML` (the direct translation of
// the Python version's `Markup(text)`), this returns plain segments that a component renders
// as ordinary Preact children. Preact escapes text children automatically, so this sidesteps
// needing `dangerouslySetInnerHTML` at all -- strictly safer than the string-building
// approach, not just an equivalent port of it.
//
// Also fixes a latent bug the sequential `re.sub` version had: highlighting one skill could
// wrap text in a `<span>` that a later skill's regex would then partially match inside,
// corrupting the markup. Matching all skills in one pass, longest-first, avoids that.

export interface HighlightSegment {
  text: string;
  highlighted: boolean;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function highlightTerms(
  description: string | null | undefined,
  skills: readonly string[] | null | undefined
): HighlightSegment[] {
  const text = description ?? "";
  const validSkills = (skills ?? []).filter((s) => !!s);
  if (!text || !validSkills.length) return [{ text, highlighted: false }];

  // Longest first, so "React Native" is matched whole rather than "React" claiming part of
  // it and leaving "Native" to match again on its own.
  const sorted = [...validSkills].sort((a, b) => b.length - a.length);
  const patterns = sorted.map((skill) => {
    const escaped = escapeRegExp(skill);
    // Word boundaries would not survive skills like "node.js", "CI/CD" or "front-end",
    // whose punctuation is not a word character.
    return /[./-]/.test(skill) ? escaped : `\\b${escaped}\\b`;
  });
  const regex = new RegExp(patterns.join("|"), "gi");

  const segments: HighlightSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(regex)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      segments.push({ text: text.slice(lastIndex, index), highlighted: false });
    }
    segments.push({ text: match[0], highlighted: true });
    lastIndex = index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ text: text.slice(lastIndex), highlighted: false });
  }
  return segments;
}
