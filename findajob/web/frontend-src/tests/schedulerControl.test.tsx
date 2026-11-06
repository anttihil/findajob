import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, waitFor } from "@testing-library/preact";
import { ErrorBanner } from "../src/components/ErrorBanner";
import { SchedulerControl } from "../src/routes/settings/SchedulerControl";
import { appErrors, dismissAppErrors } from "../src/state/errors";
import { liveSchedulerPreferences } from "../src/state/liveEvents";

afterEach(() => {
  cleanup();
  liveSchedulerPreferences.value = null;
  dismissAppErrors();
  vi.useRealTimers();
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
            "search:google": "2026-10-28T18:00:00Z",
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
    expect(text).toContain("Google Jobs:");
    expect(text).not.toContain("score:");
  });
});

it("updates reactively from liveSchedulerPreferences SSE signal without polling", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);

  liveSchedulerPreferences.value = {
    enabled: true,
    updated_at: "2026-11-04T18:00:00Z",
    status: {
      enabled: true,
      is_running: true,
      active_stage: null,
      next_runs: {
        "search:indeed": "2026-11-04T19:00:00Z",
      },
    },
  };

  const view = render(<SchedulerControl />);
  expect(view.getByText(/Next searches/).textContent).toContain("Indeed:");
  expect(fetchMock).not.toHaveBeenCalled();

  await act(async () => {
    vi.advanceTimersByTime(60_000);
  });
  expect(fetchMock).not.toHaveBeenCalled();

  await act(async () => {
    liveSchedulerPreferences.value = {
      enabled: true,
      updated_at: "2026-11-04T18:00:00Z",
      status: {
        enabled: true,
        is_running: true,
        active_stage: null,
        next_runs: {
          "search:indeed": "2026-11-04T19:00:00Z",
          "search:linkedin": "2026-11-04T21:00:00Z",
        },
      },
    };
  });

  expect(view.getByText(/Next searches/).textContent).toContain("LinkedIn:");
});

it("separates error context and detail in ErrorBanner", () => {
  appErrors.value = [{ what: "Loading automatic update settings", detail: "Failed to fetch" }];
  const view = render(<ErrorBanner />);
  expect(view.container.querySelector(".sync-error-item")?.textContent).toBe(
    "Loading automatic update settings: Failed to fetch",
  );
});
