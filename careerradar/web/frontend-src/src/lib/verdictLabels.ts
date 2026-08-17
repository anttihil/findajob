import type { Verdict } from "../api/types";

export const VERDICT_LABELS: Record<Verdict, string> = {
  strong: "Strong match",
  worth_applying: "Worth applying",
  stretch: "Stretch",
  poor_fit: "Poor fit",
  mismatch: "Mismatch",
};
