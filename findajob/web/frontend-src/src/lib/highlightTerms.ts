// Return escaped text segments rather than injecting generated HTML. Matching all skills in
// one pass, longest-first, also prevents overlapping terms from corrupting the markup.

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
