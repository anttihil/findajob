import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/preact";
import { SchedulerControl } from "../src/routes/settings/SchedulerControl";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows the next search separately for each source", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        enabled: true,
        updated_at: null,
        status: {
          next_runs: {
            "search:indeed": "2026-10-28T15:00:00Z",
            "search:linkedin": "2026-10-28T18:00:00Z",
            score: "2026-10-28T12:30:00Z",
          },
        },
      }),
    }),
  );
  const view = render(<SchedulerControl />);
  await waitFor(() => {
    const text = view.getByText(/Next searches/).textContent;
    expect(text).toContain("Indeed:");
    expect(text).toContain("LinkedIn:");
    expect(text).not.toContain("score:");
  });
});
