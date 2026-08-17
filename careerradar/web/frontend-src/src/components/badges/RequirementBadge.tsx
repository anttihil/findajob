import type { Job } from "../../api/types";

// Must-haves met, out of the must-haves this posting actually states. Ported from
// `macros/badges.html::requirement_badge`. The summary is computed server-side in
// `database._requirement_summary`, since the join is on normalised requirement text and
// that normalisation has to match `profile/models.py` -- one implementation, server-side.
export function RequirementBadge({ job }: { job: Job }) {
  const s = job.requirement_summary;
  if (!s) return null;

  // Anything short of `met` is named rather than folded into the numerator: "4/6" with two
  // partials is a different application from "4/6" with two unmets.
  const rest = [
    s.must_partial ? `${s.must_partial} partial` : "",
    s.must_unmet ? `${s.must_unmet} unmet` : "",
    s.must_unassessed ? `${s.must_unassessed} unassessed` : "",
  ]
    .filter(Boolean)
    .join(", ");
  const cls = s.must_met === s.must_total ? "req-all" : s.must_unmet ? "req-gaps" : "req-partial";

  return (
    <span
      class={`requirement-badge ${cls}`}
      title={`must-haves this posting states${rest ? ` — ${rest}` : ""}`}
    >
      {s.must_met}/{s.must_total} must-haves
    </span>
  );
}
