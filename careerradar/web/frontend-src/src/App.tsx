import { useEffect, useState } from "preact/hooks";
import { Redirect, Route, Switch, useLocation } from "wouter-preact";
import { Sidebar } from "./components/Sidebar";
import { ErrorBanner } from "./components/ErrorBanner";
import { ModalHost } from "./components/ModalHost";
import { DashboardPage } from "./routes/dashboard/DashboardPage";
import { MarketPage } from "./routes/market/MarketPage";
import { SkillsPage } from "./routes/skills/SkillsPage";
import { ResumesPage } from "./routes/resumes/ResumesPage";
import { ObservabilityPage } from "./routes/observability/ObservabilityPage";
import { SettingsPage } from "./routes/settings/SettingsPage";
import { initLiveEvents } from "./state/liveEvents";

function formatRetroDate(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const yy = String(d.getFullYear() % 100).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${mm}-${dd}-${yy}  ${hh}:${min}:${ss}`;
}

const PAGE_SUBTITLES: Record<string, string> = {
  "/": "Job Dashboard",
  "/market": "Market Supply",
  "/skills": "Skill Gap Analysis",
  "/observability": "Model Observability & Token Inspector",
  "/resumes": "Resumes & Skill Profiles",
  "/settings": "Settings & Configuration",
};

export function App() {
  const [location] = useLocation();
  const [clock, setClock] = useState(() => formatRetroDate(new Date()));

  useEffect(() => {
    initLiveEvents();

    const timer = setInterval(() => {
      setClock(formatRetroDate(new Date()));
    }, 1000);

    return () => clearInterval(timer);
  }, []);

  return (
    <div class="crt-chassis">
      <div class="crt-screen">
        <div class="crt-scanlines"></div>

        {/* Top System Header Bar */}
        <header class="system-header-bar">
          <div class="system-title">
            <strong>CAREERRADAR</strong> -{" "}
            {PAGE_SUBTITLES[location] ?? "Job Dashboard"}
          </div>
          <div class="system-clock">{clock}</div>
        </header>

        {/* Main Application Layout */}
        <div class="app-container">
          <Sidebar />
          <main class="main-content">
            <ErrorBanner />

            <Switch>
              <Route path="/" component={DashboardPage} />
              <Route path="/market" component={MarketPage} />
              <Route path="/skills" component={SkillsPage} />
              <Route path="/observability" component={ObservabilityPage} />
              <Route path="/resumes" component={ResumesPage} />
              <Route path="/settings" component={SettingsPage} />
              <Route>
                <Redirect to="/" />
              </Route>
            </Switch>
          </main>
        </div>

        {/* Central Modals */}
        <ModalHost />
      </div>
    </div>
  );
}
