# Project Development Tracker

> A living, layer-by-layer map of how far ProdRAG is built. Update the tables and the
> changelog as milestones land — don't re-derive from scratch each time.
> Companion to `docs/HANDOFF.md` (session state) and `CLAUDE.md` (the constitution).
>
> _Last updated: 2026-07-06 — M5 (write) landed: `write_chunks` maps chunk elements + M4
> vectors → `chunks` rows (flush, no commit; M6 owns the txn). First real-DB test in the repo
> — a throwaway `testcontainers` pgvector container + a rolled-back per-test session._

## How to read the scores

The system is measured as **10 architectural layers** (horizontal tiers, each owning one
concern). Every layer gets two completion numbers:

- **Code-only** — counts working, running code. Locked-but-unbuilt decisions score ~0.
- **Code + design credit** _(the baseline we report against)_ — also credits locked
  decisions and shaped-but-unfilled seams (e.g. the `owner_id` column, the job-shaped
  ingestion contract). This repo deliberately front-loads design, so this is the fairer lens.

The gap between the two numbers *is the story*: design risk is bought down before code piles up.

## Layer scorecard

| # | Layer | Owns | Code-only | **Code + design** |
|---|-------|------|-----------|-------------------|
| 1 | Presentation (Frontend) | Next.js chat/upload UI, streaming, auth session | ~5% | **~10%** |
| 2 | API (FastAPI) | upload/ask/list/stream endpoints; 202-and-poll upload contract | ~15% | **~25%** |
| 3 | Async / task queue | Celery workers + Redis broker/cache | ~5% | **~25%** |
| 4 | Ingestion pipeline | parse → chunk → embed → write, orchestrated | ~70% | **~76%** |
| 5 | Query / retrieval pipeline | embed → hybrid search → RRF → rerank → generate → guard | ~0% | **~15%** |
| 6 | Data & storage | Postgres+pgvector, schema, blob store | ~55% | **~70%** |
| 7 | AI / ML | Gemini LLM, embeddings, reranker, Guardrails, LangGraph | ~10% | **~28%** |
| 8 | Auth & isolation | Supabase JWT (ES256/JWKS) + RLS | ~5% | **~30%** |
| 9 | Deployment / Infra | Docker local + one-time Fargate | ~30% | **~40%** |
| 10 | Observability & Evaluation | Sentry, LangSmith, structured logs, RAGAS golden set | ~5% | **~20%** |

**Weighted overall: ~15–17% (code-only) · ~32% (code + design).**

## Notes per layer (why the score, what's next)

1. **Presentation** — only a `/health/db` JSON page exists; builds clean. Stack fixed, but no
   UI beyond the health probe. Chat + upload UI are Phase 2 / late Phase 1.
2. **API** — Phase 0 spine: CORS, async DB session, `GET /health` + `GET /health/db`. The RAG
   endpoints (upload=M7, ask, list) aren't coded; the 202-and-poll upload contract is fully
   specified, which is the design credit.
3. **Async / queue** — Redis container runs but is unwired. No Celery yet. Ingestion is
   *job-shaped* by design so the Celery swap is one line (`await orchestrate(id)` →
   `orchestrate.delay(id)`) — that shaped seam is the credit.
4. **Ingestion** — the critical path. Milestone detail below.
5. **Query pipeline** — no code; entire pipeline shape is locked (hybrid → RRF → cross-encoder
   → 5–10 chunks). Blocked on layer 4 landing embedded chunks.
6. **Data & storage** — Postgres+pgvector up; `documents`+`chunks` schema applied &
   round-trip tested; local-disk storage seam done. HNSW index, `tsvector` column, and S3 are
   deferred *by design* (all derived from stored data → no re-ingest to switch on).
7. **AI / ML** — **embeddings now wired** (`embed_texts` via Gemini `google-genai`, 768d,
   auto-normalized by v2 + a defensive L2 no-op; surface decided = Developer API, not Vertex).
   Doc/query role is a text **prefix** (`as_retrieval_document` / `as_retrieval_query`), not a
   `task_type` field — `gemini-embedding-2` dropped it. Model ID **confirmed** via the live test.
   LLM generation, reranker, Guardrails, LangGraph still unbuilt (Phase 2); the query-side
   `as_retrieval_query` call lands with retrieval.
8. **Auth & isolation** — biggest design-vs-code gap. JWKS/ES256 verification, the RLS policy
   pattern, the Supavisor-bypasses-RLS caveat, and `owner_id NOT NULL` are all decided &
   documented; only enforcement code is missing.
9. **Deployment / Infra** — local `docker-compose` (Postgres+Redis) + `dev.ps1`/`stop.ps1` are
   real. Fargate is a deliberate one-time learning touch, not yet done (and mostly out of scope).
10. **Observability & Eval** — logging contract + "eval as substrate" strategy + `.claude` eval
    skill scaffolds exist; no tracing, no golden set, no wiring.

## Ingestion milestones (Layer 4 detail)

The pipeline is broken into M0–M7. This is where near-term progress happens.

| Milestone | What | Status |
|-----------|------|--------|
| M0 | Ingestion schema (`documents` + `chunks`) | ✅ complete & verified |
| M1 | Storage interface (`Storage` Protocol + `LocalDiskStorage`) | ✅ complete & verified |
| M2 | Parse (Unstructured path → typed elements; `infer_table_structure=True`; PDF default `hi_res`) | ✅ complete & verified — text + PDF `hi_res` table path both under automated test |
| M3 | Chunk (`by_title`; `max_characters=1500`, `combine_text_under_n_chars=500`; carries `text_as_html` onto the isolated Table chunk) | ✅ complete & verified — grouping + hard-cap + table `text_as_html` all under automated test |
| M4 | Embed (`gemini-embedding-2` @768d, batched; role via text **prefix** helpers — v2 has no `task_type`; v2 auto-normalizes + defensive L2; **interface A** — `embed_texts(list[str])`, reused at query time, enrich resolves `embed_text` upstream) | ✅ complete & verified — order/batching/no-`task_type`+dims/prefix-format/empty/unit-length under mock test; 1 opt-in live test passed against real Gemini (deselected, not skipped) |
| M5 | Write (elements → `Chunk` ORM rows + vectors; set `element_type`, store `text_as_html` in `metadata` JSONB) | ✅ complete & verified — `write_chunks` flushes (no commit; M6 owns the txn); mining + pgvector/JSONB round-trip under a real-Postgres testcontainers test |
| M-enrich | LLM summary for `Table`/`Image` chunks → fills `embed_text` (runs between M3 and M4) | ⬜ deferred — gated on eval; needs a generation LLM client |
| M6 | Orchestrator (self-contained coroutine keyed on `document_id`) | ⬜ |
| M7 | Upload endpoint (persist + `pending` row + kick job + 202 + doc id) | ⬜ |

## Open loose ends (inside "done" work)

- **M2 PDF path proven** — `test_parse_pdf_hi_res_infers_table_structure` drives a committed
  table PDF (`tests/fixtures/quarterly_report.pdf`) under `hi_res` and asserts the Table keeps
  `text_as_html`. poppler + tesseract confirmed installed. The suite now depends on those
  binaries (and downloads the table-transformer model on first run) for that one test — a
  deliberate trade to keep the table-structure guarantee under automated red/green.
- **Repo-wide lint not green** — an `app/models.py` M0 comment exceeds 88 chars (ruff E501 /
  black). Everything else is clean.
- **M4 + M5 committed but UNPUSHED** — M2+M3 are committed & pushed; M4 (`1a52ade`) and the M5
  write stage (this commit: `write.py`, `conftest.py`, `test_write.py`, the `testcontainers`
  dev dep, + these doc refreshes) are committed locally, awaiting `git push`. See HANDOFF
  "Git state".

## Changelog

- **2026-07-06** — M5 (write) complete. `app/ingest/write.py` `write_chunks(session,
  document_id, chunks, embeddings, *, owner_id)`: the translation boundary that maps each
  Unstructured chunk element + its aligned M4 vector into a `Chunk` ORM row. Two aligned
  lists (never `embed(content)`) so the deferred enrich seam lands with zero M5 change; a
  length-mismatch raises before any DB work. `flush` but **no commit** — M6 owns the
  transaction (chunk-write + `documents.status` commit together). Mines `section_title`
  (first `Title` in `orig_elements`), `page_number`, `element_type` (`type().__name__`),
  and lands a table's `text_as_html` in the `metadata` JSONB; `token_count` stays NULL
  (deferred). **First real-DB test in the repo:** new `backend/tests/conftest.py` boots a
  throwaway `pgvector/pgvector:pg16` **testcontainers** container once per session, applies
  `002_schema.sql` (via a raw asyncpg connection — SQLAlchemy's asyncpg dialect can't run a
  multi-statement script), and a function-scoped `db_session` wraps each test in a rolled-
  back transaction. 4 immutable tests (1 offline length guard + 3 real-DB round-trips using
  synthetic elements + synthetic 768-float vectors, so no paid Gemini call and no
  poppler/tesseract). Suite now **21 collected → 20 passed, 1 deselected (live), 0
  skipped** (was 16+1); touched files lint-clean. New dev dep
  `testcontainers[postgres]==4.14.2` (needs a running Docker daemon; no psycopg2).
- **2026-07-06** — M4 (embed) complete. `app/ingest/embed.py` `embed_texts(list[str])` via
  Gemini (`google-genai==2.10.0`, async `client.aio`); **interface A** (pure text embedder,
  reused at query time), batched, empty→[], 768-dim vectors. **Role via text prefix, not
  `task_type`:** `gemini-embedding-2` dropped the `task_type` config field (v2 ignores it), so the
  doc/query asymmetry is now a prefix on the input — built by the helpers `as_retrieval_document`
  / `as_retrieval_query`; `embed_texts` is role-agnostic. `_l2_normalize` kept as a defensive
  no-op (v2 auto-normalizes truncated vectors via Matryoshka training — the old "un-normalized
  <3072" reason no longer holds). 5 mock tests (offline, pin the contract incl. no-`task_type` +
  the prefix formats) + 1 opt-in live test (`-m live`, deselected not skipped) — **live test
  passed against the real API, confirming the `gemini-embedding-2` ID**. Config gained
  `gemini_api_key` / `embedding_model` / `embedding_dimensions` / `embedding_batch_size`. Suite
  now **16 tests** (+1 live deselected); touched files lint-clean. Also: documented the parse
  `to_thread` concurrency footgun (Option A, deferred).
- **2026-07-06** — M3 (chunk) complete. `app/ingest/chunk.py` (`chunk_document`, async
  seam over Unstructured `by_title`), `max_characters=1500` / `combine_text_under_n_chars
  =500` in config, `/ingest-inspect` grown a chunk view (`--max-chars`/`--combine`). Two
  immutable chunk tests (grouping+hard-cap on markdown; table `text_as_html` survives on
  the PDF). Suite now **11 tests** (6 storage + 3 parse + 2 chunk); touched files lint-clean.
- **2026-07-05** — First full-codebase layer review. Established the 10-layer rubric and the
  "code + design credit" reporting baseline. Snapshot: ~30% overall (code+design).
