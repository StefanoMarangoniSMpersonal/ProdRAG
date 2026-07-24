# ADR 0007 — `/ask` returns source passages, not just citation ids

- **Status:** Accepted (2026-07-24)
- **Milestone:** Phase 2.5, P6 (chat + upload frontend) — backend rider enabling the UI
- **Deciders:** the architect (human); implemented by Claude Code

## Context

Through Q10/P2/P3 the `POST /ask` response carried the answer plus three id-only views of the
funnel: `citations: list[int]` (the chunk ids the answer drew on), `retrieved_chunk_ids` (the
pre-rerank candidate pool), and `reranked_chunk_ids` (what the model was shown). That is exactly
what a backend/eval caller needs — ids join back to `chunks` and `query_logs`.

P6 builds the first human-facing surface (the chat page). A reader looking at an answer with a
`[13] [11]` badge and **no passage behind it** learns nothing: they can't verify the answer against
its evidence, which is the entire point of a cited RAG system. The UI needs the *text* of each
shown chunk.

The key observation is that this text is **already in memory** at response time: `ask.py` holds
`result.chunks` (each a `ScoredChunk` with `.chunk.id`, `.chunk.content`, `.score`) — it already
sums their lengths for `context_chars`. Returning them is nearly free; the only question is the
shape and scope of the contract change.

## Decision

1. **Add a response-only `sources: list[Source]` to `AskResponse`**, where
   `Source = {id: int, content: str, score: float}`.
2. **`sources` = the SHOWN chunks (post-rerank `result.chunks`), not just the cited ones** — a
   *superset* of `citations`, in shown order. The UI renders every passage the model saw and
   highlights the subset the answer cited.
3. **Response-only — the frozen per-query audit contract is untouched.** The `ask.query` stdout
   line and the `query_logs` row are not changed; there is **no migration**. Sources are
   reconstructable from `reranked_chunk_ids` (already logged) joined back to `chunks`, so persisting
   the text again would be redundant.
4. **`filename` is deferred.** A human-friendly source label needs the `Document` join, which
   `retrieve()` does not load today. Out of scope for v1; a noted follow-up (a `retrieve()` change,
   not an `ask.py` one).
5. **The cache replays sources too.** `cache.CachedAnswer` gains `sources: list[dict]` (defaulted to
   `[]`) so a P3 cache hit reproduces the exact response a fresh miss would, sources included.

## Rationale

- **Shown, not just cited.** Showing only cited passages would hide the chunks the model saw but
  didn't cite — the exact context a user needs to judge whether a citation is *missing*. The wider
  set is the honest evidence trail; `citations` is the highlight over it.
- **Response-only, no migration.** Sources are derived data (chunk text is already durable in
  `chunks`; which chunks were shown is already in `query_logs.final_chunk_ids`). Adding a column
  would duplicate content and enlarge every audit row for no query the log can't already answer.
- **Plain dicts in the cache, `Source` models in the response.** `CachedAnswer` is a frozen
  dataclass serialized via `asdict()` + `json.dumps()`; storing `Source` (a Pydantic model) there
  would need a custom encoder. Plain `{id, content, score}` dicts round-trip natively, and the
  endpoint rebuilds `Source(**d)` on the way out. The default `[]` makes the field purely additive:
  a cache entry written before this field existed reconstructs via `CachedAnswer(**payload)` and
  degrades to id-only — never errors.

## Alternatives rejected

- **Ship bare ids, resolve text in the frontend via a new `GET /chunks/{id}`.** Adds a new endpoint
  + N round-trips per answer to fetch data the backend already had in memory. Rejected — pure cost,
  no benefit.
- **Persist `sources` into `query_logs` (a migration).** Duplicates chunk content into every audit
  row; the mapping is already recoverable from `final_chunk_ids`. Rejected — a schema change for
  derived data.
- **`sources` = only the cited chunks.** Smaller payload, but hides the shown-but-uncited context
  and forbids the UI from surfacing a likely-missing citation. Rejected.
- **Include `filename` now.** Wants a `Document` join not currently loaded by `retrieve()`; would
  reshape the retrieval layer for a label. Deferred, not blocking the UI (ids suffice for v1).

## Consequences

- **`app/api/ask.py`:** new `Source(BaseModel)`; `AskResponse` gains `sources: list[Source]`. Built
  once on the miss path as plain dicts (`source_dicts`) reused for both the response and the
  `cache_set` payload; rebuilt into `Source` models on both the miss return and the cache-hit return.
- **`app/cache.py`:** `CachedAnswer` gains `sources: list[dict] = field(default_factory=list)` —
  additive, JSON-native, fail-safe on pre-P6 entries.
- **Immutable tests untouched.** `test_ask.py` asserts key-by-key (not exact-dict), so the new key
  is invisible to it; `test_cache.py` / `test_ask_cache.py` construct `CachedAnswer` without
  `sources` and rely on the default. All pass unedited. New coverage: `test_ask_sources.py` (3) —
  a source per shown chunk, `sources: []` on refusal, and cache-hit replay. Suite **182+5 → 185+5**,
  0-skip.
- **Follow-up:** source `filename` (needs a `Document` join in `retrieve()`); a persistent document
  library endpoint (`GET /documents`) for the upload page — both noted, not built in P6.
