import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/preact";
import { getJSON } from "../src/api/client";
import type { ObservabilityStats, ObservabilityVerdictItem } from "../src/api/types";
import { ObservabilityPage } from "../src/routes/observability/ObservabilityPage";

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  getJSON: vi.fn(),
}));
const get = vi.mocked(getJSON);
const reasons = [
  "Strong platform experience aligns with the role.\nThe distributed systems work makes this worth reviewing first.",
  "Relevant backend experience; the payments domain would require learning.",
  "The role is junior and offers little opportunity to use the candidate’s experience.",
];
const items: ObservabilityVerdictItem[] = (["first", "consider", "low"] as const).map(
  (priority, index) => ({
    id: index + 1,
    job_id: index + 1,
    title: `Role ${index + 1}`,
    company: `Company ${index + 1}`,
    location: "Remote",
    url: `https://example.com/${index + 1}`,
    priority,
    reason_description: reasons[index],
    tokens_in: 5000,
    tokens_cached: 4000,
    tokens_out: 100,
    cost_usd: 0.0006,
    model: "test-model",
    created_at: "2026-11-01T10:00:00Z",
  }),
);
const summary: ObservabilityStats = {
  total_verdicts: 3,
  total_tokens_in: 15000,
  total_tokens_cached: 12000,
  total_tokens_out: 300,
  cache_hit_rate: 0.8,
  avg_tokens_out: 100,
  min_tokens_out: 100,
  max_tokens_out: 100,
  avg_tokens_in: 5000,
  total_cost_usd: 0.0018,
  avg_cost_usd: 0.0006,
  priorities: items.map(({ priority }) => ({ priority, count: 1, percentage: 1 / 3 })),
  models: [
    {
      model: "test-model",
      count: 3,
      total_tokens_out: 300,
      avg_tokens_out: 100,
      total_cost_usd: 0.0018,
    },
  ],
};
let total: number;
let records: ObservabilityVerdictItem[];
function lastParams() {
  const url = get.mock.calls
    .filter(([url]) => url.startsWith("/api/observability/verdicts?"))
    .at(-1)![0];
  return new URL(url, "http://localhost").searchParams;
}
beforeEach(() => {
  localStorage.clear();
  get.mockReset();
  total = 3;
  records = items;
  get.mockImplementation(async (url) => {
    if (url === "/api/observability/stats") return summary;
    if (url === "/api/observability/prompt")
      return {
        prompt_hash: "abc123",
        system_prompt: "Test scoring prompt",
        summary_text: "Test profile",
        rules: "Test rules",
      };
    const params = new URL(url, "http://localhost").searchParams;
    const priority = params.get("priority");
    const matches = records.filter((item) => !priority || item.priority === priority);
    return {
      items: matches,
      total: priority ? matches.length : total,
      limit: Number(params.get("limit")),
      offset: Number(params.get("offset")),
    };
  });
});
afterEach(cleanup);

it("shows all priorities with their full free-text reasons and no legacy categories", async () => {
  const view = render(<ObservabilityPage />);
  await view.findByRole("link", { name: "Role 1" });
  for (const [index, label] of ["Review first", "Worth considering", "Low priority"].entries()) {
    const card = view
      .getByRole("link", { name: `Role ${index + 1}` })
      .closest(".obs-verdict-card") as HTMLElement;
    expect(within(card).getByText(label).className).toBe(
      `obs-badge-verdict priority-${items[index].priority}`,
    );
    expect(card.querySelector(".obs-explanation-text")?.textContent).toBe(reasons[index]);
  }
  expect(
    view.queryByText(/Historical fit|Fit Verdict|Reason Type|Verdict Reason Distribution/),
  ).toBeNull();
  expect(view.container.querySelector(".obs-badge-reason")).toBeNull();
  const diagnostics = view.getByRole("region", { name: "Model diagnostics" });
  const explorer = view.container.querySelector(".obs-explorer-card")!;
  expect(
    explorer.compareDocumentPosition(diagnostics) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  fireEvent.click(view.getByRole("button", { name: "Inspect Prompt" }));
  expect(view.getByText("Test scoring prompt")).toBeTruthy();
});

it("ignores saved fit, category and old sort preferences and defaults to newest", async () => {
  for (const [key, value] of Object.entries({
    "observability-fit-filter": "no_fit",
    "observability-reason-filter": "skills",
    "observability-sort-order": "tokens_out_desc",
  }))
    localStorage.setItem(`findajob.preferences.${key}`, JSON.stringify(value));
  const view = render(<ObservabilityPage />);
  await view.findByRole("link", { name: "Role 1" });
  expect(lastParams().get("sort")).toBe("date_desc");
  expect(lastParams().has("fit")).toBe(false);
  expect(lastParams().has("reason_type")).toBe(false);
  expect(lastParams().has("priority")).toBe(false);
});

it("keeps summary and priority filters aligned and resets pagination", async () => {
  total = 23;
  const view = render(<ObservabilityPage />);
  await view.findByRole("link", { name: "Role 1" });
  fireEvent.click(view.getByRole("button", { name: "Next" }));
  await waitFor(() => expect(lastParams().get("offset")).toBe("20"));
  fireEvent.change(view.getByLabelText("Priority:"), { target: { value: "low" } });
  await waitFor(() => expect(view.queryByRole("link", { name: "Role 1" })).toBeNull());
  expect(view.getByRole("link", { name: "Role 3" })).toBeTruthy();
  expect(lastParams().get("priority")).toBe("low");
  expect(lastParams().get("offset")).toBe("0");
  const first = view.getByRole("button", { name: /Review first/ });
  fireEvent.click(first);
  await view.findByRole("link", { name: "Role 1" });
  expect((view.getByLabelText("Priority:") as HTMLSelectElement).value).toBe("first");
  expect(first.getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(first);
  await view.findByRole("link", { name: "Role 3" });
  expect(lastParams().has("priority")).toBe(false);
});

it("submits and clears reason searches and preserves priority with alternate sorts", async () => {
  total = 23;
  const view = render(<ObservabilityPage />);
  await view.findByRole("link", { name: "Role 1" });
  fireEvent.change(view.getByLabelText("Priority:"), { target: { value: "consider" } });
  await view.findByRole("link", { name: "Role 2" });
  fireEvent.input(view.getByLabelText("Search verdicts"), { target: { value: "payments" } });
  fireEvent.click(view.getByRole("button", { name: "Search", exact: true }));
  await waitFor(() => expect(lastParams().get("q")).toBe("payments"));
  expect(lastParams().get("priority")).toBe("consider");
  fireEvent.change(view.getByLabelText("Sort:"), { target: { value: "cost_desc" } });
  await waitFor(() => expect(lastParams().get("sort")).toBe("cost_desc"));
  fireEvent.click(view.getByRole("button", { name: "Clear search" }));
  await waitFor(() => expect(lastParams().has("q")).toBe(false));
  fireEvent.change(view.getByLabelText("Priority:"), { target: { value: "all" } });
  await view.findByRole("link", { name: "Role 1" });
  fireEvent.click(view.getByRole("button", { name: "Next" }));
  await waitFor(() => expect(lastParams().get("offset")).toBe("20"));
  fireEvent.change(view.getByLabelText("Page size:"), { target: { value: "10" } });
  await waitFor(() => expect(lastParams().get("limit")).toBe("10"));
  expect(lastParams().get("offset")).toBe("0");
});

it("persists the new priority and sort preferences across visits", async () => {
  let view = render(<ObservabilityPage />);
  await view.findByRole("link", { name: "Role 1" });
  fireEvent.change(view.getByLabelText("Priority:"), { target: { value: "consider" } });
  fireEvent.change(view.getByLabelText("Sort:"), { target: { value: "date_asc" } });
  await waitFor(() => expect(lastParams().get("sort")).toBe("date_asc"));
  view.unmount();
  view = render(<ObservabilityPage />);
  await view.findByRole("link", { name: "Role 2" });
  expect(lastParams().get("priority")).toBe("consider");
  expect(lastParams().get("sort")).toBe("date_asc");
});

it("handles missing reasons and empty priority results", async () => {
  records = [{ ...items[0], reason_description: "  " }];
  let view = render(<ObservabilityPage />);
  await view.findByText("No reason recorded");
  view.unmount();
  records = [];
  total = 0;
  view = render(<ObservabilityPage />);
  await view.findByText("No matching verdicts found");
  expect(view.getByText(/Only priority verdicts appear here/)).toBeTruthy();
  expect(view.queryByRole("button", { name: "Next" })).toBeNull();
});
