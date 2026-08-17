// Palette validated against the dark surface with the six checks -- lightness band, chroma
// floor, CVD separation, normal-vision floor, contrast:
//   #a855f7 (accent) + #c08217 (censored/warning): all PASS, deutan dE 32.3
// The gap-component meters use a single-hue SEQUENTIAL ramp rather than categorical hues,
// because the three components are parts of one composite score and each is directly
// labelled -- so color is not carrying identity, and a purple/blue categorical pair would
// have been indistinguishable under deuteranopia (dE 0.9).
//
// Ported byte-for-byte from `frontend/js/charts.js` -- this is empirical color science, not
// something to touch during the framework rewrite.
export const CHART = {
  accent: "#a855f7",
  accentDim: "rgba(168, 85, 247, 0.28)",
  censored: "#c08217",
  ramp: ["#c4a5fb", "#a855f7", "#7c3aed"],
  track: "rgba(255,255,255,0.06)",
  grid: "rgba(255,255,255,0.07)",
  BAR_H: 18,
  BAR_GAP: 12,
  RADIUS: 4,
} as const;

export function esc(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      (
        {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        } as Record<string, string>
      )[c]
  );
}
