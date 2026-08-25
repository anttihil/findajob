import { useEffect } from "preact/hooks";
import { Redirect, Route, Switch, useLocation } from "wouter-preact";
import { Sidebar } from "./components/Sidebar";
import { ErrorBanner } from "./components/ErrorBanner";
import { DashboardPage } from "./routes/dashboard/DashboardPage";
import { MarketPage } from "./routes/market/MarketPage";
import { SkillsPage } from "./routes/skills/SkillsPage";
import { ResumesPage } from "./routes/resumes/ResumesPage";
import { ObservabilityPage } from "./routes/observability/ObservabilityPage";
import { DigestsPage } from "./routes/digests/DigestsPage";
import { SettingsPage } from "./routes/settings/SettingsPage";
import { initLiveEvents } from "./state/liveEvents";

const PAGE_TITLES: Record<string, string> = {
  "/": "Career Dashboard",
  "/market": "Market Supply",
  "/skills": "Skill Gap Analysis",
  "/observability": "Model Observability & Token Inspection",
  "/resumes": "My Resumes & Skill Profiles",
  "/digests": "Daily Job Digests",
  "/settings": "Radar Configurations",
};

const today = new Date().toLocaleDateString("en-US", {
  month: "long",
  day: "numeric",
  year: "numeric",
});

export function App() {
  const [location] = useLocation();

  useEffect(() => {
    // TODO (Live Feeds): Call initLiveEvents() on mount once implemented in state/liveEvents.ts
    // initLiveEvents();

    initLiveEvents();
  }, []);

  return (
    <div class="app-container">
      <Sidebar />
      <main class="main-content">
        <ErrorBanner />
        <header class="top-bar">
          <h1>{PAGE_TITLES[location] ?? "Career Dashboard"}</h1>
          <div class="current-date">{today}</div>
        </header>

        <Switch>
          <Route path="/" component={DashboardPage} />
          <Route path="/market" component={MarketPage} />
          <Route path="/skills" component={SkillsPage} />
          <Route path="/observability" component={ObservabilityPage} />
          <Route path="/resumes" component={ResumesPage} />
          <Route path="/digests" component={DigestsPage} />
          <Route path="/settings" component={SettingsPage} />
          <Route>
            <Redirect to="/" />
          </Route>
        </Switch>
      </main>
    </div>
  );
}
