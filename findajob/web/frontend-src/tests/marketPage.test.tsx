import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { MarketPage } from "../src/routes/market/MarketPage";
import { ProgressionChart } from "../src/routes/market/ProgressionChart";
import type { MarketProgressionResponse, QueryYieldTuple } from "../src/api/types";
import { getJSON } from "../src/api/client";

vi.mock("../src/api/client", () => ({ getJSON: vi.fn() }));
const get = vi.mocked(getJSON);
const tuple = (id: number, query = "Engineer", source = "indeed"): QueryYieldTuple => ({
  cell_id: id,
  query,
  source,
  location_id: "remote",
  enabled: true,
  search_query_id: 1,
  total_postings: 100,
  unique_postings: 100,
  scored_postings: 20,
  priority_postings: 1,
  first_priority: 1,
  consider_priority: 0,
  low_priority: 19,
  priority_rate: 0.05,
  priority_rate_pct: 5,
  yield_rate: 0.01,
  yield_rate_pct: 1,
  total_scrapes: 2,
  last_scraped_at: null,
  last_success_at: null,
  yield_category: "moderate_yield",
});
const progression: MarketProgressionResponse = {
  tuple: { id: 1, query: "Engineer", source: "indeed", location_id: "remote" },
  window_days: 30,
  start_date: "2026-08-24",
  end_date: "2026-08-26",
  totals: {
    total_postings: 100,
    scored_postings: 20,
    priority_postings: 1,
    first_priority: 1,
    consider_priority: 0,
    low_priority: 19,
  },
  points: [
    {
      date: "2026-08-24",
      total_postings: 100,
      scored_postings: 20,
      priority_postings: 1,
      first_priority: 1,
      consider_priority: 0,
      low_priority: 19,
    },
    {
      date: "2026-08-25",
      total_postings: 0,
      scored_postings: 0,
      priority_postings: 0,
      first_priority: 0,
      consider_priority: 0,
      low_priority: 0,
    },
    {
      date: "2026-08-26",
      total_postings: 0,
      scored_postings: 0,
      priority_postings: 0,
      first_priority: 0,
      consider_priority: 0,
      low_priority: 0,
    },
  ],
};

beforeEach(() => {
  localStorage.clear();
  get.mockReset();
  get.mockImplementation(async (url) => {
    if (url === "/api/market/locations")
      return { locations: [{ id: "remote", label: "United States" }], queries: [] };
    if (url.includes("/progression")) return progression;
    return { tuples: [tuple(1), tuple(2, "Engineer", "linkedin")] };
  });
});
afterEach(cleanup);

describe("market tuple exploration", () => {
  it("starts with tuples, searches all three dimensions, and opens the exact tuple", async () => {
    // Old view/filter preferences must not hide tuples in the simplified overview.
    localStorage.setItem("findajob.preferences.market-view-mode", JSON.stringify("query"));
    localStorage.setItem("findajob.preferences.market-source-filter", JSON.stringify("linkedin"));
    const view = render(<MarketPage />);
    await view.findByRole("button", { name: "View progression: Engineer, indeed, United States" });
    expect(view.queryByRole("img")).toBeNull();
    fireEvent.input(view.getByLabelText("Find tuples"), {
      target: { value: "engineer indeed united" },
    });
    expect(view.queryByRole("button", { name: /View progression:.*linkedin/ })).toBeNull();
    fireEvent.click(
      view.getByRole("button", { name: "View progression: Engineer, indeed, United States" }),
    );
    await view.findByRole("img", { name: /Daily jobs found/ });
    expect(get).toHaveBeenCalledWith("/api/market/tuples/1/progression?window_days=30");
    expect(view.getByText(/earlier days can change/)).toBeTruthy();
    expect(view.getByText("Daily counts")).toBeTruthy();
    fireEvent.change(view.getByLabelText("Time window"), { target: { value: "7" } });
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/api/market/tuples/1/progression?window_days=7"),
    );
    fireEvent.click(view.getByRole("button", { name: "← All search tuples" }));
    await view.findByRole("button", { name: /View progression: Engineer, indeed/ });
    expect((view.getByLabelText("Find tuples") as HTMLInputElement).value).toBe(
      "engineer indeed united",
    );
    expect((view.getByLabelText("Time window") as HTMLSelectElement).value).toBe("7");
  });

  it("paginates tuples and resets the page when searching", async () => {
    get.mockImplementation(async (url) =>
      url.includes("locations")
        ? { locations: [] }
        : {
            tuples: Array.from({ length: 27 }, (_, i) =>
              tuple(i, `Engineer ${String(i).padStart(2, "0")}`),
            ),
          },
    );
    const view = render(<MarketPage />);
    await view.findByRole("button", { name: /View progression: Engineer 00/ });
    expect(view.getAllByRole("button", { name: /View progression/ })).toHaveLength(25);
    fireEvent.click(view.getByRole("button", { name: "Next" }));
    expect(view.getAllByRole("button", { name: /View progression/ })).toHaveLength(2);
    fireEvent.input(view.getByLabelText("Find tuples"), { target: { value: "Engineer 00" } });
    expect(view.getByRole("button", { name: /View progression: Engineer 00/ })).toBeTruthy();
    expect(view.queryByRole("navigation", { name: "Search tuple pages" })).toBeNull();
  });

  it("shows empty windows and distinguishes load failures, with retry", async () => {
    get.mockImplementation(async (url) => {
      if (url.includes("locations")) return { locations: [] };
      if (url.includes("progression")) throw new Error("offline");
      return { tuples: [tuple(1)] };
    });
    const view = render(<MarketPage />);
    fireEvent.click(await view.findByRole("button", { name: /View progression/ }));
    expect((await view.findByRole("alert")).textContent).toContain(
      "Could not load time progression",
    );
    get.mockResolvedValue({
      ...progression,
      totals: {
        total_postings: 0,
        scored_postings: 0,
        priority_postings: 0,
        first_priority: 0,
        consider_priority: 0,
        low_priority: 0,
      },
    });
    fireEvent.click(view.getByRole("button", { name: "Retry" }));
    await view.findByText("No jobs found for this tuple in this time window.");
    expect(view.queryByRole("img")).toBeNull();
  });

  it("ignores stale requests when changing the time window", async () => {
    let resolveOld: (value: unknown) => void = () => {};
    get.mockImplementation((url) => {
      if (url.includes("locations")) return Promise.resolve({ locations: [] });
      if (url.endsWith("window_days=30"))
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      return Promise.resolve({ tuples: [tuple(2, "New window")] });
    });
    const view = render(<MarketPage />);
    fireEvent.change(view.getByLabelText("Time window"), { target: { value: "7" } });
    await view.findByRole("button", { name: /View progression: New window/ });
    resolveOld({ tuples: [tuple(1, "Old window")] });
    await waitFor(() =>
      expect(view.queryByRole("button", { name: /View progression: Old window/ })).toBeNull(),
    );
  });
});

it("uses the same zero-based scale for jobs and fits without a minimum visual count", () => {
  const view = render(<ProgressionChart points={progression.points} />);
  const jobs = view.container
    .querySelector('[data-series="total_postings"]')!
    .getAttribute("points")!;
  const fits = view.container
    .querySelector('[data-series="priority_postings"]')!
    .getAttribute("points")!;
  const coords = (value: string) => value.split(" ").map((pair) => pair.split(",").map(Number));
  const jobPoints = coords(jobs);
  const fitPoints = coords(fits);
  const baseline = jobPoints[1][1];
  expect(baseline).toBe(fitPoints[1][1]);
  expect((baseline - fitPoints[0][1]) / (baseline - jobPoints[0][1])).toBeCloseTo(1 / 100, 10);
  expect(jobPoints[1][0] - jobPoints[0][0]).toBe(jobPoints[2][0] - jobPoints[1][0]);
});

it("shows priority counts and rate and orders tuples by First + Consider", async () => {
  get.mockImplementation(async (url) =>
    url.includes("locations")
      ? { locations: [] }
      : {
          tuples: [
            tuple(1, "Low yield"),
            {
              ...tuple(2, "High yield"),
              first_priority: 2,
              consider_priority: 3,
              low_priority: 15,
              priority_postings: 5,
              priority_rate_pct: 25,
            },
          ],
        },
  );
  const view = render(<MarketPage />);
  await view.findByRole("button", { name: /View progression: High yield/ });
  expect(view.getAllByRole("button", { name: /View progression/ })[0].textContent).toContain(
    "High yield",
  );
  const row = view.getAllByRole("button", { name: /View progression/ })[0].closest("tr")!;
  expect(row.querySelector('[data-label="First"]')?.textContent).toBe("2");
  expect(row.querySelector('[data-label="Consider"]')?.textContent).toBe("3");
  expect(row.querySelector('[data-label="Low"]')?.textContent).toBe("15");
  expect(row.querySelector('[data-label="Priority rate"]')?.textContent).toBe("25%");
  expect(view.queryByText("Strong fits")).toBeNull();
  expect(view.getByText(/Historical binary judgments are excluded/)).toBeTruthy();
});
