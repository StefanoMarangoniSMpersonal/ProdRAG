"use client";

import { useRef, useState } from "react";

// The drop target + click-to-browse control. It's deliberately dumb: it collects File
// objects (from a drop or the hidden <input>) and hands each to `onFiles`; the page owns
// what happens next (upload + track). Keeping it stateless beyond its own drag-hover means
// the upload/poll logic isn't tangled into drag-and-drop event plumbing.

// Formats Unstructured can partition (matches the backend's parse stage). Advisory only —
// the backend is the real gate; this just sets the file picker's filter and the hint text.
const ACCEPT = ".pdf,.docx,.doc,.pptx,.html,.htm,.md,.txt,.eml";

type Props = {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
};

export default function UploadDropzone({ onFiles, disabled }: Props) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    const files = Array.from(e.dataTransfer.files);
    if (files.length) onFiles(files);
  }

  function handlePick(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    if (files.length) onFiles(files);
    // Reset so picking the SAME file again still fires a change event.
    e.target.value = "";
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => !disabled && inputRef.current?.click()}
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          inputRef.current?.click();
        }
      }}
      className={`group relative flex cursor-pointer flex-col items-center justify-center rounded-2xl border border-dashed px-6 py-14 text-center transition-colors ${
        dragging
          ? "border-amber-400 bg-amber-400/5"
          : "border-ink-700 bg-ink-900/40 hover:border-ink-600"
      } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        multiple
        onChange={handlePick}
        disabled={disabled}
        className="hidden"
        aria-label="Choose files to ingest"
      />

      <svg
        className={`h-8 w-8 transition-colors ${dragging ? "text-amber-400" : "text-fog-500 group-hover:text-fog-400"}`}
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
      >
        <path
          d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>

      <p className="mt-4 text-sm text-fog-200">
        <span className="text-amber-400">Drop a document</span> or click to browse
      </p>
      <p className="mt-1.5 font-mono text-[0.7rem] uppercase tracking-[0.15em] text-fog-500">
        pdf · docx · pptx · html · md · txt · eml
      </p>
    </div>
  );
}
