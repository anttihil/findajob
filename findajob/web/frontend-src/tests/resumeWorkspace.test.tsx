import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { CareerMemoPanel } from "../src/routes/resumes/CareerMemoPanel";
import { ResumeWorkspacePanel } from "../src/routes/resumes/ResumeWorkspacePanel";
import type { ResumeWorkspace } from "../src/routes/resumes/workspaceApi";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });

describe("Career memo", () => {
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
