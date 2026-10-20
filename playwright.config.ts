import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  fullyParallel: false,
  outputDir: "test-results/browser",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:8011",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { browserName: "chromium", viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { ...devices["iPhone 13"], browserName: "chromium" } },
  ],
  webServer: {
    command: "npm run build && .venv/bin/python .agents/skills/browser-verification/scripts/serve_preview.py --mock-ai --port 8011",
    url: "http://127.0.0.1:8011/api/career-memo",
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
