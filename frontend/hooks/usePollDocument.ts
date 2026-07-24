"use client";

import { useEffect, useState } from "react";

import { getDocumentStatus } from "@/lib/api";
import type { DocumentStatusValue } from "@/lib/types";

// Ingestion is job-shaped: POST /documents returns 202 + a doc id immediately, then the
// work happens on a worker and the client watches GET /documents/{id} until it settles.
// This hook is that watch loop for ONE document. It polls on a timer, stops the moment the
// status reaches a terminal value, and cleans up on unmount so a row that scrolls away (or
// a page that navigates) doesn't keep hitting the backend.
//
// Why setTimeout recursion instead of setInterval: each tick awaits a network round-trip,
// and setInterval would fire again whether or not the previous request came back — under a
// slow backend that stacks overlapping in-flight polls. Chaining the next timer only after
// the current tick resolves gives a clean "wait ~intervalMs BETWEEN polls" with no overlap.

const TERMINAL: readonly DocumentStatusValue[] = ["ready", "failed"];
const DEFAULT_INTERVAL_MS = 1500;

export type PollResult = {
  /** Latest ingestion status from the backend (seeded with the 202 value). */
  status: DocumentStatusValue;
  /** Populated by the backend only when `status === "failed"`. */
  error: string | null;
  /** True when the last poll couldn't reach the backend — the loop keeps retrying. */
  unreachable: boolean;
};

export function usePollDocument(
  id: string,
  initialStatus: DocumentStatusValue,
  intervalMs: number = DEFAULT_INTERVAL_MS,
): PollResult {
  const [status, setStatus] = useState<DocumentStatusValue>(initialStatus);
  const [error, setError] = useState<string | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    // If the doc arrived already-terminal, there is nothing to watch.
    if (TERMINAL.includes(initialStatus)) return;

    let alive = true;
    let timer: ReturnType<typeof setTimeout>;

    async function tick() {
      try {
        const doc = await getDocumentStatus(id);
        if (!alive) return;
        setStatus(doc.status);
        setError(doc.error);
        setUnreachable(false);
        // Reached ready/failed — stop; do NOT schedule another poll.
        if (TERMINAL.includes(doc.status)) return;
      } catch {
        // A transient failure (backend restarting, blip) shouldn't kill the watch;
        // flag it and keep polling so the row recovers on its own.
        if (!alive) return;
        setUnreachable(true);
      }
      timer = setTimeout(tick, intervalMs);
    }

    // First poll after one interval — the 202 status is fresh enough to show at once.
    timer = setTimeout(tick, intervalMs);

    return () => {
      alive = false;
      clearTimeout(timer);
    };
    // `id`/`initialStatus`/`intervalMs` fully key this loop; `status` is intentionally NOT
    // a dep — the recursion owns its own stopping, and adding it would restart the timer on
    // every status change.
  }, [id, initialStatus, intervalMs]);

  return { status, error, unreachable };
}
