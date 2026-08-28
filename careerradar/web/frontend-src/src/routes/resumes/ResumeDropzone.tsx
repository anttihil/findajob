import { useState, useRef } from "preact/hooks";
import type { ResumeUploadResponse } from "../../api/types";

interface ResumeDropzoneProps {
  onUploadSuccess: (data: ResumeUploadResponse) => void;
  disabled?: boolean;
}

export function ResumeDropzone({ onUploadSuccess, disabled }: ResumeDropzoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [uploadSuccessMsg, setUploadSuccessMsg] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleDragEnter = (e: DragEvent) => {
    e.preventDefault();
    if (!disabled) setIsDragging(true);
  };

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault();
    if (!disabled) setIsDragging(true);
  };

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleDrop = async (e: DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (disabled || !e.dataTransfer?.files?.length) return;
    const file = e.dataTransfer.files[0];
    await processFile(file);
  };

  const handleFileChange = async (e: Event) => {
    const input = e.target as HTMLInputElement;
    if (input.files && input.files[0]) {
      await processFile(input.files[0]);
    }
  };

  const processFile = async (file: File) => {
    setIsUploading(true);
    setErrorMsg(null);
    setUploadSuccessMsg(null);

    const formData = new FormData();
    formData.append("file", file);

    try {
      const resp = await fetch("/api/profile/upload-resume", {
        method: "POST",
        body: formData,
      });

      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.detail || `Upload failed with status ${resp.status}`);
      }

      const data: ResumeUploadResponse = await resp.json();
      setUploadSuccessMsg(`Successfully extracted: ${file.name}`);
      onUploadSuccess(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMsg(msg);
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  };

  return (
    <div class="resume-dropzone-container">
      <div
        class={`resume-dropzone ${isDragging ? "dragging" : ""} ${isUploading ? "uploading" : ""}`}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={() => !disabled && !isUploading && fileInputRef.current?.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx,.txt,.md"
          style={{ display: "none" }}
          onChange={handleFileChange}
          disabled={disabled || isUploading}
        />

        <div class="dropzone-inner">
          <div class="dropzone-icon">
            <i class={`fa-solid ${isUploading ? "fa-spinner fa-spin" : "fa-cloud-arrow-up"}`}></i>
          </div>
          <div class="dropzone-text">
            <strong>
              {isUploading
                ? "Parsing resume..."
                : "Drop updated resume here (.PDF, .DOCX, .TXT, .MD)"}
            </strong>
            <span class="dropzone-hint">
              {isUploading
                ? "Extracting profile details..."
                : "or click to browse from disk. Automatically extracts and updates your profile."}
            </span>
          </div>
        </div>
      </div>

      {uploadSuccessMsg && (
        <div class="dropzone-status-msg success">
          <i class="fa-solid fa-check-circle"></i> {uploadSuccessMsg}
        </div>
      )}

      {errorMsg && (
        <div class="dropzone-status-msg error">
          <i class="fa-solid fa-triangle-exclamation"></i> {errorMsg}
        </div>
      )}
    </div>
  );
}
