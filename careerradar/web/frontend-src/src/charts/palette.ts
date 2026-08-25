export const CHART = {
  accent: "#000000",
  accentDim: "rgba(0, 0, 0, 0.15)",
  censored: "#555555",
  ramp: ["#000000", "#444444", "#777777"],
  track: "rgba(0,0,0,0.08)",
  grid: "rgba(0,0,0,0.15)",
  BAR_H: 18,
  BAR_GAP: 12,
  RADIUS: 0,
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

