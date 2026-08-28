import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/preact";
import { SearchTargetsWidget } from "../src/components/SearchTargetsWidget";

const mockTargets = {
  roles: [
    { id: 1, key: "ai_engineer", label: "AI Engineer", enabled: 1 },
    { id: 2, key: "platform_engineer", label: "Platform Engineer", enabled: 1 },
  ],
  queries: [
    { id: 1, role_key: "ai_engineer", query: "Agentic AI", enabled: 1 },
    { id: 2, role_key: "ai_engineer", query: "GenAI Specialist", enabled: 0 },
    { id: 3, role_key: "platform_engineer", query: "Kubernetes Architect", enabled: 1 },
  ],
  locations: [
    {
      id: "us_remote",
      label: "United States (remote)",
      search_label: "United States",
      country: "US",
      indeed_country: "usa",
      is_remote: 1,
      access: "remote",
      weight: 1.0,
      distance: 50,
      enabled: 1,
    },
    {
      id: "helsinki",
      label: "Helsinki, Finland",
      search_label: "Helsinki, Finland",
      country: "FI",
      indeed_country: "finland",
      is_remote: 0,
      access: "relocation",
      weight: 0.85,
      distance: 50,
      enabled: 0,
    },
  ],
};

const mockCapacity = {
  active_queries: 2,
  active_locations: 1,
  search_pairs: 2,
  total_cells: 4,
  runs_per_day: 4,
  daily_capacity_pairs: 40,
  cycle_days: 0.05,
  cycle_hours: 1.2,
  zone: "optimal",
  message: "Optimal freshness",
  optimal_threshold: 20,
  balanced_threshold: 40,
};

describe("SearchTargetsWidget", () => {
  beforeEach(() => {
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
      })
    );
  });

  it("renders collapsed header with live metrics", async () => {
    const { getByText, container } = render(<SearchTargetsWidget initialExpanded={false} />);
    await waitFor(() => {
      expect(getByText(/SEARCH MATRIX & TARGETS/)).toBeTruthy();
      expect(getByText(/Queries/)).toBeTruthy();
      expect(getByText(/Locations/)).toBeTruthy();
      expect(container.querySelector(".matrix-chip-badge")).toBeTruthy();
    });
  });

  it("renders search queries when expanded and allows filtering", async () => {
    const { getByText, getByPlaceholderText, queryByText } = render(
      <SearchTargetsWidget initialExpanded={true} />
    );

    await waitFor(() => {
      expect(getByText("Agentic AI")).toBeTruthy();
      expect(getByText("GenAI Specialist")).toBeTruthy();
      expect(getByText("Kubernetes Architect")).toBeTruthy();
    });

    // Filter by text
    const filterInput = getByPlaceholderText("Filter search queries or role keys...");
    fireEvent.input(filterInput, { target: { value: "Kubernetes" } });

    await waitFor(() => {
      expect(getByText("Kubernetes Architect")).toBeTruthy();
      expect(queryByText("Agentic AI")).toBeNull();
    });
  });

  it("switches to locations tab and shows configured locations and presets", async () => {
    const { getByText, container, queryByText } = render(
      <SearchTargetsWidget initialExpanded={true} />
    );

    await waitFor(() => {
      expect(getByText("Agentic AI")).toBeTruthy();
    });

    // Click Locations Tab
    const locTabBtn = getByText(/Search Locations/);
    fireEvent.click(locTabBtn);

    await waitFor(() => {
      expect(container.querySelector(".locations-table-wrap")).toBeTruthy();
      expect(container.textContent).toContain("United States (remote)");
      expect(container.textContent).toContain("Helsinki, Finland");
      expect(queryByText("Agentic AI")).toBeNull();
    });
  });

  it("switches to capacity tab and renders metric calculations", async () => {
    const { getByText } = render(<SearchTargetsWidget initialExpanded={true} />);

    await waitFor(() => {
      expect(getByText(/Search Query Terms/)).toBeTruthy();
    });

    const capTabBtn = getByText(/Matrix Capacity & Sweep Health/);
    fireEvent.click(capTabBtn);

    await waitFor(() => {
      expect(getByText("Active Search Queries")).toBeTruthy();
      expect(getByText("Active Locations")).toBeTruthy();
      expect(getByText("Search Pairs")).toBeTruthy();
      expect(getByText(/STATUS: OPTIMAL/)).toBeTruthy();
    });
  });

  it("allows toggling a query's active state", async () => {
    const onTargetsChanged = vi.fn();
    const { getAllByRole } = render(
      <SearchTargetsWidget initialExpanded={true} onTargetsChanged={onTargetsChanged} />
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
