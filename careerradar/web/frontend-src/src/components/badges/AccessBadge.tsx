import type { Access } from "../../api/types";

// Answers "could I take this without moving?" at a glance. Ported from
// `macros/badges.html::access_badge`.
const ACCESS_BADGES: Record<Access, [icon: string, label: string, cls: string, tooltip: string]> = {
  commutable: [
    "fa-house",
    "LA area",
    "is-commutable",
    "Within commuting distance — no relocation or remote arrangement needed",
  ],
  remote: ["fa-wifi", "Remote", "is-remote", "Remote, so location is not a constraint"],
  relocation: ["fa-plane", "Relocate", "is-relocation", "Onsite somewhere you would have to move to"],
};

export function AccessBadge({ access }: { access: Access | null | undefined }) {
  const badge = access ? ACCESS_BADGES[access] : undefined;
  if (!badge) return null;
  const [icon, label, cls, tooltip] = badge;
  return (
    <span class={`access-badge ${cls}`} title={tooltip}>
      <i class={`fa-solid ${icon}`}></i> {label}
    </span>
  );
}
