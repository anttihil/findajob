import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/preact";
import { ImportJobModal } from "../src/components/ImportJobModal";
import { ModalHost } from "../src/components/ModalHost";
import { activeModal, openImportModal, closeModal } from "../src/state/modal";

describe("ImportJobModal & ModalHost", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    closeModal();
  });

  it("does not render when closed", () => {
    const { container } = render(
      <ImportJobModal isOpen={false} onClose={vi.fn()} onJobImported={vi.fn()} />
    );
    expect(container.querySelector(".retro-modal-overlay")).toBeNull();
  });

  it("renders when open and closes on close button click", () => {
    const onClose = vi.fn();
    const { container, getByLabelText } = render(
      <ImportJobModal isOpen={true} onClose={onClose} onJobImported={vi.fn()} />
    );

    expect(container.querySelector(".retro-modal-overlay")).toBeTruthy();
    expect(container.querySelector(".retro-modal-box")).toBeTruthy();

    const closeBtn = getByLabelText("Close");
    fireEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes when clicking outside (on the overlay)", () => {
    const onClose = vi.fn();
    const { container } = render(
      <ImportJobModal isOpen={true} onClose={onClose} onJobImported={vi.fn()} />
    );

    const overlay = container.querySelector(".retro-modal-overlay")!;
    fireEvent.click(overlay);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close when clicking inside the modal box", () => {
    const onClose = vi.fn();
    const { container } = render(
      <ImportJobModal isOpen={true} onClose={onClose} onJobImported={vi.fn()} />
    );

    const box = container.querySelector(".retro-modal-box")!;
    fireEvent.click(box);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes on Escape key press", () => {
    const onClose = vi.fn();
    const { container } = render(
      <ImportJobModal isOpen={true} onClose={onClose} onJobImported={vi.fn()} />
    );

    const overlay = container.querySelector(".retro-modal-overlay")!;
    fireEvent.keyDown(overlay, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("submits import request and triggers onJobImported", async () => {
    const onJobImported = vi.fn();
    const onClose = vi.fn();

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", job_id: 123 }),
      })
    );

    const { getByPlaceholderText, getByText } = render(
      <ImportJobModal isOpen={true} onClose={onClose} onJobImported={onJobImported} />
    );

    const input = getByPlaceholderText("https://...");
    fireEvent.input(input, { target: { value: "https://jobs.example.com/posting/456" } });

    const submitBtn = getByText("Import & Process");
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(onJobImported).toHaveBeenCalledWith(123);
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("works with central ModalHost and openImportModal", async () => {
    const onJobImported = vi.fn();
    const { container } = render(<ModalHost />);

    expect(container.querySelector(".retro-modal-overlay")).toBeNull();

    openImportModal({ onJobImported });

    await waitFor(() => {
      expect(container.querySelector(".retro-modal-overlay")).toBeTruthy();
    });

    const overlay = container.querySelector(".retro-modal-overlay")!;
    fireEvent.keyDown(overlay, { key: "Escape" });

    await waitFor(() => {
      expect(container.querySelector(".retro-modal-overlay")).toBeNull();
      expect(activeModal.value).toBeNull();
    });
  });
});
