export async function workspaceRequest<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method,
    ...(body === undefined ? {} : {
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
  return data as T;
}

export interface CareerMemo {
  id: number | null;
  markdown: string;
  source: string;
  created_at: string | null;
}

export interface ResumeDraft {
  id: number;
  attempt: number;
  run_id: string;
  memo_revision_id: number | null;
  generated_resume_id: number | null;
  created_at: string;
  text: string;
  layout: { is_valid?: boolean; violations?: string[]; pdf_verified?: boolean; page_count?: number };
  review: {
    evidence: {
      grounded: boolean;
      unsupported_claims?: string[];
      conflicts?: string[];
      qualification_gaps?: string[];
      revision_advice: string;
      questions: string[];
      citations: { claim: string; sources: string[] }[];
    };
    recruiter: { score: number; actionable_feedback: string; missing_signals: string[] } | null;
  };
}

export interface ResumeWorkspace {
  job_id: number;
  status: string;
  selected_draft_id: number | null;
  memo_revision_id: number | null;
  error?: string;
  drafts: ResumeDraft[];
  answers: { id: number; draft_id: number; question: string; answer: string; created_at: string }[];
}
