export const SOURCE_LABELS: Record<string, string> = {
  indeed: "Indeed",
  linkedin: "LinkedIn",
  google: "Google Jobs",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] || source;
}
