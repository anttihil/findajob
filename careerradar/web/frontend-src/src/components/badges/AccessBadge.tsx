import type { Access } from "../../api/types";

// Answers "could I take this without moving?" at a glance. Ported from
// `macros/badges.html::access_badge`.
const ACCESS_BADGES: Record<Access, [icon: string, label: string, cls: string, tooltip: string]> = {
  commutable: [
    "fa-house",
    "Local",
    "is-commutable",
    "Local to search location / within commuting distance",
  ],
  remote: ["fa-wifi", "Remote", "is-remote", "Remote work arrangement — not bound to a physical office"],
  relocation: ["fa-building", "Onsite", "is-relocation", "Onsite at target search location"],
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
