// The one place the frontend talks to FastAPI. Every network call goes through the
// `request` wrapper below, so error handling (non-2xx -> typed ApiError, JSON parse,
// the base URL) lives in exactly one spot.
//
// SSE SWAP SEAM: transport is non-streaming today (P1 streaming is parked). When it
// unparks, only `ask()` changes here — it becomes an EventSource/ReadableStream reader
// yielding tokens — and every caller keeps importing the same `ask` name. Nothing in
// the components needs to know which transport is live. That localization is the whole
// reason the fetch logic is centralized here rather than inlined in the chat page.

import type {
  AskConfig,
  AskControls,
  AskResponse,
  DocumentCreated,
  DocumentStatus,
  HealthDb,
} from "./types";

// NEXT_PUBLIC_ prefix is what exposes an env var to the browser bundle. Default matches
// the local stack (`.\dev.ps1` runs FastAPI on :8000).
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * A failed API call, carrying the HTTP status so callers can branch on it — the chat
 * page shows a 400 (blank/too-long query) inline as user feedback rather than as a
 * generic failure. `detail` is FastAPI's `{detail: "..."}` message when present.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Core fetch wrapper: resolves the URL, parses JSON, and turns any non-2xx into an
 * `ApiError` carrying the status + the backend's `detail`. A network failure (fetch
 * rejects) surfaces as an `ApiError` with status 0 so callers have one error type.
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, init);
  } catch (err) {
    // fetch only rejects on network-level failure (server down, CORS, offline).
    const message = err instanceof Error ? err.message : "Network request failed";
    throw new ApiError(0, message);
  }

  if (!res.ok) {
    // FastAPI errors are `{detail: string}`; fall back to the status text otherwise.
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body — keep the status text.
    }
    throw new ApiError(res.status, detail);
  }

  return res.json() as Promise<T>;
}

/**
 * POST /ask — send a question, await the full grounded answer (non-streaming).
 *
 * `controls` are the per-request read-path overrides from the chat's control panel
 * (reranker/cache on-off, floor value). They're spread into the body, so omitting them
 * sends `{ query }` unchanged — the pre-controls contract — and passing them adds the
 * knobs the backend reads to flip stages for this one query. This stays the single SSE
 * swap seam: when streaming unparks, only this function changes.
 */
export function ask(
  query: string,
  controls?: AskControls,
): Promise<AskResponse> {
  return request<AskResponse>("/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, ...controls }),
  });
}

/** GET /ask/config — the server's current read-path defaults, to seed the control panel. */
export function getAskConfig(): Promise<AskConfig> {
  return request<AskConfig>("/ask/config");
}

/**
 * POST /documents — multipart upload; the field name MUST be `file` (matches
 * `UploadFile = File(...)` in documents.py). Returns 202 + the new doc id to poll.
 */
export function uploadDocument(file: File): Promise<DocumentCreated> {
  const form = new FormData();
  form.append("file", file);
  return request<DocumentCreated>("/documents", {
    method: "POST",
    body: form, // no Content-Type header — the browser sets the multipart boundary.
  });
}

/** GET /documents/{id} — the poll: current ingestion status (+ error on failure). */
export function getDocumentStatus(id: string): Promise<DocumentStatus> {
  return request<DocumentStatus>(`/documents/${id}`);
}

/** GET /health/db — backend liveness + Postgres/pgvector probe (the header badge). */
export function getHealth(): Promise<HealthDb> {
  return request<HealthDb>("/health/db");
}
