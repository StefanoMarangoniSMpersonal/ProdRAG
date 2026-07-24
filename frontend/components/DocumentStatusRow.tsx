"use client";

import { usePollDocument } from "@/hooks/usePollDocument";
import type { DocumentStatusValue } from "@/lib/types";

// One line in the upload list. There are two kinds of row and the split matters for React's
// rules-of-hooks: a row we're actively polling MUST call usePollDocument, but a row that's
// still uploading (no doc id yet) or that failed to upload has nothing to poll. You can't
// call a hook conditionally, so the polling lives in `TrackedDocumentRow` (a component that
// only mounts once there IS an id) and the plain visual row below stays hook-free.

// The visual states a row can be in, flattened from both the client-side upload phase and
// the backend ingestion status into one set the pill knows how to render.
type RowState = "uploading" | "upload-error" | DocumentStatusValue;

const PILL: Record<RowState, { label: string; dot: string; text: string }> = {
  uploading: { label: "uploading", dot: "bg-fog-400 pulse-dot", text: "text-fog-400" },
  pending: { label: "queued", dot: "bg-fog-400", text: "text-fog-400" },
  processing: { label: "processing", dot: "bg-amber-400 pulse-dot", text: "text-amber-400" },
  ready: { label: "ready", dot: "bg-signal-ok", text: "text-signal-ok" },
  failed: { label: "failed", dot: "bg-signal-bad", text: "text-signal-bad" },
  "upload-error": { label: "upload failed", dot: "bg-signal-bad", text: "text-signal-bad" },
};

type RowProps = {
  filename: string;
  state: RowState;
  /** Failure detail — the backend `error` on `failed`, or the upload exception message. */
  error?: string | null;
  /** True while a poll can't reach the backend (still retrying under the hood). */
  unreachable?: boolean;
};

export default function DocumentStatusRow({
  filename,
  state,
  error,
  unreachable,
}: RowProps) {
  const pill = PILL[state];
  const settled = state === "ready" || state === "failed" || state === "upload-error";

  return (
    <li className="rise rounded-lg border border-ink-800 bg-ink-900/60 px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        <span className="min-w-0 flex-1 truncate font-mono text-sm text-fog-200">
          {filename}
        </span>

        <span
          className={`flex shrink-0 items-center gap-2 font-mono text-[0.7rem] uppercase tracking-[0.18em] ${pill.text}`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${pill.dot}`} aria-hidden />
          {pill.label}
        </span>
      </div>

      {/* A transient poll failure — the loop is still retrying, so this is informational. */}
      {unreachable && !settled && (
        <p className="mt-2 font-mono text-[0.7rem] text-fog-500">
          can&rsquo;t reach the backend — retrying…
        </p>
      )}

      {/* The failure reason: backend ingestion error, or the upload call's own error. */}
      {(state === "failed" || state === "upload-error") && error && (
        <p className="mt-2 rounded border border-signal-bad/30 bg-signal-bad/10 px-2.5 py-1.5 text-xs leading-relaxed text-signal-bad">
          {error}
        </p>
      )}
    </li>
  );
}

// The polling variant: mounts only once the upload has returned a doc id. It runs the watch
// loop and feeds the resolved status straight into the presentational row above.
export function TrackedDocumentRow({
  id,
  filename,
  initialStatus,
}: {
  id: string;
  filename: string;
  initialStatus: DocumentStatusValue;
}) {
  const { status, error, unreachable } = usePollDocument(id, initialStatus);
  return (
    <DocumentStatusRow
      filename={filename}
      state={status}
      error={error}
      unreachable={unreachable}
    />
  );
}
