import { Link, useLocation } from "wouter-preact";
import { SyncStatusWidget } from "./SyncStatusWidget";

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
      {/* 1983 NETLINK-style Hatched Logo */}
      <div class="retro-logo-container">
        <div class="hatched-logo-box">
          <svg class="hatched-logo-svg" viewBox="0 0 54 54" width="54" height="54">
            <defs>
              <pattern id="retroHatch" width="4" height="4" patternUnits="userSpaceOnUse">
                <line x1="0" y1="2" x2="4" y2="2" stroke="#000000" stroke-width="1.8" />
              </pattern>
            </defs>
            {/* Outer Box */}
            <rect x="2" y="2" width="50" height="50" fill="none" stroke="#000000" stroke-width="2" />
            {/* Bold Hatched 'N' / 'CR' Symbol */}
            <path
              d="M 10 10 L 18 10 L 18 30 L 36 10 L 44 10 L 44 44 L 36 44 L 36 24 L 18 44 L 10 44 Z"
              fill="url(#retroHatch)"
              stroke="#000000"
              stroke-width="1.5"
            />
          </svg>
        </div>
        <div class="hatched-logo-caption">CAREERRADAR</div>
      </div>

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

      <SyncStatusWidget />
    </aside>
  );
}
