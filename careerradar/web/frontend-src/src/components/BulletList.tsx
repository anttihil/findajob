// Ported from `macros/badges.html::bullet_list`.
export function BulletList({ items, className = "" }: { items: string[] | null | undefined; className?: string }) {
  if (!items || !items.length) return null;
  return (
    <ul class={`verdict-list ${className}`}>
      {items.map((item, i) => (
        <li key={i}>{item}</li>
      ))}
    </ul>
  );
}
