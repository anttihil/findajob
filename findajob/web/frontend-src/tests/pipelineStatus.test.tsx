import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/preact";
import { PipelineStatus } from "../src/routes/settings/PipelineStatus";
import { livePipelineStatus } from "../src/state/liveEvents";

afterEach(() => {
  cleanup();
  livePipelineStatus.value = null;
});

describe("pipeline scoring health", () => {
  it("shows a persistent balance failure even with no eligible backlog or recent scores", () => {
    livePipelineStatus.value = {
      scrape: { in_progress: false },
      score: {
        backlog: 0,
        backlog_share: 0,
        recent_verdicts_5min: 0,
        error: {
          message:
            "Scoring is blocked because the AI provider has insufficient balance. Add API credits, then retry scoring.",
          occurred_at: "2026-10-20T20:00:00Z",
        },
      },
    };
    const view = render(<PipelineStatus compact />);
    expect(view.getByText("blocked")).toBeTruthy();
    expect(view.getByRole("alert").textContent).toContain("insufficient balance");
    expect(view.getByRole("alert").textContent).toContain("Add API credits");
  });

  it("shows normal scoring activity after recovery", () => {
    livePipelineStatus.value = {
      scrape: { in_progress: false },
      score: { backlog: 4, backlog_share: 0.2, recent_verdicts_5min: 3, error: null },
    };
    const view = render(<PipelineStatus />);
    expect(view.getByText("scoring")).toBeTruthy();
    expect(view.queryByRole("alert")).toBeNull();
  });
});
