import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/preact";
import { JobCard } from "../src/components/JobCard";
import { JobDrawer } from "../src/routes/dashboard/JobDrawer";
import { ResumesPage } from "../src/routes/resumes/ResumesPage";
import { FilterQuery } from "../src/lib/filterQuery";
import type { Job, JobContext, GeneratedResumeRecord } from "../src/api/types";

function mockJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 42,
    job_key: "k42",
    title: "Senior Backend Engineer",
    company: "Acme Corp",
    location: "Remote",
    country: "US",
    url: "https://example.com/job/42",
    description: "Job description here",
    source: "linkedin",
    match_score: 95,
    matched_skills: ["Python", "PostgreSQL"],
    resume_match: null,
    status: "unread",
    date_found: "2026-08-25T10:00:00Z",
    date_applied: null,
    access: "remote",
    role_family: "Backend",
    seniority: "Senior",
    is_remote: true,
    city: null,
    region: null,
    date_posted: "2026-08-25T08:00:00Z",
    salary_min: 150000,
    salary_max: 180000,
    salary_currency: "USD",
    salary_annual_usd: 165000,
    fit: true,
    reason_type: "match",
    reason_description: "Great skills match",
    pipeline_state: "scored",
    liveness: "live",
    ...overrides,
  };
}

describe("Bidirectional Job and Resume Links", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  describe("JobCard", () => {
    it("renders resume badge when job has tailored_resume_id", () => {
      const jobWithResume = mockJob({ tailored_resume_id: 101 });
      const query = new FilterQuery("");
      const { getByText } = render(<JobCard job={jobWithResume} query={query} />);
      expect(getByText("[Resume]")).toBeTruthy();
    });

    it("does not render resume badge when job has no tailored_resume_id", () => {
      const jobWithoutResume = mockJob({ tailored_resume_id: null });
      const query = new FilterQuery("");
      const { queryByText } = render(<JobCard job={jobWithoutResume} query={query} />);
      expect(queryByText("[Resume]")).toBeNull();
    });
  });

  describe("JobDrawer", () => {
    it("renders link to Resumes tab when tailored resume exists", () => {
      const resume: GeneratedResumeRecord = {
        id: 101,
        job_id: 42,
        created_at: "2026-08-25T12:00:00Z",
        summary: "Expert Python Architect",
        ats_score: 9,
        ats_verdict: "Strong Match",
      };

      const context: JobContext = {
        job: mockJob({ id: 42 }),
        dossier: null,
        resume,
        next_job_id: null,
      };

      const { getAllByText, container } = render(
        <JobDrawer context={context} onClose={() => {}} onStatusChange={() => {}} />
      );

      const links = container.querySelectorAll<HTMLAnchorElement>('a[href*="/resumes?tab=tailored&job=42"]');
      expect(links.length).toBeGreaterThanOrEqual(1);
      expect(getAllByText(/Open in Resumes/i).length).toBeGreaterThanOrEqual(1);
    });

    it("renders Resumes Library link when no tailored resume exists yet", () => {
      const context: JobContext = {
        job: mockJob({ id: 42 }),
        dossier: null,
        resume: null,
        next_job_id: null,
      };

      const { getByText, container } = render(
        <JobDrawer context={context} onClose={() => {}} onStatusChange={() => {}} />
      );

      expect(getByText("Resumes Library")).toBeTruthy();
      const link = container.querySelector<HTMLAnchorElement>('a[href="/resumes?tab=tailored"]');
      expect(link).toBeTruthy();
    });
  });

  describe("ResumesPage", () => {
    it("renders links to dashboard job in tailored resumes library", async () => {
      const mockResumes: GeneratedResumeRecord[] = [
        {
          id: 101,
          job_id: 42,
          job_title: "Senior Backend Engineer",
          job_company: "Acme Corp",
          created_at: "2026-08-25T12:00:00Z",
          summary: "Expert Python developer tailored for Acme Corp",
          ats_score: 9,
          ats_verdict: "Strong Match",
        },
      ];

      vi.mocked(globalThis.fetch).mockImplementation((url) => {
        const urlStr = String(url);
        if (urlStr === "/api/resume-builder/profile") {
          return Promise.resolve(
            new Response(JSON.stringify({ name: "Jane Doe" }), { status: 200 })
          );
        }
        if (urlStr === "/api/resumes") {
          return Promise.resolve(
            new Response(JSON.stringify(mockResumes), { status: 200 })
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify({}), { status: 200 })
        );
      });

      // Render with tab=tailored
      window.history.pushState({}, "", "/resumes?tab=tailored");

      const { getByText, container } = render(<ResumesPage />);

      await waitFor(() => {
        expect(getByText("View Job")).toBeTruthy();
      });

      // Check for View Job link pointing to /?job=42
      const jobLinks = container.querySelectorAll<HTMLAnchorElement>('a[href="/?job=42"]');
      expect(jobLinks.length).toBeGreaterThanOrEqual(1);

      expect(container.textContent).toContain("Acme Corp");
      expect(getByText("Job #42")).toBeTruthy();
    });
  });
});
