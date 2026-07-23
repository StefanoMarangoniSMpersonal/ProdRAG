# Phase 2.5 — Read-Path Hardening & Frontend (milestone breakdown)

## Context

Phase 2 closed with Q10: the read path runs end to end and is reachable at `POST /ask`
(`app/api/ask.py` → `retrieve()` → `generate()` → grounded, cited answer + per-query log to
stdout *and* `query_logs`). Offline suite 144+5, 0 skip. Everything works, but the read path is
a **single blocking call** with no streaming, no input/output guard beyond the hand-rolled
grounding levers, no cache, no query transformation, and **no user-facing surface** — the only
frontend is a `/health/db` probe.

**Phase 2.5 hardens that read path and finally puts a UI on it.** It is *not* a new capability
tier so much as making the existing pipeline production-shaped and usable: lower perceived
latency (streaming, warm-up, cache), tighten the I/O contract (guardrails), add a retrieval-
quality lever (rewrite/HyDE), make the pipeline an explicit graph (LangGraph), and build the
chat + upload frontend.

**Architect decisions locked for this phase (2026-07-23):**

- **Sequencing = backend-first.** Deepen and measure every backend layer against the existing
  eval harness *before* building UI. Order: P0 warm-up → P1 streaming → P2 guardrails → P3
  cache → P4 rewrite/HyDE → P5 LangGraph → P6 frontend. Rationale: each backend step is
  measurable (eval delta or a live latency number); the frontend lands last, on top of a
  finished, streaming-capable API.
- **Auth is DEFERRED to a later phase (Phase 3).** The frontend (P6) ships on the existing
  hardcoded `DEV_OWNER_ID` (`app.models.DEV_OWNER_ID`). Supabase Auth (JWT/ES256/JWKS + RLS +
  the Supavisor-bypasses-RLS plumbing) is a cross-cutting frontend+backend `owner_id` rewrite
  and gets its own phase — it is NOT smuggled into the frontend module. Every module below
  keeps `owner_id` as the seam it already is.
- **Milestone labels = P0–P6** (Phase 2.5), continuing the Phase 1 `M*` / Phase 2 `Q*` scheme
  with a new letter, so a milestone id is unambiguous about its phase.
- **The per-query audit log is load-bearing and its contract is frozen.** `ask.py` logs the
  complete answer + citations + token `usage` and writes a `QueryLog` row. Streaming (P1),
  guardrails (P2), cache (P3) and rewrite (P4) all touch data this log records — every module
  must keep that contract intact (accumulate/derive the same fields), never weaken it.

## Conventions to mirror (from Phase 1 & 2 — reuse, don't reinvent)

Same as `docs/PHASE2-PLAN.md`: **one stage per module** under `backend/app/` (heavy imports
lazy *inside* the function; `from __future__ import annotations`; milestone-tagged docstrings);
**async signatures with keyword-only knobs** returning frozen dataclass results carrying
`timings_ms`; **module-level names as monkeypatch seams**
(`monkeypatch.setattr(mod, "generate", fake)` — the pattern `ask.py` already relies on); **every
new knob is a typed `Settings` field** in `app/config.py` with an env override (`get_settings()`
stays `@lru_cache`d); **tests test-first + immutable**, run from repo root via
`backend\.venv\Scripts\python.exe -m pytest`, suite stays **0-skip**, live-API tests behind
`@pytest.mark.live`.

**Dependency fragility is a first-class constraint this phase.** `requirements.txt` documents
hard-won pins — `langchain-community<0.4` (ragas), `redis<6.5` (via `celery[redis]`), pydantic
2.14.2. P2 (guardrails), P3 (redis client) and P5 (langgraph) each risk colliding with these;
each milestone must prove `import`-clean against the existing pins before it's "done".

## How each milestone teaches (the second deliverable)

Exactly as Phase 2: every milestone ships **two** things — working test-first code **and** a
teaching note `docs/learning/P<n>-<topic>.md` (~1 page: the concept, the alternative(s) rejected
and why, the gotcha(s) hit). **Genuine design forks additionally get an ADR** in `docs/adr/`
(next serial is `0003`). Inline teaching while building, per CLAUDE.md — the read-path items are
RAG core ("slow down and teach"); the frontend is glue ("move fast").

## Milestones

### P0 — Reranker warm-up on startup (remove the cold-load papercut)
- **Code:** add a FastAPI **lifespan handler** to `app/main.py` (which has *no* startup hook
  today). On startup, **only if `settings.rerank_enabled`**, call `_get_reranker()`
  (`app/retrieve/rerank.py`, the `@lru_cache(maxsize=1)` seam) inside `asyncio.to_thread` so the
  ~90 MB `ms-marco-MiniLM-L-6-v2` model + torch load happens once, off the event loop, before
  the first request — turning the observed **24.8 s cold first query into a warm ~2.3 s**.
  Gate is mandatory: unconditional warm-up would force a torch import even when rerank is off
  (the default), defeating the deliberate laziness that keeps the offline suite torch-free.
- **Teaching note** (`P0-warmup.md`): lazy `lru_cache` model loading vs eager warm-up; why gate
  on the feature flag; `asyncio.to_thread` for CPU-bound work in an async lifespan; the ASGI
  lifespan protocol vs the deprecated `@app.on_event`.
- **Test (`test_warmup.py`):** with `rerank_enabled=False`, lifespan startup does NOT call
  `_get_reranker` (assert the seam untouched — no torch import); with it True, `_get_reranker`
  is invoked exactly once. Drive via httpx `ASGITransport` over the lifespan, `_get_reranker`
  faked.

### P1 — SSE streaming on `POST /ask` (transport change, pipeline untouched)  ← biggest perceived-latency win
- **Code:** a streaming variant in `app/generate/generate.py` using the SDK's
  `client.aio.models.generate_content_stream(...)` (async iterator, parallel to the existing
  non-streaming `generate_content`), and a new streaming branch in `app/api/ask.py` returning
  FastAPI's `StreamingResponse` (already available in `fastapi==0.115.6`, not yet imported) as
  **SSE**. Answer tokens flush as they arrive; the terminal event carries `citations`,
  `usage`/token counts and `timings_ms`. Non-streaming `/ask` stays as-is (a query param or
  `Accept: text/event-stream` selects the transport).
- **Decision to make at this milestone (→ ADR `0003-streaming-structured-output.md`):** the
  current typed-output contract (`response_mime_type="application/json"` +
  `response_schema=GeneratedAnswer`, parsed whole via `model_validate_json`) **fights token
  streaming** — you can't validate JSON until the stream completes. Fork: (a) stream a plain-text
  answer and resolve citations/usage only on the final chunk, or (b) keep structured output and
  stream the accumulating JSON. Pick one; write up why.
- **Teaching note** (`P1-streaming.md`): SSE vs WebSockets vs chunked; why structured JSON and
  token streaming conflict; where `usage_metadata` appears in a stream (terminal chunk); what
  `generate_ms` even means when TTFB ≠ total.
- **Test (`test_ask_stream.py`, `test_generate_stream.py`):** fake the stream iterator; assert
  answer chunks are yielded in order, the final event carries citations + usage, the
  **empty-context refusal path still returns without a billable call**, and **the `QueryLog`
  row + `ask.query` log line are written with the same fields as the non-streaming path** (the
  frozen audit contract). Live test behind `@pytest.mark.live`.

### P2 — Guardrails I/O validation (tighten the contract)
- **Code:** an input guard + output guard around the read path. **Concrete first win (naive-
  first):** validate `GeneratedAnswer.citations` against the **actually-retrieved chunk ids**
  (`RetrievalResult.candidate_chunk_ids` / final ids available in `ask.py`) — today the model
  can cite an id it was never shown and nothing checks it. Input guard formalizes the existing
  blank-query 400 at the top of `ask()`. Whether this is hand-rolled Pydantic validators or the
  **Guardrails-AI library** (CLAUDE.md's locked choice) is the fork below.
- **Decision to make (→ ADR `0004-guardrails-approach.md`):** Guardrails-the-library vs
  hand-rolled validators. The library is the CLAUDE.md-stated stack but **adds a dependency that
  may drag its own langchain/pydantic constraints** into a requirements file already threading
  `ragas`/`langchain-community<0.4`/pydantic-2.14.2 pins — prove `import`-clean or reject it.
  Naive-first bias: hand-rolled citation-id validation is a few lines and testable today; adopt
  the library only if it earns the dep.
- **Teaching note** (`P2-guardrails.md`): the three grounding levers already in `generate.py`
  (system instruction / temp 0 / structured output) as *implicit* guards vs an *explicit*
  validation layer; citation-grounding as a hallucination check; validate-and-repair vs
  validate-and-fail.
- **Test (`test_guardrails.py`):** an answer citing an unretrieved chunk id is rejected/flagged;
  a valid answer passes untouched; blank input still 400s before any billable call.

### P3 — Redis semantic cache (skip retrieve+generate on near-duplicate queries)
- **Code:** a new `app/cache.py` async Redis client (Redis is **broker-only** today — no cache
  client exists) plus `Settings` knobs (`redis_url`, cache TTL, similarity threshold). Cache
  sits in `ask()` *before* retrieval: embed the query, look up the nearest cached query vector,
  and on a hit above threshold return the stored answer — skipping retrieve+generate entirely.
- **Gotchas baked into the plan:** (1) **`redis<6.5`** — Celery caps it; do NOT `pip install`
  a bare redis 8.x or the worker breaks (requirements.txt warns explicitly). (2) The query
  embedding currently happens *inside* `retrieve()`; a semantic cache either duplicates that
  embed or `retrieve()` is refactored to expose the query vector — prefer exposing it (also
  useful for P4). (3) Cache key includes `owner_id` (today `DEV_OWNER_ID`) so it's already
  correct when auth lands. (4) Redis connection pools are **event-loop-bound** — same cross-loop
  discipline the worker fought; build the client on the API's single persistent loop.
- **Decision to make:** exact-match cache vs true semantic (vector-similarity) cache, and where
  cached vectors live (Redis vector search vs a small in-Redis scan). Note it; naive-first =
  start exact-match on a normalized query string, upgrade to semantic once it works.
- **Teaching note** (`P3-semantic-cache.md`): exact vs semantic caching; cache-before-rewrite vs
  after (interacts with P4); TTL + invalidation; why the embed must be shared not duplicated.
- **Test (`test_cache.py`):** miss → pipeline runs and result is stored; hit → pipeline
  functions (`retrieve`/`generate` seams) are **not** called and the stored answer is returned;
  a below-threshold near-match is a miss. Fake Redis (fakeredis or a dict-backed stub).

### P4 — Query rewrite / HyDE (retrieval-quality lever)  ← measured against the eval harness
- **Code:** a rewrite step at the **top of `retrieve()`** (before the embed at `retrieve.py`
  ~L114) that transforms the raw query via an LLM round-trip (the `generate()`/`get_client()`
  seam is already available). HyDE = generate a hypothetical answer document, embed *that* for
  the semantic arm.
- **Gotcha baked in:** the semantic and lexical arms currently receive the **same** `query`
  string; HyDE's hypothetical document must feed the **semantic** arm only — feeding it to
  `search_lexical` would pollute the tsquery. Plan for **two texts**: rewritten/hypothetical for
  semantic, (lightly) rewritten or raw for lexical.
- **Decision to make (→ ADR `0005-query-transformation.md`):** plain rewrite vs HyDE vs
  multi-query; and cache-before-or-after-rewrite (ties to P3). This is a genuine RAG-core fork —
  **eval decides.** Adds an LLM hop to a ~1–3 s path, so latency vs recall is the trade.
- **Teaching note** (`P4-query-rewrite.md`): why raw queries under-retrieve; HyDE's insight
  (embed a hypothetical answer, not the question); asymmetric semantic-vs-lexical inputs; the
  latency cost.
- **Test (`test_rewrite.py`):** fake the rewrite LLM; assert the semantic arm embeds the
  transformed text and the lexical arm gets the intended (non-polluted) text; rewrite failure
  degrades gracefully to the raw query.
- **Still MANUAL (live, with the architect):** re-run `python -m eval.run` (rerank ON, to match
  the baseline) and record the hit@k / MRR delta vs the Q7 baseline
  (`retrieval-20260718T192246_887014Z.json`); accept/reject per the delta.

### P5 — LangGraph orchestration of the read path (make the pipeline an explicit graph)
- **Code:** wrap the existing linear read path as a LangGraph graph. `retrieve()` already reads
  as a hand-rolled graph (embed → `asyncio.gather(_semantic, _lexical)` → RRF → conditional
  rerank); `ask()`'s retrieve→generate is the outer sequence. This is a **refactor of working
  code**, not new behavior — the "why a graph" milestone Phase 2 deliberately deferred here.
- **⚠ Immutable-test risk (flag, don't silently break):** the offline tests pin the exact
  **module-global seam names** in `retrieve.py`/`ask.py` (`monkeypatch.setattr` targets).
  Wrapping stages in graph nodes changes that monkeypatch surface. If a graph node relocates a
  seam a Phase 2 immutable test pins, that is a **spec revision → STOP and ask the architect**;
  do not edit the test to go green.
- **Gotchas baked in:** preserve **per-arm session ownership** (each concurrent DB arm owns its
  own `SessionLocal()` — a subtle, documented correctness point); LangGraph is a **new
  import-time dependency** (prove it doesn't drag the pinned deps and doesn't force torch/genai
  eager-import).
- **Teaching note** (`P5-langgraph.md`): plain-async pipeline vs a graph — what conditional
  edges, state, and checkpointing actually buy (cache-hit short-circuit, rewrite branch); why we
  built functions first and graphed later.
- **Test (`test_read_graph.py`):** the graph produces byte-identical `RetrievalResult` /
  answer to the pre-refactor path on the same seeded input; conditional edges (rerank on/off,
  cache hit) route correctly.

### P6 — Chat + upload frontend (the first user-facing surface)  ← glue, move fast; NO auth
- **Code:** a Next.js App Router UI in `frontend/app/` (today only a `/health/db` probe page +
  bare `next`/`react` deps — **no Supabase, no component/state libs**). Build: a **chat page**
  that POSTs to `/ask` and **renders the SSE stream from P1** (tokens live) with citations; an
  **upload page** that POSTs multipart to `/documents` (202 + doc id) and **polls
  `GET /documents/{id}`** until `ready`. CORS already allows `http://localhost:3000`. Runs on
  `DEV_OWNER_ID` — **no auth this phase** (locked decision).
- **Teaching note** (`P6-frontend.md`): consuming SSE in the browser (EventSource / fetch
  streams); the 202-and-poll upload contract from the client side; why auth is deliberately a
  separate phase.
- **Test:** component/e2e per the frontend's chosen runner (lighter bar than the backend — this
  is glue). At minimum: a mocked-stream chat render and a mocked upload-poll cycle.

## Deferred to Phase 3 (explicitly out of Phase 2.5)

- **Supabase Auth end-to-end** — `@supabase/ssr` cookie sessions + middleware refresh, FastAPI
  JWKS/ES256 verification, `owner_id` plumbed from the JWT through `retrieve`/`generate`/
  `QueryLog`, the RLS policy pattern, and the Supavisor-bypasses-RLS transaction-claims
  mechanism. The single biggest design-vs-code gap (Layer 8) — its own phase.
- **M-enrich** (LLM summaries for table/image chunks) — still gated on eval; Q9 produced the
  evidence *for* it (chunk context loss) but it's an ingestion change, not read-path.
- **LangSmith / Sentry tracing** and per-query cost/latency dashboards (Layer 10 remainder).
- **S3 blob store**, multi-region/HA, the one-time Fargate touch.

## New dependencies (by milestone)

- **P0:** none (uses installed `transformers`/`torch`).
- **P1:** none (`StreamingResponse` is in the installed `fastapi`; `generate_content_stream` is
  in the installed `google-genai`).
- **P2:** possibly `guardrails-ai` — **only if** ADR `0004` accepts it over hand-rolled
  validators AND it's `import`-clean against the pinned deps. Naive path adds nothing.
- **P3:** an async Redis client **within `redis<6.5`** (present transitively via
  `celery[redis]`); possibly `fakeredis` (dev/test only).
- **P4:** none (reuses the Gemini client).
- **P5:** `langgraph` — prove it doesn't collide with `langchain-community<0.4` / pydantic
  2.14.2 and doesn't force eager torch/genai import.
- **P6:** frontend-only npm deps (no Supabase this phase).

## Verification (how we prove each milestone works)

- **Offline suite:** `backend\.venv\Scripts\python.exe -m pytest` from repo root — every
  milestone red-first then green; suite stays **0-skip** (Docker up for the pgvector
  testcontainer). Report counts in `NN+M` form.
- **Live, interactively:** P0/P1 — `.\dev.ps1` then `curl.exe`/EventSource against
  `POST /ask` and confirm cold-start is gone (P0) and tokens stream (P1). P6 — the chat + upload
  pages driven in the browser at `:3000`.
- **Quality, quantitatively:** P4 (and any change that moves what retrieval embeds) re-runs
  `python -m eval.run --golden eval/golden.jsonl` **rerank-ON** and records the delta vs the Q7
  baseline; the teaching note carries the number. **⚠ Do NOT re-ingest / wipe the dev DB** —
  `golden.jsonl` is keyed to the exact chunk-ids in `infra_pgdata`.
- **Audit-contract check (every backend milestone):** after P1/P2/P3/P4, confirm a `POST /ask`
  still emits the `ask.query` log line and writes a complete `query_logs` row (answer, both
  rankings, `context_chars`, token counts, `timings_ms`) — the frozen contract.

## Open decisions to make at their milestone (not now)

- **P1:** stream plain text vs stream structured JSON (structured output fights token streaming)
  → ADR `0003`.
- **P2:** Guardrails-AI library vs hand-rolled validators (dependency-cost fork) → ADR `0004`.
- **P3:** exact-match vs true semantic cache; cache-before-rewrite vs after (ties to P4); where
  cached vectors live.
- **P4:** plain rewrite vs HyDE vs multi-query → ADR `0005`; **eval decides**.
- **P5:** does any graph node relocate a seam a Phase 2 immutable test pins? If so → **STOP and
  ask** (spec revision, architect's call).
- **P6:** frontend test runner + component-library choice (glue-tier decision, low stakes).
