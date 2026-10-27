import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { getJSON, putJSON } from "../src/api/client";
import type { JobStatus, Stats } from "../src/api/types";
import { DashboardPage } from "../src/routes/dashboard/DashboardPage";
import { stats } from "../src/state/stats";

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  getJSON: vi.fn(),
  putJSON: vi.fn(),
}));
vi.mock("../src/components/SearchTargetsWidget", () => ({ SearchTargetsWidget: () => null }));
vi.mock("../src/routes/settings/PipelineStatus", () => ({ PipelineStatus: () => null }));
vi.mock("../src/routes/settings/SyncTrigger", () => ({ SyncTrigger: () => null }));
vi.mock("../src/routes/settings/SchedulerControl", () => ({ SchedulerControl: () => null }));
vi.mock("../src/routes/dashboard/JobDrawer", () => ({
  JobDrawer: ({ onStatusChange }: { onStatusChange: (id: number, status: JobStatus) => void }) => (
    <button onClick={() => onStatusChange(1, "saved")}>Save job</button>
  ),
}));

const get = vi.mocked(getJSON);
const counters = (reviewFirst: number): Stats => ({
  total_jobs: 20,
  status_counts: { unread: 18, saved: 2, applied: 0, rejected: 0 },
  review_first: reviewFirst,
  strong_matches: 0,
  skill_coverage: { matched: 0, required: 0, ratio: null },
  country_counts: {},
  pipeline_counts: {},
  liveness_counts: { live: 0, stale: 0, likely_closed: 0, unknown: 20 },
});

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState(null, "", "/?country=FI&fit=true");
  stats.value = null;
  get.mockReset();
  vi.mocked(putJSON).mockReset().mockResolvedValue({ success: true });
  get.mockImplementation(async (url) => {
    if (url === "/api/meta") return { countries: [["FI", "Finland"]], locations: [] };
    if (url.startsWith("/api/stats?")) return counters(3);
    return { jobs: [], total: 0, limit: 50, offset: 0, has_more: false };
  });
});
afterEach(cleanup);

it("loads filtered counters and keeps the card and dropdown filters identical", async () => {
  const view = render(<DashboardPage />);
  const card = view.getByRole("link", { name: /Review first/ });
  await waitFor(() => expect(card.querySelector("h3")?.textContent).toBe("3"));
  const initialStatsUrl = get.mock.calls.find(([url]) => url.startsWith("/api/stats?"))![0];
  const initialParams = new URL(initialStatsUrl, "http://localhost").searchParams;
  expect(initialParams.get("country")).toBe("FI");
  expect(initialParams.get("fit")).toBe("true");
  const cardHref = card.getAttribute("href");
  expect(new URL(cardHref!, "http://localhost").searchParams.get("fit")).toBe("true");

  fireEvent.change(view.getByLabelText("Application priority"), { target: { value: "first" } });
  await waitFor(() =>
    expect(window.location.search).toBe(new URL(cardHref!, "http://localhost").search),
  );
  await waitFor(() => {
    const lastStatsUrl = get.mock.calls
      .filter(([url]) => url.startsWith("/api/stats?"))!
      .at(-1)![0];
    expect(new URL(lastStatsUrl, "http://localhost").searchParams.get("priority")).toBe("first");
  });
});

it("ignores an older counter response after filters change", async () => {
  let resolveOld: (value: Stats) => void = () => {};
  get.mockImplementation(async (url) => {
    if (url === "/api/meta") return { countries: [["FI", "Finland"]], locations: [] };
    if (url.startsWith("/api/stats?")) {
      if (new URL(url, "http://localhost").searchParams.get("priority") === "first")
        return counters(2);
      return new Promise<Stats>((resolve) => {
        resolveOld = resolve;
      });
    }
    return { jobs: [], total: 0, limit: 50, offset: 0, has_more: false };
  });
  const view = render(<DashboardPage />);
  const card = view.getByRole("link", { name: /Review first/ });
  fireEvent.change(view.getByLabelText("Application priority"), { target: { value: "first" } });
  await waitFor(() => expect(card.querySelector("h3")?.textContent).toBe("2"));
  await act(async () => resolveOld(counters(99)));
  expect(card.querySelector("h3")?.textContent).toBe("2");
});

it("refreshes filtered counters and the feed after saving a job", async () => {
  const view = render(<DashboardPage />);
  const card = view.getByRole("link", { name: /Review first/ });
  await waitFor(() => expect(card.querySelector("h3")?.textContent).toBe("3"));
  const statsUrl = get.mock.calls.find(([url]) => url.startsWith("/api/stats?"))![0];
  const jobsUrl = get.mock.calls.find(([url]) => url.startsWith("/api/jobs?"))![0];
  get.mockImplementation(async (url) =>
    url.startsWith("/api/stats?")
      ? counters(2)
      : { jobs: [], total: 0, limit: 50, offset: 0, has_more: false },
  );
  fireEvent.click(view.getByRole("button", { name: "Save job" }));
  await waitFor(() => expect(card.querySelector("h3")?.textContent).toBe("2"));
  expect(putJSON).toHaveBeenCalledWith("/api/jobs/1/status", { status: "saved" });
  expect(get.mock.calls.filter(([url]) => url === statsUrl)).toHaveLength(2);
  expect(get.mock.calls.filter(([url]) => url === jobsUrl)).toHaveLength(2);
});
