// TypeScript mirrors of the backend's Pydantic response shapes. Kept hand-written
// (not generated) and deliberately narrow: only the fields the UI reads. If the
// backend contract changes, this file is the single place the frontend learns of it.
//
// Sources of truth (backend):
//   - AskRequest / Source / AskResponse -> app/api/ask.py
//   - DocumentCreated / DocumentStatus  -> app/api/documents.py
//   - HealthDb                          -> /health/db

/**
 * POST /ask request body. `query` plus optional per-request read-path overrides that let
 * the chat experiment live (flip the reranker/cache, move the floor) without touching
 * server config. Each is optional; omitted means "use the server default". For
 * `rerank_score_floor`, `null` is a real value (disable the floor) — distinct from
 * omitting the field. Source of truth: `AskRequest` in app/api/ask.py.
 */
export type AskRequest = {
  query: string;
  rerank_enabled?: boolean;
  cache_enabled?: boolean;
  rerank_score_floor?: number | null;
};

/** The three live-tunable knobs, as the control panel holds them (all concrete). */
export type AskControls = {
  rerank_enabled: boolean;
  cache_enabled: boolean;
  rerank_score_floor: number | null;
};

/** GET /ask/config — the server's current read-path defaults the panel initialises from. */
export type AskConfig = AskControls;

/**
 * The read-path settings that actually governed one answer (the `applied` echo). Lets a
 * message show what produced it; `cache_hit` says whether THIS answer came from the cache
 * (on a hit, retrieval was skipped, so the rerank/floor fields are the effective config,
 * not something that ran). Source of truth: `AppliedSettings` in app/api/ask.py.
 */
export type AppliedSettings = {
  rerank_enabled: boolean;
  rerank_score_floor: number | null;
  cache_enabled: boolean;
  cache_hit: boolean;
};

/**
 * One shown passage — the text behind a citation badge. `id` joins back to
 * `citations` and the id lists; `score` is the chunk's fused/rerank score.
 * (`filename` is a deferred backend follow-up — not present in v1.)
 */
export type Source = {
  id: number;
  content: string;
  score: number;
};

/**
 * POST /ask response. `citations` are the chunk ids the answer drew on (a subset
 * of `sources` by id). `retrieved_chunk_ids` is the pre-rerank candidate pool;
 * `reranked_chunk_ids` is what the model was actually shown. `sources` carries the
 * text of those shown chunks so the UI can render each passage and highlight the
 * cited ones.
 */
export type AskResponse = {
  query_id: string;
  answer: string;
  citations: number[];
  retrieved_chunk_ids: number[];
  reranked_chunk_ids: number[];
  sources: Source[];
  timings_ms: Record<string, number>;
  // Optional in the TS type so callers that build a response without it (e.g. older test
  // fixtures) still typecheck; the live backend always populates it. Consumers must guard.
  applied?: AppliedSettings;
};

/** The document lifecycle, as the backend reports it via GET /documents/{id}. */
export type DocumentStatusValue = "pending" | "processing" | "ready" | "failed";

/** POST /documents 202 body — enough to start polling. */
export type DocumentCreated = {
  id: string;
  status: DocumentStatusValue;
};

/** GET /documents/{id} body. `error` is populated only when `status === "failed"`. */
export type DocumentStatus = {
  id: string;
  status: DocumentStatusValue;
  filename: string;
  error: string | null;
};

/** GET /health/db — the backend liveness + Postgres/pgvector probe. */
export type HealthDb = {
  status: string;
  postgres_version: string;
  pgvector_installed: boolean;
  pgvector_version: string | null;
};
