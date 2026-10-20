import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { CareerMemoPanel } from "../src/routes/resumes/CareerMemoPanel";
import { ResumeWorkspacePanel } from "../src/routes/resumes/ResumeWorkspacePanel";
import type { ResumeWorkspace } from "../src/routes/resumes/workspaceApi";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });

describe("Career memo", () => {
  it("compacts unsaved editor text, refines the proposal, and saves only after applying", async () => {
    let proposal = "First compact proposal";
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/chat")) return response({
        reply: "Merged repetition and retained qualifications.",
        updated_markdown: proposal, changes_made: ["Merged repetition"],
      });
      if (url.endsWith("/revisions")) return response([]);
      if (init?.method === "PUT") return response({
        id: 2, markdown: proposal, source: "AI memo copilot", created_at: "now",
      });
      return response({ id: 1, markdown: "Original memo", source: "import", created_at: "before" });
    });
    vi.stubGlobal("fetch", fetcher);
    const view = render(<CareerMemoPanel />);
    const editor = view.getByLabelText("Career memo Markdown") as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toBe("Original memo"));
    fireEvent.input(editor, { target: { value: "Unsaved memo with final correction" } });
    fireEvent.click(view.getByText("AI Copilot"));
    fireEvent.click(view.getByText("Compact memo"));
    await waitFor(() => expect((view.getByLabelText("Proposed career memo") as HTMLTextAreaElement).value).toBe(proposal));
    const first = fetcher.mock.calls.find(([url]) => url.endsWith("/chat"));
    expect(JSON.parse(String(first?.[1]?.body)).current_markdown).toBe("Unsaved memo with final correction");
    expect(editor.value).toBe("Unsaved memo with final correction");
    expect(fetcher.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);

    proposal = "Refined concise memo with correction";
    fireEvent.input(view.getByLabelText("Message memo copilot"), { target: { value: "Make my career direction clearer" } });
    fireEvent.click(view.getByLabelText("Send message"));
    await waitFor(() => expect((view.getByLabelText("Proposed career memo") as HTMLTextAreaElement).value).toBe(proposal));
    const second = fetcher.mock.calls.filter(([url]) => url.endsWith("/chat"))[1];
    const body = JSON.parse(String(second[1]?.body));
    expect(body.current_markdown).toBe("First compact proposal");
    expect(body.messages.at(-1).content).toBe("Make my career direction clearer");

    fireEvent.click(view.getByText("Apply to memo editor"));
    expect(editor.value).toBe(proposal);
    expect(view.queryByRole("dialog")).toBeNull();
    expect(fetcher.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
    fireEvent.click(view.getByText("Save memo revision"));
    await waitFor(() => expect(view.getByRole("status").textContent).toContain("Saved revision #2"));
    const saved = fetcher.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(saved?.[1]?.body))).toEqual({
      markdown: proposal, source: "AI memo copilot", base_revision_id: 1,
    });
  });

  it("discards proposals and leaves the editor intact after copilot failure", async () => {
    let fail = false;
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith("/chat")) return fail
        ? new Response(JSON.stringify({ detail: "Provider unavailable" }), { status: 500 })
        : response({ reply: "Proposal ready", updated_markdown: "Short memo", changes_made: [] });
      if (url.endsWith("/revisions")) return response([]);
      return response({ id: 1, markdown: "Original memo", source: "import", created_at: "before" });
    });
    vi.stubGlobal("fetch", fetcher);
    const view = render(<CareerMemoPanel />);
    const editor = view.getByLabelText("Career memo Markdown") as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toBe("Original memo"));
    fireEvent.click(view.getByText("AI Copilot"));
    fireEvent.click(view.getByText("Compact memo"));
    await waitFor(() => expect(view.getByLabelText("Proposed career memo")).toBeTruthy());
    fireEvent.click(view.getByText("Discard proposal"));
    expect(view.queryByLabelText("Proposed career memo")).toBeNull();
    expect(editor.value).toBe("Original memo");
    fail = true;
    fireEvent.click(view.getByText("Compact memo"));
    await waitFor(() => expect(view.getByText(/Provider unavailable/)).toBeTruthy());
    expect(editor.value).toBe("Original memo");
    expect(view.queryByLabelText("Proposed career memo")).toBeNull();
    expect(fetcher.mock.calls.every(([url]) => !url.endsWith("/profile/chat"))).toBe(true);
  });

  it("imports and saves the complete source with its base revision", async () => {
    const markdown = "Achievement\n".repeat(4000) + "Important final correction";
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/revisions")) return response([]);
      if (init?.method === "PUT") return response({ id: 2, markdown, source: "import", created_at: "now" });
      return response({ id: 1, markdown: "old memo", source: "manual", created_at: "before" });
    });
    vi.stubGlobal("fetch", fetcher);
    const view = render(<CareerMemoPanel />);
    await waitFor(() => expect((view.getByLabelText("Career memo Markdown") as HTMLTextAreaElement).value).toBe("old memo"));
    fireEvent.change(view.getByLabelText("Import career memo"), { target: { files: [{
      name: "achievements.md", text: async () => markdown,
    }] } });
    await waitFor(() => expect((view.getByLabelText("Career memo Markdown") as HTMLTextAreaElement).value).toBe(markdown));
    fireEvent.click(view.getByText("Save memo revision"));
    await waitFor(() => expect(view.getByRole("status").textContent).toContain("Saved revision #2"));
    const request = fetcher.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      markdown, source: "import: achievements.md", base_revision_id: 1,
    });
  });
});

describe("Resume workspace", () => {
  it("persists an interview answer, resumes generation, and compares saved drafts", async () => {
    const question = "How many users adopted this tool?";
    const evidence = { grounded: true, questions: [question], citations: [], revision_advice: "Clarify adoption" };
    let workspace: ResumeWorkspace = {
      job_id: 42, status: "interview", selected_draft_id: 1, memo_revision_id: 2, answers: [],
      drafts: [
        { id: 2, attempt: 2, run_id: "run", memo_revision_id: 2, generated_resume_id: null,
          created_at: "now", text: "New wording", layout: { is_valid: true },
          review: { evidence, recruiter: { score: 7, actionable_feedback: "", missing_signals: [] } } },
        { id: 1, attempt: 1, run_id: "run", memo_revision_id: 2, generated_resume_id: 5,
          created_at: "before", text: "Earlier wording", layout: { is_valid: true },
          review: { evidence, recruiter: { score: 8, actionable_feedback: "", missing_signals: [] } } },
      ],
    };
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/answers") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        workspace = { ...workspace, memo_revision_id: 3, answers: [{
          id: 1, draft_id: body.draft_id, question: body.question, answer: body.answer, created_at: "today",
        }] };
        return response({ workspace, memo_revision_id: 3 });
      }
      if (url.endsWith("/generate")) return response({ status: "ok" });
      return response(workspace);
    });
    vi.stubGlobal("fetch", fetcher);
    const generated = vi.fn();
    const view = render(<ResumeWorkspacePanel jobId={42} onGenerated={generated} />);
    await waitFor(() => expect(view.getByLabelText(question)).toBeTruthy());
    fireEvent.input(view.getByLabelText(question), { target: { value: "Approximately five people" } });
    fireEvent.click(view.getByText("Save answer to memo"));
    await waitFor(() => expect(view.getByText("Saved today")).toBeTruthy());
    const saved = fetcher.mock.calls.find(([url]) => url.endsWith("/answers"));
    expect(JSON.parse(String(saved?.[1]?.body))).toEqual({
      draft_id: 1, question, answer: "Approximately five people", base_revision_id: 2,
    });
    fireEvent.change(view.getByLabelText("Compare draft"), { target: { value: "2" } });
    expect(view.container.querySelectorAll(".workspace-changed").length).toBe(2);
    fireEvent.click(view.getByText("Revise from latest evidence"));
    await waitFor(() => expect(generated).toHaveBeenCalledOnce());
    expect(fetcher.mock.calls.some(([url, init]) => url === "/api/jobs/42/resume/generate" && init?.method === "POST")).toBe(true);
  });
});
