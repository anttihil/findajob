import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/preact";
import { ResumeDropzone } from "../src/routes/resumes/ResumeDropzone";

describe("ResumeDropzone", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders upload instructions by default", () => {
    const onUploadSuccess = vi.fn();
    const { getByText } = render(<ResumeDropzone onUploadSuccess={onUploadSuccess} />);

    expect(
      getByText("Drop updated resume here (.PDF, .TXT, .MD)")
    ).toBeTruthy();
  });

  it("handles drag over and drag leave states", () => {
    const onUploadSuccess = vi.fn();
    const { container } = render(<ResumeDropzone onUploadSuccess={onUploadSuccess} />);
    const dropzone = container.querySelector(".resume-dropzone")!;

    expect(dropzone.classList.contains("dragging")).toBe(false);

    fireEvent.dragEnter(dropzone);
    expect(dropzone.classList.contains("dragging")).toBe(true);

    fireEvent.dragLeave(dropzone);
    expect(dropzone.classList.contains("dragging")).toBe(false);
  });

  it("uploads dropped file and invokes onUploadSuccess", async () => {
    const onUploadSuccess = vi.fn();
    const mockProfile = {
      name: "Jane Doe",
      email: "jane@example.com",
      phone: "",
      location: "",
      github: "",
      linkedin: "",
      website: "",
      eligibility: {
        citizenship: ["US"],
        locations: ["Remote"],
        willing_to_relocate: false,
        comp_floor_usd: null,
      },
      seniority: "Staff",
      years_experience: 8,
      executive_summary: "Experienced staff engineer",
      model_guidance: "",
      dealbreakers: [],
      experience: [],
      projects: [],
      skills: [],
      education: [],
    };

    const mockResponse = {
      status: "ok",
      filename: "resume.pdf",
      text_snippet: "Sample resume snippet",
      raw_text: "Full raw text",
      profile: mockProfile,
    };

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => mockResponse,
      })
    );

    const { container, getByText } = render(
      <ResumeDropzone onUploadSuccess={onUploadSuccess} />
    );
    const dropzone = container.querySelector(".resume-dropzone")!;

    const file = new File(["dummy resume content"], "resume.pdf", {
      type: "application/pdf",
    });

    fireEvent.drop(dropzone, {
      dataTransfer: {
        files: [file],
      },
    });

    await waitFor(() => {
      expect(getByText("Successfully extracted: resume.pdf")).toBeTruthy();
    });

    expect(onUploadSuccess).toHaveBeenCalledWith(mockResponse);
  });

  it("displays server error message when upload fails", async () => {
    const onUploadSuccess = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        json: async () => ({ detail: "Uploaded file contained no readable text." }),
      })
    );

    const { container, getByText } = render(
      <ResumeDropzone onUploadSuccess={onUploadSuccess} />
    );
    const input = container.querySelector("input[type='file']")!;

    const file = new File([""], "empty.txt", { type: "text/plain" });
    fireEvent.change(input, {
      target: { files: [file] },
    });

    await waitFor(() => {
      expect(
        getByText("Uploaded file contained no readable text.")
      ).toBeTruthy();
    });

    expect(onUploadSuccess).not.toHaveBeenCalled();
  });
});
