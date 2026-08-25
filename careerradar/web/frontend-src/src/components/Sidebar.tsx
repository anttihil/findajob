import { Link, useLocation } from "wouter-preact";
import { SyncStatusWidget } from "./SyncStatusWidget";

const NAV_ITEMS: { href: string; icon: string; label: string }[] = [
  { href: "/", icon: "fa-house", label: "Dashboard" },
  { href: "/market", icon: "fa-chart-simple", label: "Market Supply" },
  { href: "/skills", icon: "fa-arrow-trend-up", label: "Skill Gaps" },
  { href: "/observability", icon: "fa-gauge-high", label: "Model Observability" },
  { href: "/resumes", icon: "fa-file-invoice", label: "Resumes & Skills" },
  { href: "/settings", icon: "fa-sliders", label: "Settings & Sync" },
];


// Ported from `partials/sidebar.html`.
export function Sidebar() {
  const [location] = useLocation();

  return (
    <aside class="sidebar">
      <div class="logo">
        <div class="logo-icon">
          <i class="fa-solid fa-satellite-dish animate-pulse"></i>
        </div>
        <div class="logo-text">
          <h2>CareerRadar</h2>
          <span>Daily Job Intelligence</span>
        </div>
      </div>

      <nav class="nav-menu">
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            class={`nav-item ${location === item.href ? "active" : ""}`}
          >
            <i class={`fa-solid ${item.icon}`}></i> {item.label}
          </Link>
        ))}
      </nav>

      <SyncStatusWidget />
    </aside>
  );
}
