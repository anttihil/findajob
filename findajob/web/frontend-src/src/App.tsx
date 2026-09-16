import { useEffect } from "preact/hooks";
import { Redirect, Route, Switch } from "wouter-preact";
import { Sidebar } from "./components/Sidebar";
import { ErrorBanner } from "./components/ErrorBanner";
import { ModalHost } from "./components/ModalHost";
import { DashboardPage } from "./routes/dashboard/DashboardPage";
import { MarketPage } from "./routes/market/MarketPage";
import { SkillsPage } from "./routes/skills/SkillsPage";
import { ResumesPage } from "./routes/resumes/ResumesPage";
import { ObservabilityPage } from "./routes/observability/ObservabilityPage";
import { initLiveEvents } from "./state/liveEvents";
import { Header } from "./components/Header";

export function App() {
  useEffect(() => {
    initLiveEvents();
  }, []);

  return (
    <div class="crt-chassis">
      <div class="crt-screen">
        <div class="crt-scanlines"></div>

        <Header />

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
              <Route>
                <Redirect to="/" />
              </Route>
            </Switch>
          </main>
        </div>

        <ModalHost />
      </div>
    </div>
  );
}
