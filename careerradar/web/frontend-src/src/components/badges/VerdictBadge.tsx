import type { Verdict } from "../../api/types";
import { VERDICT_LABELS } from "../../lib/verdictLabels";

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return <span class={`verdict-badge verdict-${verdict}`}>{VERDICT_LABELS[verdict] ?? verdict}</span>;
}
