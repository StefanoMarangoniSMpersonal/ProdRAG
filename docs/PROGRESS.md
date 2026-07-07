# Project Development Tracker

> A living, layer-by-layer map of how far ProdRAG is built. Update the tables and the
> changelog as milestones land — don't re-derive from scratch each time.
> Companion to `docs/HANDOFF.md` (session state) and `CLAUDE.md` (the constitution).
>
> _Last updated: 2026-07-07 — M6 (orchestrator) landed: `orchestrate(document_id)` runs the
> whole pipeline end to end (open_local → parse → chunk → embed → write), drives
> `documents.status`, owns the commit via a three-transaction lifecycle. First code that
> persists retrievable chunks end to end. A live end-to-end run against real Gemini then
> exposed & fixed an M4 batch bug (v2 folds `list[str]` into one fused vector — must pass
> one `Content` per input). Suite = 26 pass + 2 deselected (live)._

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
| 4 | Ingestion pipeline | parse → chunk → embed → write, orchestrated | ~85% | **~90%** |
| 5 | Query / retrieval pipeline | embed → hybrid search → RRF → rerank → generate → guard | ~0% | **~15%** |
| 6 | Data & storage | Postgres+pgvector, schema, blob store | ~55% | **~70%** |
| 7 | AI / ML | Gemini LLM, embeddings, reranker, Guardrails, LangGraph | ~10% | **~28%** |
| 8 | Auth & isolation | Supabase JWT (ES256/JWKS) + RLS | ~5% | **~30%** |
| 9 | Deployment / Infra | Docker local + one-time Fargate | ~30% | **~40%** |
| 10 | Observability & Evaluation | Sentry, LangSmith, structured logs, RAGAS golden set | ~5% | **~20%** |

**Weighted overall: ~17–19% (code-only) · ~34% (code + design).**

## Notes per layer (why the score, what's next)

1. **Presentation** — only a `/health/db` JSON page exists; builds clean. Stack fixed, but no
   UI beyond the health probe. Chat + upload UI are Phase 2 / late Phase 1.
2. **API** — Phase 0 spine: CORS, async DB session, `GET /health` + `GET /health/db`. The RAG
   endpoints (upload=M7, ask, list) aren't coded; the 202-and-poll upload contract is fully
   specified, and the job it kicks (`orchestrate`) now exists — M7 is just the HTTP shell +
   `pending`-row insert on top of it.
3. **Async / queue** — Redis container runs but is unwired. No Celery yet. Ingestion is
   *job-shaped* by design so the Celery swap is one line (`await orchestrate(id)` →
   `orchestrate.delay(id)`) — and `orchestrate` now exists in that exact shape (takes only a
   `document_id`, returns a result no caller waits on), so the seam is real, not just planned.
4. **Ingestion** — the critical path. Milestone detail below.
5. **Query pipeline** — no code; entire pipeline shape is locked (hybrid → RRF → cross-encoder
   → 5–10 chunks). Blocked on layer 4 landing embedded chunks.
6. **Data & storage** — Postgres+pgvector up; `documents`+`chunks` schema applied &
   round-trip tested; local-disk storage seam done. HNSW index, `tsvector` column, and S3 are
   deferred *by design* (all derived from stored data → no re-ingest to switch on).
7. **AI / ML** — **embeddings now wired** (`embed_texts` via Gemini `google-genai`, 768d,
   auto-normalized by v2 + a defensive L2 no-op; surface decided = Developer API, not Vertex).
   Doc/query role is a text **prefix** (`as_retrieval_document` / `as_retrieval_query`), not a
   `task_type` field — `gemini-embedding-2` dropped it. Model ID **confirmed** via the live test,
   and the **batch contract** now proven against the real API: each input must be its own
   `types.Content` (a bare `list[str]` is read as one multi-part input → one fused vector) —
   caught 2026-07-07 by the live end-to-end run, invisible to the offline mock. LLM generation,
   reranker, Guardrails, LangGraph still unbuilt (Phase 2); the query-side `as_retrieval_query`
   call lands with retrieval.
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
| M4 | Embed (`gemini-embedding-2` @768d, batched **one `Content` per input**; role via text **prefix** helpers — v2 has no `task_type`; v2 auto-normalizes + defensive L2; **interface A** — `embed_texts(list[str])`, reused at query time, enrich resolves `embed_text` upstream) | ✅ complete & verified — order/batching/no-`task_type`+dims/prefix-format/empty/unit-length under mock test; **batch contract corrected 2026-07-07** (real API folds a raw `list[str]` into 1 vector → wrap each input in its own `types.Content`; caught by the live end-to-end run, mock revised); 2 opt-in live tests pass against real Gemini (embed + full end-to-end; deselected, not skipped) |
| M5 | Write (elements → `Chunk` ORM rows + vectors; set `element_type`, store `text_as_html` in `metadata` JSONB) | ✅ complete & verified — `write_chunks` flushes (no commit; M6 owns the txn); mining + pgvector/JSONB round-trip under a real-Postgres testcontainers test |
| M-enrich | LLM summary for `Table`/`Image` chunks → fills `embed_text` (runs between M3 and M4) | ⬜ deferred — gated on eval; needs a generation LLM client |
| M6 | Orchestrator (self-contained coroutine keyed on `document_id`; three-txn lifecycle, owns the commit, `open_local` path seam, `IngestResult` + structured metrics) | ✅ complete & verified — real parse+chunk+write end-to-end + failure/atomic-rollback + missing-doc, under a committing real-Postgres test |
| M7 | Upload endpoint (persist + `pending` row + kick job + 202 + doc id) | ⬜ next |

## Open loose ends (inside "done" work)

- **M2 PDF path proven** — `test_parse_pdf_hi_res_infers_table_structure` drives a committed
  table PDF (`tests/fixtures/quarterly_report.pdf`) under `hi_res` and asserts the Table keeps
  `text_as_html`. poppler + tesseract confirmed installed. The suite now depends on those
  binaries (and downloads the table-transformer model on first run) for that one test — a
  deliberate trade to keep the table-structure guarantee under automated red/green.
- **Repo-wide lint status — all `app/` source clean; `test_embed.py` now clean too.** The
  `app/models.py` E501 nit is fixed (M6 session), and `tests/test_embed.py` was hand-cleaned
  (2026-07-07, alongside the batch fix — the `import os` is live again via the restored live-test
  key guard). Remaining debt is a single **pre-existing, immutable** file: `tests/test_parse.py`
  isn't `black`-clean (two commented-out lines). Left untouched on purpose (editing immutable
  specs is a deliberate call) — worth a separate formatting-only cleanup commit.
- **M4 + M5 committed but UNPUSHED; M6 + the M4 batch fix uncommitted** — M2+M3 are committed &
  pushed; M4 (`1a52ade`) and M5 (`d9f4fd6`) are committed locally awaiting `git push`; the working
  tree holds the M6 work (`orchestrate.py`, `open_local`, the committing `session_factory` fixture,
  `test_orchestrate.py`, the `open_local` + `models.py` edits) **plus the 2026-07-07 M4 batch-
  contract fix** (`embed.py` one-`Content`-per-input, the `test_embed.py` mock revision, the new
  `@pytest.mark.live` end-to-end test) + these doc refreshes — none committed yet. See HANDOFF
  "Git state".

## Changelog

- **2026-07-07** — M4 embed **batch-contract fix** (found by running M6 end-to-end against
  *real* Gemini). A new `@pytest.mark.live` `test_orchestrate_ingests_document_end_to_end_live`
  ran the whole pipeline with the real embedding call and failed at the write stage: 16 chunks
  came back as **1** vector. Root cause: `embed_texts` passed `contents=list[str]` to
  `embed_content`, and gemini-embedding-2's SDK reads a bare string list as the many *Parts of
  one Content* → a single fused vector for the whole batch. The offline mock returned one vector
  per list element, so every mock test passed while reality was broken — exactly what a live test
  exists to catch. **Fix:** wrap each text as its own `types.Content(parts=[Part(text=t)])`, so a
  batch of N strings → N Contents → N vectors, still one API call per batch. Architect authorized
  the accompanying **mock revision** (it encoded the wrong contract): added a `_sent_texts` helper
  and updated two assertions in the immutable `test_embed.py` (red-first re-confirmed, then green).
  Both live tests now pass against real Gemini (`test_embed_live_real_gemini` + the new end-to-end,
  which writes 16 distinct 768-dim unit vectors to Postgres and lands the doc `ready`). Suite =
  **26 pass + 2 deselected (live), 0 skip**; `embed.py` SDK-surface docstring corrected; all
  touched files ruff+black clean.
- **2026-07-07** — M6 (orchestrator) complete. `app/ingest/orchestrate.py`
  `orchestrate(document_id) -> IngestResult`: the conductor, and the first code that runs
  parse → chunk → embed → write together against a real document and persists retrievable
  chunks end to end. Drives `documents.status` (`pending → processing → ready|failed`) and
  **owns the commit** via a **three-transaction lifecycle** — Txn1 claims the row
  (`processing`, committed alone so progress is observable and no DB connection is held
  across the slow parse/embed), the heavy stages run with no transaction open, Txn2 writes
  chunks + flips to `ready` in **one atomic commit**, and on any failure a fresh Txn3 records
  `failed` + the error (surviving the poisoned rollback). Four architect decisions locked:
  (1) **`open_local` added to the Storage seam** — M6 gets a real *path* and never loads
  bytes into RAM (corrects the old "M6 reads bytes" plan language; `LocalDiskStorage` yields
  its on-disk path zero-copy, S3 later spills to a temp file); (2) **keep the blob on
  failure** (retry/debug; lifecycle belongs to the row's creator); (3) **return an
  `IngestResult`** (status, chunk_count, per-stage `timings_ms`) for tests + eval-substrate,
  with structured logs regardless; (4) **M7 owns `byte_size`/`checksum`/`content_type`** so
  M6 stays purely path-based. Collaborators are module globals (monkeypatch seam). New
  committing `session_factory` fixture in `conftest.py` (TRUNCATE teardown) — the
  rollback-based `db_session` can't be used because M6 commits for real. 3 immutable tests
  (`test_orchestrate.py`, real parse+chunk on the md corpus + faked embed + real Postgres
  write: ready path, failure/atomic-rollback + blob-kept, missing-doc raises) + 3 `open_local`
  tests. Suite now **26 passed, 1 deselected (live), 0 skipped** (was 20+1). All `app/` source
  ruff+black clean (incl. the `models.py` E501 nit, finally fixed).
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
