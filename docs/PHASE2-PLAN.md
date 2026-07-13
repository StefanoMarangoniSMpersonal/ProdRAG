# Phase 2 — Query / Retrieval Pipeline (milestone breakdown)

## Context

Phase 1 is complete: a document travels upload → parse → chunk → embed → written
`chunks` (768-dim vectors) on a real Celery worker, proven live. **Nothing reads those
chunks yet** — Layer 5 (query/retrieval) is ~0% code. Phase 2 builds the read side: turn a
user question into the right chunks plus a grounded, cited answer.

This is a **learning-first** phase (per `CLAUDE.md`: I am the architect, you implement +
teach; slow down on the RAG core). So every milestone ships **two** things: working
test-first code **and** a teaching artifact. The naive version lands end-to-end first, then
we improve it with eval evidence.

**Architect decisions locked for this phase (2026-07-11):**
- **Eval is woven, not bolted on.** A lightweight retrieval-only eval (hit@k / MRR) stands up
  right after naive semantic search, so every later retrieval upgrade is measured against a
  baseline. Full RAGAS answer-eval lands after generation. (Realizes the "eval as substrate" rule.)
- **LangGraph is wrapped later.** Each stage is a plain `async` function first (mirroring
  `app/ingest/*`); the pipeline works end-to-end as functions, then LangGraph is a dedicated
  refactor milestone in **Phase 2.5**, not a prerequisite. Teaches "why a graph" by contrast.
- **Reranker provider is decided at its milestone** (Q7), weighing eval evidence. The rerank
  *seam* is designed now; the provider (managed rerank API vs Gemini-as-reranker) is chosen then.
- **Phase 2 scope = a working `POST /ask`**: semantic + lexical + RRF + rerank + grounded
  generation + per-query logging + eval. **Deferred to Phase 2.5:** LangGraph orchestration,
  Guardrails I/O validation, SSE streaming, Redis semantic cache, query rewrite/HyDE, chat UI.

Milestones are labelled **Q1–Q10** (Q = query pipeline) to sit clearly apart from the
ingestion **M0–M8** series; numbering is adjustable.

## Conventions to mirror (from Phase 1 — reuse, don't reinvent)

- **One stage per module** in a new `backend/app/retrieve/` package (+ `backend/app/generate/`),
  exactly like `app/ingest/{parse,chunk,embed,write,orchestrate}.py`. Long module docstring with a
  milestone tag; full per-function docstrings; heavy third-party imports done **lazily inside the
  function**; `from __future__ import annotations` at top.
- **Async signatures with keyword-only knobs**, returning concrete types /
  **frozen dataclass result objects with per-stage `timings_ms`** (like `IngestResult` in
  `app/ingest/orchestrate.py`) — eval-substrate: retrieval must not run silently.
- **Module-level names as monkeypatch seams**: import collaborators
  (`SessionLocal`, `embed_texts`, `as_retrieval_query`, `search_semantic`, …) as module globals so
  tests swap this module's copy (`monkeypatch.setattr(mod, "embed_texts", fake)`), the pattern
  `orchestrate.py` and `documents.py` already use.
- **Query-embedding seam already exists** — reuse verbatim: `embed_texts([as_retrieval_query(q)])`
  then `[0]` (`app/ingest/embed.py`). 768-dim, L2-normalized → **cosine** is the matching metric
  (HNSW `vector_cosine_ops`). Note the query/doc asymmetry is a **text prefix**, not a `task_type`
  field, and each input must be its own `types.Content` (the 2026-07-07 batch fix).
- **Config** → add retrieval knobs to `Settings` in `app/config.py` (typed field + default +
  env-override comment; `@lru_cache get_settings()`).
- **Owner filter** = `DEV_OWNER_ID` (`app/models.py`) on every query — the RLS seam until auth.
- **Tests** — test-first + immutable (`pytest.ini` at repo root, `asyncio_mode=auto`, `live`
  marker deselected by default). Retrieval tests that seed-then-query use the **committing
  `session_factory`** fixture (not the rolled-back `db_session`), TRUNCATE teardown. Endpoint test
  mirrors `tests/test_upload.py` (`httpx.AsyncClient` + `ASGITransport`, DI override of
  `get_session`, faked pipeline seams). Reuse the `_vec(seed)` synthetic-768-vector helper.

## How each milestone "teaches" (the second deliverable)

1. **Inline teaching while building** — per `CLAUDE.md`, explain non-obvious choices and name
   rejected alternatives as I implement (the RAG core is the "slow down and teach" zone).
2. **A short teaching note per milestone** in a new `docs/learning/` folder
   (`docs/learning/Qx-<topic>.md`, ~1 page): the concept, the alternative(s) rejected and why, and
   the gotcha(s) hit. Genuine design forks (reranker provider, generation model) additionally get a
   one-paragraph ADR in `docs/adr/` (the `/docs` "decisions (ADRs)" slot that's currently empty).
3. **The `.claude` scaffolds become live teaching tools as we fill them** — `explain-retrieval`
   (trace a query stage-by-stage), `eval-run` (measure quality), and the `retrieval-debugger`
   agent all currently name interfaces that don't exist yet; each milestone wires the part it adds.

## Milestones

### Q1 — Schema switch-on: HNSW + full-text `tsvector`
- **Code:** new append-only migration `infra/db/migrations/003_retrieval_indexes.sql` (keeps the
  numbered-SQL, no-Alembic convention; `IF NOT EXISTS` guards so it's idempotent and needs
  **no re-ingest**):
  - `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)` (the pre-written line in `002`).
  - `ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;`
    + `CREATE INDEX ... USING gin (tsv)`.
  - Add `tsv` to the `Chunk` model as a read-only computed column
    (`mapped_column(TSVECTOR, Computed("to_tsvector('english', content)", persisted=True))`) **or**
    leave it out of the model and reach it via raw SQL in Q5 — small call, default to mapping it.
- **Teaching note** (`docs/learning/Q1-indexes.md`): ANN vs exact search; HNSW `m` /
  `ef_construction` / `ef_search` (build-vs-recall-vs-latency); why cosine when vectors are already
  L2-normalized (cosine ≡ inner product); `GENERATED … STORED` columns; GIN vs GiST for `tsvector`;
  **why Postgres FTS ranking is "BM25-ish", not true Okapi BM25** (name ParadeDB/`pg_search` as the
  real-BM25 upgrade, deferred).
- **Test (`test_retrieve_schema.py`):** over `session_factory`, seed chunks; assert a nearest-vector
  query returns the expected order, and a `tsv` full-text match hits. (Index *presence* asserted via
  `pg_indexes`; index *usage* is an EXPLAIN spot-check in the teaching note, not a brittle assertion.)

### Q2 — Semantic retrieval (naive vector search)
- **Code:** `app/retrieve/semantic.py` — `async def search_semantic(session, query_embedding, *,
  k, owner_id=DEV_OWNER_ID) -> list[ScoredChunk]`, ordering by `Chunk.embedding.cosine_distance(vec)`
  ascending, `limit k`, owner-filtered. `ScoredChunk` = frozen dataclass (the `Chunk` + `score`).
- **Teaching note** (`Q2-semantic-search.md`): the pgvector `<=>` cosine operator and
  `.cosine_distance()`; distance→score; the per-session `SET hnsw.ef_search` recall knob; the
  owner filter as the future RLS boundary.
- **Test (`test_semantic.py`):** seed committed chunks with known `_vec(seed)` vectors; a query
  vector near one seed returns it first; `k` truncates.

### Q3 — Retrieval orchestrator + query embedding (first end-to-end retrieve)
- **Code:** `app/retrieve/retrieve.py` — `async def retrieve(query: str, *, k) -> RetrievalResult`,
  with module-level seams `SessionLocal`, `embed_texts`, `as_retrieval_query`, `search_semantic`.
  Embeds the query (`embed_texts([as_retrieval_query(query)])[0]`), runs `search_semantic`, returns
  `RetrievalResult` (frozen: query, chunks, `timings_ms`). First code that answers "what does this
  query retrieve?". Wire the **semantic stage of the `explain-retrieval` skill**.
- **Teaching note** (`Q3-query-embedding.md`): query/document asymmetry as a prefix (not
  `task_type`); the single-`Content`-per-input gotcha; the monkeypatch-seam pattern; why the result
  object carries timings (eval substrate).
- **Test (`test_retrieve.py`):** fake `embed_texts` recording its input → assert the query was
  wrapped with `as_retrieval_query`; real pgvector search over seeded chunks returns expected order.

### Q4 — Retrieval eval baseline (golden set + hit@k / MRR)  ← woven eval begins
- **Code:** create the `/eval` package the scaffolds already assume:
  `eval/golden.jsonl` (a handful of questions over the ingested corpus, each with its relevant
  `chunk_id`s), `eval/run.py` (`python -m eval.run --golden eval/golden.jsonl --out eval/results/`),
  retrieval metrics **hit@k** and **MRR**, results saved under `eval/results/` for baseline
  comparison. Wire `eval-run` skill (retrieval mode). Record the **semantic-only baseline**.
- **Teaching note** (`Q4-retrieval-eval.md`): what hit@k and MRR measure and their blind spots; how
  to build a golden set (and how a bad golden set lies to you); the baseline→delta loop that Q5–Q7
  are measured by.
- **Test (`test_eval_metrics.py`):** hit@k / MRR are pure functions — assert on hand-built rankings
  (deterministic, offline). Harness `run` gets a smoke test with a faked retriever so CI stays offline.

### Q5 — Lexical retrieval (Postgres full-text)
- **Code:** `app/retrieve/lexical.py` — `async def search_lexical(session, query_text, *, k) ->
  list[ScoredChunk]` via `websearch_to_tsquery('english', q)` against `chunks.tsv`, ranked by
  `ts_rank_cd`. Re-run Q4 eval, record the delta.
- **Teaching note** (`Q5-full-text.md`): what lexical catches that vectors miss (proper nouns,
  acronyms, code, exact IDs); `tsvector`/`tsquery`; `websearch_to_tsquery` (Google-style user input)
  vs `plainto_`/`to_tsquery`; `ts_rank` vs `ts_rank_cd`; the BM25 caveat again.
- **Test (`test_lexical.py`):** seed chunks including an exact rare token; a query for that token
  returns the right chunk (a case semantic search would rank poorly), ranked and `k`-truncated.

### Q6 — Hybrid fusion (Reciprocal Rank Fusion)
- **Code:** `app/retrieve/fuse.py` — pure `reciprocal_rank_fusion(rankings: list[list[ChunkId]], *,
  k_constant=60) -> list[ChunkId]`. Update `retrieve()` to run semantic + lexical **concurrently**
  (`asyncio.gather`) and fuse. Re-run eval, record delta.
- **Teaching note** (`Q6-rrf.md`): the RRF formula `Σ 1/(k_constant + rank)`; **why RRF over
  score-normalization** (scale-free — semantic distances and `ts_rank` scores aren't comparable);
  the `k=60` origin (Cormack et al.); dedup across lists.
- **Test (`test_fuse.py`):** hand-crafted rankings → known fused order. Pure function, no DB — the
  cleanest red/green teaching unit in the phase.

### Q7 — Cross-encoder rerank  ← provider decision made here
- **Code:** `app/retrieve/rerank.py` — `async def rerank(query, chunks, *, top_n) ->
  list[ScoredChunk]`, module-level seam so it's fake-able. Retrieve-wide (fused ~20–50) →
  rerank-narrow (**5–10**, the locked target). **Decision at this milestone:** provider (managed
  rerank API vs Gemini-as-reranker) → written up as an ADR. Re-run eval — expect the largest quality jump.
- **Teaching note** (`Q7-rerank.md`): bi-encoder vs cross-encoder (cross-encoder reads query+chunk
  *together* → more accurate but O(n), so only on the shortlist); the retrieve-wide-then-rerank-narrow
  funnel; the latency/cost tradeoff; the no-GPU constraint that forces an API reranker.
- **Test (`test_rerank.py`):** fake reranker → assert it receives the fused shortlist, reorders by
  score, and truncates to `top_n`.

### Q8 — Generation: grounded answer + citations  ← new generation LLM client
- **Code:** `app/generate/generate.py` — a **new Gemini generation client** (only the *embedding*
  client exists today; reuses the same `google-genai` SDK → no new dependency). System instruction:
  answer **only from provided context**, say "I don't know" when it's absent, **cite source chunk
  IDs**. Pydantic model for structured `{answer, citations[]}` output. Module-level seam.
  **Decisions here:** generation model id; citation granularity (chunk-level vs char-span) → ADR.
- **Teaching note** (`Q8-generation.md`): grounding via prompt (faithfulness); the "only from
  context" instruction and why it curbs hallucination; citation strategy; Pydantic structured output;
  temperature.
- **Test (`test_generate.py`):** fake LLM client → assert the prompt carries the retrieved context +
  the only-from-context instruction, and citations parse into the Pydantic model. `@pytest.mark.live`
  test for real grounding (answers from context; says "I don't know" when context lacks the answer).

### Q9 — Full RAGAS answer-eval  ← woven eval, late half
- **Code:** extend `/eval` with **RAGAS** metrics — faithfulness, answer relevance, context
  precision, context recall — over the golden set (add reference answers to `golden.jsonl`). Complete
  `python -m eval.run`; finish the `eval-run` skill. New dep `ragas` (+ its Gemini judge wiring).
- **Teaching note** (`Q9-ragas.md`): what each metric isolates and **which stage it blames**
  (context recall→retrieval, context precision→rerank/fusion, faithfulness→generation grounding,
  answer relevance→prompt); using it to A/B any earlier stage.
- **Test (`test_eval_ragas.py`):** metric-wiring smoke with a mocked LLM judge so the harness runs
  offline in CI; live RAGAS opt-in (`-m live`).

### Q10 — Query API endpoint (`POST /ask`) + per-query logging
- **Code:** `app/api/ask.py` — `POST /ask` runs `retrieve → rerank → generate`, returns
  `{answer, citations, retrieved_chunk_ids, reranked_chunk_ids}`. **Request-shaped** (synchronous),
  the deliberate contrast with job-shaped ingestion. Emit the locked **per-query structured log**
  (user message, retrieved IDs, reranked order, final context, answer, token/cost) — the eval
  substrate as live logs. Mount in `main.py` like `documents_router`.
- **Teaching note** (`Q10-ask-endpoint.md`): request-shaped vs job-shaped; the query log as eval raw
  material; why streaming is deferred (Phase 2.5).
- **Test (`test_ask.py`):** mirror `test_upload.py` — `ASGITransport`, `get_session` DI override,
  faked retrieve/generate seams; assert 200 + answer + citations + chunk-id lists, and that an
  empty/whitespace question is rejected before any LLM call.

## Deferred to Phase 2.5 (explicitly out of Phase 2)
LangGraph node orchestration (wrap Q3+Q7+Q8 as a graph: input-guard → query-process → retrieve →
rerank → generate → output-guard) · Guardrails AI I/O validation · SSE streaming on `/ask` · Redis
semantic cache · query rewrite / HyDE · frontend chat + upload UI.

## New dependencies (by milestone)
- Q1–Q6, Q8, Q10: **none** (pgvector + `google-genai` already installed; generation reuses the SDK).
- Q7: one reranker SDK **iff** a managed API is chosen (decided at Q7).
- Q9: `ragas`.

## Verification (how we prove each milestone works)
- **Offline suite:** `backend\.venv\Scripts\python.exe -m pytest` from repo root — every milestone
  lands red-first then green; suite stays 0-skip (Docker up for the pgvector testcontainer).
- **Live retrieval, interactively:** the `explain-retrieval` skill on a real query against the **dev
  DB** (`prodrag-postgres`, already holds 16 Aurelia chunks) — watch each stage's output as stages
  come online (semantic at Q3, +lexical/RRF at Q5–Q6, +rerank at Q7, +answer at Q8).
- **Quality, quantitatively:** `python -m eval.run --golden eval/golden.jsonl --out eval/results/`
  after Q4 (hit@k/MRR baseline) and re-run after Q5/Q6/Q7; RAGAS after Q9. Each retrieval milestone's
  teaching note records its eval delta.
- **End-to-end HTTP (after Q10):** `.\dev.ps1`, then
  `curl.exe -X POST -H "Content-Type: application/json" -d '{"query":"..."}' http://localhost:8000/ask`
  → grounded answer + citations; confirm a per-query log line was emitted.

## Open decisions to make at their milestone (not now)
- **Q1:** map `tsv` on the `Chunk` model vs raw-SQL-only (default: map it).
- **Q7:** reranker provider — managed rerank API vs Gemini-as-reranker (ADR).
- **Q8:** generation model id; citation granularity chunk vs span (ADR).
- **Q4/Q9:** golden-set size and source (hand-authored over the ingested corpus vs generated).
