"use client";

import { useState } from "react";

import DocumentStatusRow, {
  TrackedDocumentRow,
} from "@/components/DocumentStatusRow";
import UploadDropzone from "@/components/UploadDropzone";
import { ApiError, uploadDocument } from "@/lib/api";
import type { DocumentStatusValue } from "@/lib/types";

// The ingest surface. A file dropped here is uploaded (POST /documents -> 202 + id), then
// its id is handed to a TrackedDocumentRow that polls GET /documents/{id} to ready/failed.
//
// SCOPE BOUNDARY: there is no GET /documents list endpoint, so this page can only show docs
// uploaded in THIS browser session — reload and the list is empty (the docs are still
// ingested; we just can't re-enumerate them). A persistent library is a future backend
// addition, noted in the plan, not built here.

// An upload moves through phases the row renders differently. `key` is a stable client-side
// id for React (the doc id doesn't exist until the upload resolves).
type Upload =
  | { key: string; phase: "uploading"; filename: string }
  | { key: string; phase: "upload-error"; filename: string; error: string }
  | {
      key: string;
      phase: "tracking";
      filename: string;
      id: string;
      initialStatus: DocumentStatusValue;
    };

// Deterministic, monotonic client keys (see the same choice on the chat page: avoids
// crypto.randomUUID, which some test/jsdom environments lack).
let _seq = 0;
const nextKey = () => `u${++_seq}`;

export default function UploadPage() {
  // Newest-first so a fresh upload appears at the top of the list.
  const [uploads, setUploads] = useState<Upload[]>([]);

  function patch(key: string, next: Upload) {
    setUploads((prev) => prev.map((u) => (u.key === key ? next : u)));
  }

  function handleFiles(files: File[]) {
    for (const file of files) {
      const key = nextKey();
      // Optimistic row while the upload POST is in flight.
      setUploads((prev) => [
        { key, phase: "uploading", filename: file.name },
        ...prev,
      ]);

      uploadDocument(file)
        .then((created) => {
          patch(key, {
            key,
            phase: "tracking",
            filename: file.name,
            id: created.id,
            initialStatus: created.status,
          });
        })
        .catch((err) => {
          // A 400 is the backend rejecting the file (e.g. empty); status 0 is a network
          // failure. Either way it's terminal for THIS upload — surface the reason.
          const error =
            err instanceof ApiError
              ? err.status === 0
                ? `Can't reach the backend — is it running on :8000? (${err.message})`
                : err.message
              : "Upload failed.";
          patch(key, { key, phase: "upload-error", filename: file.name, error });
        });
    }
  }

  const busy = uploads.some((u) => u.phase === "uploading");

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <header className="rise">
        <p className="font-mono text-[0.7rem] uppercase tracking-[0.3em] text-amber-400">
          ingest · parse · chunk · embed
        </p>
        <h1 className="mt-3 font-display text-4xl leading-tight text-fog-100">
          Feed the corpus.
        </h1>
        <p className="mt-3 max-w-xl text-sm leading-relaxed text-fog-400">
          A document is parsed, split into chunks, embedded, and indexed — then it&rsquo;s
          answerable on the chat page. Uploads run as a background job; the status below
          updates as each one moves through the pipeline.
        </p>
      </header>

      <div className="rise mt-8" style={{ animationDelay: "0.05s" }}>
        <UploadDropzone onFiles={handleFiles} disabled={busy} />
      </div>

      {uploads.length > 0 && (
        <section className="mt-8">
          <h2 className="mb-3 font-mono text-[0.7rem] uppercase tracking-[0.2em] text-fog-500">
            This session · {uploads.length}
          </h2>
          <ul className="space-y-2">
            {uploads.map((u) =>
              u.phase === "tracking" ? (
                <TrackedDocumentRow
                  key={u.key}
                  id={u.id}
                  filename={u.filename}
                  initialStatus={u.initialStatus}
                />
              ) : (
                <DocumentStatusRow
                  key={u.key}
                  filename={u.filename}
                  state={u.phase}
                  error={u.phase === "upload-error" ? u.error : null}
                />
              ),
            )}
          </ul>
        </section>
      )}

      <p className="mt-8 font-mono text-[0.65rem] leading-relaxed text-fog-500">
        Note: this list is session-local — there&rsquo;s no document-library endpoint yet, so
        a page reload clears it. The documents stay ingested regardless.
      </p>
    </main>
  );
}
