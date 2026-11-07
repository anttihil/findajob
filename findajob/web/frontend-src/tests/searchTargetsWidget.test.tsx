import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor, within } from "@testing-library/preact";
import { SearchTargetsWidget } from "../src/components/SearchTargetsWidget";

const mockTargets = {
  queries: [
    { id: 1, query: "Agentic AI", enabled: 1, sources: ["indeed"], location_ids: ["us_remote"] },
    {
      id: 2,
      query: "GenAI Specialist",
      enabled: 0,
      sources: ["indeed"],
      location_ids: ["helsinki"],
    },
    {
      id: 3,
      query: "Kubernetes Architect",
      enabled: 1,
      sources: ["indeed"],
      location_ids: ["us_remote"],
    },
  ],
  locations: [
    {
      id: "us_remote",
      label: "United States (remote)",
      search_label: "United States",
      country: "US",
      indeed_country: "usa",
      is_remote: 1,
      distance: 50,
      last_used_at: "2026-11-06T12:00:00Z",
    },
    {
      id: "helsinki",
      label: "Helsinki, Finland",
      search_label: "Helsinki, Finland",
      country: "FI",
      indeed_country: "finland",
      is_remote: 0,
      distance: 50,
      last_used_at: null,
    },
  ],
};

const mockCapacity = {
  active_queries: 2,
  locations_in_use: 1,
  search_pairs: 2,
  total_cells: 4,
  runs_per_day: 4,
  daily_capacity_pairs: 40,
  cycle_days: 0.05,
  cycle_hours: 1.2,
  zone: "optimal",
  message: "Every active search pair is checked within a day.",
  optimal_threshold_hours: 24,
  balanced_threshold_hours: 48,
};

describe("SearchTargetsWidget", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, opts?: { method?: string; body?: string }) => {
        if (url === "/api/targets") {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(mockTargets),
          });
        }
        if (url === "/api/targets/capacity") {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(mockCapacity),
          });
        }
        if (url === "/api/config") {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () =>
              Promise.resolve({ scraper: { sources: { indeed: true, linkedin: false } } }),
          });
        }
        if (url.includes("/api/targets/queries") && opts?.method === "PUT") {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ success: true }),
          });
        }
        if (url.includes("/api/targets/locations") && opts?.method === "PUT") {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ success: true }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ success: true }),
        });
      }),
    );
  });

  it("renders collapsed header with live metrics", async () => {
    const { getByText, container } = render(<SearchTargetsWidget initialExpanded={false} />);
    await waitFor(() => {
      expect(getByText(/SEARCH SETTINGS/)).toBeTruthy();
      expect(getByText(/Queries/)).toBeTruthy();
      expect(getByText(/Locations/)).toBeTruthy();
      expect(container.textContent).toContain("~1.2h sweep");
    });
  });

  it("renders search queries when expanded and allows filtering", async () => {
    const { getByText, getByPlaceholderText, queryByText } = render(
      <SearchTargetsWidget initialExpanded={true} />,
    );

    await waitFor(() => {
      expect(getByText("Agentic AI")).toBeTruthy();
      expect(getByText("GenAI Specialist")).toBeTruthy();
      expect(getByText("Kubernetes Architect")).toBeTruthy();
    });

    // Filter by text
    const filterInput = getByPlaceholderText("Filter search queries...");
    fireEvent.input(filterInput, { target: { value: "Kubernetes" } });

    await waitFor(() => {
      expect(getByText("Kubernetes Architect")).toBeTruthy();
      expect(queryByText("Agentic AI")).toBeNull();
    });
  });

  it("edits locations on a query instead of a global locations tab", async () => {
    const view = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() => expect(view.getByText("Agentic AI")).toBeTruthy());
    expect(view.queryByText(/Search Locations/)).toBeNull();
    fireEvent.click(view.getByRole("button", { name: "Edit locations for Agentic AI" }));
    const dialog = view.getByRole("dialog", { name: "Edit query locations" });
    expect(dialog.textContent).toContain("Recently used");
    expect(dialog.textContent).toContain("Helsinki, Finland");
  });

  it("switches to capacity tab and renders metric calculations", async () => {
    const { getByText } = render(<SearchTargetsWidget initialExpanded={true} />);

    await waitFor(() => {
      expect(getByText(/Search Query Terms/)).toBeTruthy();
    });

    const capTabBtn = getByText(/Matrix Capacity & Sweep Health/);
    fireEvent.click(capTabBtn);

    await waitFor(() => {
      expect(getByText("Active Search Queries").previousElementSibling?.textContent).toBe("2");
      expect(
        getByText("Locations in Use", { selector: ".metric-lbl" }).previousElementSibling
          ?.textContent,
      ).toBe("1");
      expect(getByText("Query × Location Combinations").previousElementSibling?.textContent).toBe(
        "2",
      );
      expect(getByText("Scheduled Scrape Cells").previousElementSibling?.textContent).toBe("4");
      expect(getByText(/Estimated sweep: ~1.2 hours/)).toBeTruthy();
    });
  });

  it("manages crawler sources within the search targets panel", async () => {
    const { getByText, getByLabelText } = render(<SearchTargetsWidget initialExpanded={true} />);

    await waitFor(() => expect(getByText("Crawler Sources")).toBeTruthy());
    fireEvent.click(getByText("Crawler Sources"));

    await waitFor(() => {
      expect(getByText("Enabled Crawler Sources")).toBeTruthy();
      expect(getByLabelText(/Indeed/)).toBeTruthy();
      expect(getByLabelText(/LinkedIn/)).toBeTruthy();
      expect(getByLabelText(/Google Jobs/)).toBeTruthy();
    });
  });

  it("allows toggling a query's active state", async () => {
    const onTargetsChanged = vi.fn();
    const { getAllByRole } = render(
      <SearchTargetsWidget initialExpanded={true} onTargetsChanged={onTargetsChanged} />,
    );

    await waitFor(() => {
      const activeBtns = getAllByRole("button", { name: /ACTIVE/i });
      expect(activeBtns.length).toBeGreaterThan(0);
      fireEvent.click(activeBtns[0]);
    });

    await waitFor(() => {
      expect(onTargetsChanged).toHaveBeenCalled();
    });
  });
});

describe("source sweep estimates", () => {
  it("shows independent source estimates and preserves them after matrix refresh", async () => {
    const capacity = {
      ...mockCapacity,
      cycle_hours: 12,
      sources: {
        indeed: { cells: 24, runs_per_day: 8, cycle_hours: 3 },
        linkedin: { cells: 40, runs_per_day: 4, cycle_hours: 12 },
        google: { cells: 2, runs_per_day: 4, cycle_hours: 6 },
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve(
              url === "/api/targets"
                ? mockTargets
                : url === "/api/targets/capacity"
                  ? capacity
                  : url === "/api/targets/sync-cells"
                    ? { success: true, capacity }
                    : { scraper: { sources: { indeed: true, linkedin: true } } },
            ),
        }),
      ),
    );
    const view = render(<SearchTargetsWidget />);
    await waitFor(() => expect(view.getByText("~12h sweep")).toBeTruthy());
    fireEvent.click(view.getByTitle("Click to view capacity analysis"));
    await waitFor(() =>
      expect(view.getByText(/Indeed: 24 cells, 8 runs\/day, ~3h sweep/)).toBeTruthy(),
    );
    expect(view.getByText(/LinkedIn: 40 cells, 4 runs\/day, ~12h sweep/)).toBeTruthy();
    expect(view.getByText(/Google Jobs: 2 cells, 4 runs\/day, ~6h sweep/)).toBeTruthy();
    fireEvent.click(view.getByText(/Force Matrix Re-sync/));
    await waitFor(() => expect(view.getByText("Estimated sweep: ~12 hours")).toBeTruthy());
  });
});

describe("Google Jobs queries", () => {
  it("creates, selects, filters, and reloads explicitly assigned queries", async () => {
    let queries = [
      {
        id: 1,
        query: "Existing engineer",
        enabled: 1,
        sources: ["indeed"],
        location_ids: ["us_remote"],
      },
    ];
    const fetchMock = vi.fn(async (url: string, opts?: { method?: string; body?: string }) => {
      if (url === "/api/targets/queries" && opts?.method === "POST") {
        const payload = JSON.parse(opts.body!);
        queries = [...queries, { id: 2, ...payload }];
      }
      if (url === "/api/targets/queries/1" && opts?.method === "PUT") {
        queries = queries.map((q) => (q.id === 1 ? { ...q, ...JSON.parse(opts.body!) } : q));
      }
      return {
        ok: true,
        status: 200,
        json: async () =>
          url === "/api/targets"
            ? { ...mockTargets, queries }
            : url === "/api/targets/capacity"
              ? mockCapacity
              : { scraper: { sources: { indeed: true, linkedin: true, google: true } } },
      };
    });
    vi.stubGlobal("fetch", fetchMock);
    window.localStorage.clear();
    const view = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() => expect(view.getByText("Existing engineer")).toBeTruthy());
    const existing = view.getByText("Existing engineer").closest("tr")!;
    fireEvent.click(existing.querySelectorAll('input[type="radio"]')[2]);
    await waitFor(() => expect(queries[0].sources).toEqual(["google"]));
    fireEvent.click(view.getByText("Add Query Term"));
    const modal = view.container.querySelector(".retro-modal-box")!;
    fireEvent.click(modal.querySelectorAll('input[name="new-query-source"]')[2]);
    expect(view.getByText(/The configured radius is not enforced/)).toBeTruthy();
    fireEvent.input(modal.querySelector("textarea")!, { target: { value: "Google engineer" } });
    fireEvent.click(modal.querySelector('input[type="checkbox"]')!);
    fireEvent.submit(modal.querySelector("form")!);
    await waitFor(() => expect(view.getByText("Google engineer")).toBeTruthy());
    expect(queries[1].sources).toEqual(["google"]);
    const sourceFilter = view.container.querySelectorAll(".toolbar-filters select")[1];
    fireEvent.change(sourceFilter, { target: { value: "google" } });
    expect(view.getByText("Google engineer")).toBeTruthy();
    view.unmount();
    const reloaded = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() => expect(reloaded.getByText("Google engineer")).toBeTruthy());
    const row = reloaded.getByText("Google engineer").closest("tr")!;
    expect((row.querySelectorAll('input[type="radio"]')[2] as HTMLInputElement).checked).toBe(true);
  });
});

describe("per-query location selection", () => {
  let targets: typeof mockTargets;
  let failSave: boolean;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.localStorage.clear();
    targets = structuredClone(mockTargets);
    failSave = false;
    fetchMock = vi.fn(async (url: string, opts?: { method?: string; body?: string }) => {
      if (url.startsWith("/api/targets/queries") && ["PUT", "POST"].includes(opts?.method || "")) {
        if (failSave) return { ok: false, status: 503, text: async () => "Try again" };
        const payload = JSON.parse(opts!.body!);
        if (opts?.method === "POST") {
          targets.queries.push({ id: targets.queries.length + 1, ...payload });
        } else {
          const id = Number(url.split("/").pop());
          targets.queries = targets.queries.map((q) => (q.id === id ? { ...q, ...payload } : q));
        }
        for (const loc of targets.locations) {
          if (payload.location_ids?.includes(loc.id)) loc.last_used_at = "2026-11-06T14:00:00Z";
        }
      }
      if (url === "/api/targets/locations" && opts?.method === "POST") {
        const payload = JSON.parse(opts.body!);
        targets.locations.push({ ...payload, last_used_at: null });
        return { ok: true, status: 200, json: async () => ({ id: payload.id }) };
      }
      return {
        ok: true,
        status: 200,
        json: async () =>
          url === "/api/targets"
            ? targets
            : url === "/api/targets/capacity"
              ? mockCapacity
              : { scraper: { sources: { indeed: true, linkedin: true } } },
      };
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("requires an explicit choice and applies the same locations to a batch", async () => {
    const view = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() => expect(view.getByText("Agentic AI")).toBeTruthy());
    fireEvent.click(view.getByText("Add Query Term"));
    const modal = within(view.getByRole("dialog", { name: "Add search query" }));
    const save = modal.getByRole("button", { name: "Add Query Term(s)" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(
      (modal.getByRole("checkbox", { name: /United States \(remote\)/ }) as HTMLInputElement)
        .checked,
    ).toBe(false);
    fireEvent.input(modal.getByRole("textbox", { name: "Search Query String(s)" }), {
      target: { value: "product engineer, platform engineer" },
    });
    fireEvent.click(modal.getByRole("checkbox", { name: /United States \(remote\)/ }));
    fireEvent.click(save);
    await waitFor(() => expect(view.queryByRole("dialog")).toBeNull());
    expect(targets.queries.slice(-2).map((q) => q.location_ids)).toEqual([
      ["us_remote"],
      ["us_remote"],
    ]);
    expect(targets.queries.slice(-2).map((q) => q.query)).toEqual([
      "product engineer",
      "platform engineer",
    ]);
  });

  it("cancels drafts, saves changes, and reloads only the query's selected locations", async () => {
    const view = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() => expect(view.getByText("Agentic AI")).toBeTruthy());
    const open = () =>
      fireEvent.click(view.getByRole("button", { name: "Edit locations for Agentic AI" }));
    open();
    let modal = within(view.getByRole("dialog", { name: "Edit query locations" }));
    fireEvent.click(modal.getByRole("checkbox", { name: /Helsinki/ }));
    fireEvent.click(modal.getByRole("button", { name: "Cancel" }));
    expect(targets.queries[0].location_ids).toEqual(["us_remote"]);
    open();
    modal = within(view.getByRole("dialog", { name: "Edit query locations" }));
    expect((modal.getByRole("checkbox", { name: /Helsinki/ }) as HTMLInputElement).checked).toBe(
      false,
    );
    fireEvent.click(modal.getByRole("checkbox", { name: /United States \(remote\)/ }));
    expect(
      (modal.getByRole("button", { name: "Save locations" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(modal.getByRole("checkbox", { name: /Helsinki/ }));
    fireEvent.click(modal.getByRole("button", { name: "Save locations" }));
    await waitFor(() => expect(view.queryByRole("dialog")).toBeNull());
    expect(targets.queries[0].location_ids).toEqual(["helsinki"]);
    expect(targets.queries[2].location_ids).toEqual(["us_remote"]);
    view.unmount();
    const reloaded = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() =>
      expect(reloaded.getByText("Agentic AI").closest("tr")?.textContent).toContain(
        "Helsinki, Finland",
      ),
    );
  });

  it("keeps failed edits open with selections intact for retry", async () => {
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const view = render(<SearchTargetsWidget initialExpanded />);
      await waitFor(() => expect(view.getByText("Agentic AI")).toBeTruthy());
      fireEvent.click(view.getByRole("button", { name: "Edit locations for Agentic AI" }));
      const modal = within(view.getByRole("dialog", { name: "Edit query locations" }));
      fireEvent.click(modal.getByRole("checkbox", { name: /Helsinki/ }));
      failSave = true;
      fireEvent.click(modal.getByRole("button", { name: "Save locations" }));
      await waitFor(() => expect(errorLog).toHaveBeenCalled());
      expect(modal.getByRole("alert").textContent).toContain("Your selections are kept");
      expect((modal.getByRole("checkbox", { name: /Helsinki/ }) as HTMLInputElement).checked).toBe(
        true,
      );
      expect(targets.queries[0].location_ids).toEqual(["us_remote"]);
      failSave = false;
      fireEvent.click(modal.getByRole("button", { name: "Save locations" }));
      await waitFor(() => expect(view.queryByRole("dialog")).toBeNull());
      expect(targets.queries[0].location_ids).toEqual(["us_remote", "helsinki"]);
    } finally {
      errorLog.mockRestore();
    }
  });

  it("creates a custom location inside a query and records recency only when the query saves", async () => {
    const view = render(<SearchTargetsWidget initialExpanded />);
    await waitFor(() => expect(view.getByText("Agentic AI")).toBeTruthy());
    fireEvent.click(view.getByText("Add Query Term"));
    const modal = within(view.getByRole("dialog", { name: "Add search query" }));
    fireEvent.click(modal.getByRole("button", { name: "New custom location" }));
    fireEvent.input(modal.getByLabelText("Location name"), { target: { value: "Portland, OR" } });
    fireEvent.click(modal.getByRole("button", { name: "Use location" }));
    await waitFor(() => expect(modal.getByRole("checkbox", { name: /Portland/ })).toBeTruthy());
    await waitFor(() => expect(modal.queryByRole("status")).toBeNull());
    expect(targets.locations.at(-1)?.last_used_at).toBeNull();
    expect(targets.queries).toHaveLength(3);
    fireEvent.input(modal.getByRole("textbox", { name: "Search Query String(s)" }), {
      target: { value: "Oregon engineer" },
    });
    fireEvent.click(modal.getByRole("button", { name: "Add Query Term(s)" }));
    await waitFor(() => expect(view.queryByRole("dialog")).toBeNull());
    expect(targets.queries.at(-1)?.location_ids).toEqual(["portland_or"]);
    expect(targets.locations.at(-1)?.last_used_at).toBeTruthy();
  });
});
