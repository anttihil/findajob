import { Link, useLocation } from "wouter-preact";

const NAV_ITEMS: { href: string; icon: string; label: string }[] = [
  { href: "/", icon: "fa-satellite-dish", label: "JOBS" },
  { href: "/resumes", icon: "fa-file-invoice", label: "RESUMES" },
  { href: "/market", icon: "fa-chart-simple", label: "MARKET" },
  { href: "/skills", icon: "fa-arrow-trend-up", label: "SKILLS" },
  { href: "/observability", icon: "fa-gauge-high", label: "OBSERVABILITY" },
];


export function Sidebar() {
  const [location] = useLocation();

  return (
    <aside class="sidebar">
      <nav class="nav-menu">
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            class={`nav-item ${location === item.href ? "active" : ""}`}
          >
            <i class={`fa-solid ${item.icon}`}></i>
            <span>{item.label}</span>
          </Link>
        ))}
      </nav>
    </aside>
  );
}
