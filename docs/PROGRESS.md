# Project Development Tracker

> A living, layer-by-layer map of how far ProdRAG is built. Update the tables and the
> changelog as milestones land — don't re-derive from scratch each time.
> Companion to `docs/HANDOFF.md` (session state) and `CLAUDE.md` (the constitution).
>
> _Last updated: 2026-07-13 — **Phase 2 has started: Q1 (retrieval indexes) is done & verified,
> not yet committed.** A new append-only `003_retrieval_indexes.sql` switches on the **HNSW**
> vector index (`vector_cosine_ops`) + a generated **`tsv tsvector`** column and its **GIN**
> index; `Chunk.tsv` is mapped read-only; the testcontainers harness now **globs all migrations**
> (was hardcoded to `002`). Both objects derive from stored data → applied to the existing dev
> rows **with no re-ingest** (verified: row count unchanged, `tsv` auto-populated, EXPLAIN shows
> the HNSW index is valid). Test-first (`test_retrieve_schema.py`) + teaching note
> (`docs/learning/Q1-indexes.md`). Suite = **38 pass + 2 deselected (live)** (was 35). Previously
> (2026-07-11): the async layer was proven live end to end (HTTP-through-worker smoke test) and a
> `dev.ps1` Windows Beat-window fix landed (`c2994f9`)._

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
| 2 | API (FastAPI) | upload/ask/list/stream endpoints; 202-and-poll upload contract | ~30% | **~40%** |
| 3 | Async / task queue | Celery workers + Redis broker/cache | ~70% | **~80%** |
| 4 | Ingestion pipeline | parse → chunk → embed → write, orchestrated | ~95% | **~98%** |
| 5 | Query / retrieval pipeline | embed → hybrid search → RRF → rerank → generate → guard | ~3% | **~18%** |
| 6 | Data & storage | Postgres+pgvector, schema, blob store | ~65% | **~76%** |
| 7 | AI / ML | Gemini LLM, embeddings, reranker, Guardrails, LangGraph | ~10% | **~28%** |
| 8 | Auth & isolation | Supabase JWT (ES256/JWKS) + RLS | ~5% | **~30%** |
| 9 | Deployment / Infra | Docker local + one-time Fargate | ~30% | **~40%** |
| 10 | Observability & Evaluation | Sentry, LangSmith, structured logs, RAGAS golden set | ~5% | **~20%** |

**Weighted overall: ~24–26% (code-only) · ~41% (code + design).**

## Notes per layer (why the score, what's next)

1. **Presentation** — only a `/health/db` JSON page exists; builds clean. Stack fixed, but no
   UI beyond the health probe. Chat + upload UI are Phase 2 / late Phase 1.
2. **API** — Phase 0 spine (CORS, async DB session, `GET /health` + `/health/db`) plus the
   **M7 ingestion surface**: `POST /documents` (202-and-poll upload) + `GET /documents/{id}`
   (status poll), on an `app/api/documents.py` router. The upload contract is now *code*, not
   just spec — it stores the blob, inserts the `pending` row, and now **enqueues** the job via
   `orchestrate_task.delay(str(id))` onto Celery (the `BackgroundTasks` seam swapped, exactly
   as designed). Still to build: `ask`, `list`, streaming (Phase 2).
3. **Async / queue** — **now wired.** Redis is the Celery broker; `app/worker.py` holds the
   Celery app, `orchestrate_task` (a sync task bridging to async `orchestrate` via
   `asyncio.run` + a NullPool session factory installed at worker startup to dodge the
   cross-event-loop asyncpg trap), and `reap_stuck_documents` (the Beat reaper). Broker-only:
   no result backend — `documents.status` in Postgres is the source of truth. The
   **attempt-fence** (`documents.attempt`, gated in orchestrate's three txns) makes the reaper
   safe. **Proven live 2026-07-11** — the HTTP-through-worker smoke test ran a real upload
   through Redis → worker → `ready` (16 chunks, `attempt=1`), so the enqueue/broker/NullPool-
   session path is verified, not just unit-tested. Remaining gap to 100%: the reaper's requeue
   path is still only unit-tested (not yet watched live), no result introspection/Flower, and
   the worker still uses a direct DB connection (the Supavisor pooled-worker URL is the
   documented later swap).
4. **Ingestion** — the critical path. Milestone detail below.
5. **Query pipeline** — **Phase 2 started.** Q1 landed the *storage substrate* (HNSW + `tsv`,
   see Layer 6), but no query *code* yet — `app/retrieve/` doesn't exist. Entire pipeline shape
   is locked (hybrid → RRF → cross-encoder → 5–10 chunks); the approved Q1–Q10 breakdown is
   `docs/PHASE2-PLAN.md`. Next = **Q2** (`app/retrieve/semantic.py`, naive vector search).
6. **Data & storage** — Postgres+pgvector up; `documents`+`chunks` schema applied &
   round-trip tested; local-disk storage seam done. **HNSW index + `tsvector` column now
   switched on (Q1, migration `003`)** — both derived from stored data, applied with no
   re-ingest, and verified against the dev DB (EXPLAIN confirms the HNSW index serves the
   cosine `ORDER BY`). S3 blob store is still deferred *by design*.
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
| M7 | Upload endpoint (persist + `pending` row + kick job + 202 + doc id) | ✅ complete & verified — `POST /documents` + `GET /documents/{id}` on `app/api/documents.py`; empty upload → 400 before any write; job now **enqueued via `orchestrate_task.delay`** (was FastAPI `BackgroundTasks` at landing — the one-line Celery seam swapped in M8); 4 immutable tests drive the ASGI app (httpx `ASGITransport`) over the real-Postgres container with the enqueue faked |
| M8 | Async layer (Celery worker + Redis broker + fenced stuck-job reaper) | ✅ complete, committed (`729e651`) **& proven live** — `app/worker.py` (`orchestrate_task` sync→async bridge via `asyncio.run` + NullPool `SessionLocal` at worker start; `reap_stuck_documents` Beat reaper); **attempt-fence** `documents.attempt` threaded through orchestrate's 3 txns (claim-and-increment, then fence the results/failure commits) so a reaper requeue can't corrupt a live worker; broker-only (no result backend); `write.py` untouched (fence-only idempotency). 5 immutable tests (`test_worker.py`): superseded run writes nothing / doesn't stamp failed, non-pending claim skips, task bridge, reaper requeues-stale-and-fails-poison. **HTTP-through-worker smoke passed 2026-07-11** (upload → Redis → worker → `ready`, 16 chunks · 768d · `attempt=1`) |

## Query / retrieval milestones (Layer 5 detail)

Phase 2 breaks into **Q1–Q10** (approved plan: `docs/PHASE2-PLAN.md`). Each ships test-first
code **plus** a teaching note (`docs/learning/Qx-*.md`); genuine forks also get an ADR.

| Milestone | What | Status |
|-----------|------|--------|
| Q1 | Schema switch-on: HNSW index + generated `tsv tsvector` + GIN index (migration `003`; `Chunk.tsv` mapped; conftest globs all migrations) | ✅ complete & verified (not yet committed) — `test_retrieve_schema.py`; note `docs/learning/Q1-indexes.md`; dev DB migrated in place, no re-ingest |
| Q2 | Semantic retrieval — `app/retrieve/semantic.py` `search_semantic → list[ScoredChunk]`, cosine order, `ef_search` knob | ⬜ next |
| Q3 | Retrieval orchestrator + query embedding (`retrieve() → RetrievalResult`); wire `explain-retrieval` semantic stage | ⬜ |
| Q4 | Retrieval eval baseline — `/eval` package, `golden.jsonl`, hit@k / MRR (woven eval begins) | ⬜ |
| Q5 | Lexical retrieval — `search_lexical` via `websearch_to_tsquery` + `ts_rank_cd` over `chunks.tsv` | ⬜ |
| Q6 | Hybrid fusion — Reciprocal Rank Fusion (semantic + lexical concurrently) | ⬜ |
| Q7 | Cross-encoder rerank — retrieve-wide → rerank to 5–10; **provider decision (ADR)** | ⬜ |
| Q8 | Generation — grounded, cited answer via a new Gemini generation client (ADR: model + citation granularity) | ⬜ |
| Q9 | Full RAGAS answer-eval (faithfulness / relevance / context precision+recall) | ⬜ |
| Q10 | Query API endpoint `POST /ask` (request-shaped) + per-query structured log | ⬜ |

## Open loose ends (inside "done" work)

- **M2 PDF path proven** — `test_parse_pdf_hi_res_infers_table_structure` drives a table PDF
  (`tests/fixtures/Proactive Autoscaling.pdf`) under `hi_res` and asserts the Table keeps
  `text_as_html`. poppler + tesseract confirmed installed. The suite now depends on those
  binaries (and downloads the table-transformer model on first run) for that one test — a
  deliberate trade to keep the table-structure guarantee under automated red/green.
  ⚠ **Fixture footgun (resolved):** this PDF had only ever been *untracked*, so a working-tree
  cleanup deleted it and the test broke (`FileNotFoundError`). It's restored and is being
  **committed (tracked) with the async layer** so it can't vanish again; the test's two
  `Falcon-9X` sanity lines stay commented out (immutable). `quarterly_report.pdf` is still a
  committed fixture but no longer referenced by any test.
- **Repo-wide lint status — all `app/` source clean; `test_embed.py` now clean too.** The
  `app/models.py` E501 nit is fixed (M6 session), and `tests/test_embed.py` was hand-cleaned
  (2026-07-07, alongside the batch fix — the `import os` is live again via the restored live-test
  key guard). Remaining debt is a single **pre-existing, immutable** file: `tests/test_parse.py`
  isn't `black`-clean (two commented-out lines). Left untouched on purpose (editing immutable
  specs is a deliberate call) — worth a separate formatting-only cleanup commit.
- **M0–M8 are committed & pushed** — `main` at `729e651` (the async layer / M8), on top of
  `8fa0bd5` (housekeeping: `backend/pyproject.toml` `fastapi.File` B008 exemption +
  `infra/db/explore.sql`, so `ruff check backend` is clean). **This session's tooling/doc
  commit** lands on top: the `dev.ps1` Beat-window fix, `.gitignore` `celerybeat-schedule*`,
  and these doc refreshes — no app code, no test change. (`.vscode/settings.json`, the
  SQLTools connection, is git-ignored and won't be committed.)

## Changelog

- **2026-07-13 (Phase 2 begins — Q1: retrieval indexes)** — switched on the two DB capabilities
  the query pipeline reads, both derived from already-stored data so they applied to the existing
  dev rows **with no re-ingest**. New append-only **`infra/db/migrations/003_retrieval_indexes.sql`**
  (successor to the immutable `002`; `IF NOT EXISTS` throughout): **`idx_chunks_embedding_hnsw`**
  (HNSW, `vector_cosine_ops` — cosine because embeddings are L2-normalized at ingest, so cosine ≡
  inner product); a generated **`tsv tsvector`** column (`GENERATED ALWAYS AS
  (to_tsvector('english', content)) STORED` — the DB keeps it in sync with `content`, the app never
  writes it); and **`idx_chunks_tsv`** (GIN). **`Chunk.tsv`** mapped read-only on the model
  (`mapped_column(TSVECTOR, Computed(..., persisted=True))`) so the model stays a complete mirror of
  the schema (Option A); `write.py` untouched. **Harness change (the gotcha):** `conftest.py`
  hardcoded applying only `002_schema.sql`, so a `003` file would never reach the testcontainer —
  it now **globs `infra/db/migrations/*.sql` in filename order** (mirroring `apply-migrations.ps1`),
  so the test schema is built by the same path as the dev DB and every future migration auto-applies
  (closes a "forgot to register a migration → false green" class). **Test-first, 3 immutable tests**
  (`test_retrieve_schema.py`, committing `session_factory`): index presence via `pg_indexes` (not
  *usage* — would flap on a tiny table), nearest-vector order via `Chunk.embedding.cosine_distance`
  (works without the index — HNSW changes speed, not results), and `tsv @@ websearch_to_tsquery`
  full-text match. Teaching note **`docs/learning/Q1-indexes.md`** (new `docs/learning/` folder):
  ANN vs exact, HNSW `m`/`ef_construction`/`ef_search`, cosine-on-normalized, GIN vs GiST, and why
  Postgres FTS is "BM25-ish" not Okapi (ParadeDB/`pg_search` = the deferred real-BM25 upgrade).
  **Verified:** suite **38 pass + 2 deselected (live), 0 skip** (was 35); ruff clean, black clean on
  touched files (only pre-existing immutable `test_parse.py` still fails black). Dev DB migrated in
  place via `apply-migrations.ps1` — row count unchanged (32 chunks / 2 docs), `tsv` populated on all
  32, both indexes present, EXPLAIN shows a Seq Scan naturally (32 rows) but `Index Scan using
  idx_chunks_embedding_hnsw` under `enable_seqscan=off` (index valid; planner picks it as the corpus
  grows). **Not yet committed — under review.**
- **2026-07-11 (async layer proven live + `dev.ps1` Windows fix)** — ran the **HTTP-through-
  worker smoke test** for the first time — the one seam of M8 never previously exercised (all
  prior live proof was in-process via `test_orchestrate_..._live`, never through a real Celery
  worker over Redis). `curl.exe -F file=@rag_test_document.md POST /documents` → `202 + id`,
  the worker consumed the job off Redis, and the doc walked `pending → processing → ready`; the
  dev DB confirmed **16 chunks · 768-dim vectors · `attempt=1`** (the uncontended fence path).
  This verifies the enqueue → broker → `asyncio.run` → NullPool-session → atomic commit chain
  live. **Fixed a `dev.ps1` Windows bug surfaced by the run:** the worker window baked in `-B`
  (embedded Beat), which Celery rejects on Windows ("does not work on Windows") — split Beat
  into its **own window** (`celery -A app.worker beat -l info`); `dev.ps1` now opens four
  windows (API, worker, beat, frontend). Added `.gitignore` entries for `celerybeat-schedule*`
  / `celerybeat.pid` (Beat's local schedule DB, written into `backend/`). Also recorded the
  `curl` vs `curl.exe` Windows gotcha (bare `curl` is an `Invoke-WebRequest` alias that can't
  do `-F`/`@file`). **Tooling + docs only — no app code, no test change; suite unchanged at
  35 + 2.**
- **2026-07-10 (async layer — M8)** — **ingestion is now production-shaped: a Celery worker
  over Redis, with a fenced stuck-job reaper.** The M7 `BackgroundTasks` kick became
  `orchestrate_task.delay(str(doc.id))` (`app/api/documents.py`); the new **`app/worker.py`**
  holds the Celery app (broker-only — no result backend, `documents.status` stays the source
  of truth), `orchestrate_task` (a *sync* task bridging to the *async* `orchestrate` via
  `asyncio.run`), and `reap_stuck_documents` (a Celery-Beat reaper). Two traps handled in
  code: (1) `asyncio.run` makes a fresh event loop per task, so reusing a pooled asyncpg
  connection across loops raises "Future attached to a different loop" — the worker installs a
  **NullPool** `async_sessionmaker` onto orchestrate's `SessionLocal` seam at
  `worker_process_init`; (2) UUIDs cross the JSON broker as `str`, re-parsed in the task. On
  Windows the worker must run `--pool=solo` (default prefork pool is broken). **The reaper is
  made safe by an attempt-fence:** new column `documents.attempt` (`ALTER … ADD COLUMN IF NOT
  EXISTS`, mirrored on the `Document` model) is a fencing token bumped once per claim;
  `orchestrate`'s three transactions now (Txn 1) claim-and-increment atomically in SQL
  (`WHERE status='pending' RETURNING attempt`; a non-pending row → `skipped` no-op), and
  (Txn 2 results / Txn 3 failure) gate their commits on `attempt = my_attempt` — a superseded
  run (a reaper requeued the doc and a newer worker re-claimed it) rolls back and writes zero
  chunks / doesn't stamp `failed` over the winner. `IngestResult.status` gained `superseded`
  and `skipped` (in-memory signals, never row states). **`write.py` (M5) untouched** — the
  fence guarantees the winner always writes into an empty chunk set, so fence-only idempotency
  suffices (no delete-then-insert). Reaper poison-pill cap: past
  `ingest_max_processing_attempts` (default 3) it marks the doc `failed` instead of looping.
  New config (`config.py`): `celery_broker_url`, `ingest_stuck_after_seconds` (600),
  `ingest_reaper_interval_seconds` (120), `ingest_max_processing_attempts` (3). New dep
  `celery[redis]==5.6.3` (Celery caps redis `<6.5`). `dev.ps1` opens a third window for the
  worker+Beat. **Test-first, 5 immutable tests** (`test_worker.py`, on the committing
  testcontainers `session_factory`): superseded run writes nothing / doesn't record `failed`,
  non-pending claim skips, the task bridges to `orchestrate` (eager mode, sync test — the body
  calls `asyncio.run`), and the reaper requeues a stale row + fails a poison one + leaves a
  fresh one. **Authorized immutable-test revision** to `test_upload.py`: the `wired` fixture
  now patches `documents.orchestrate_task.delay` (a recorder) instead of `documents.orchestrate`
  — all other assertions byte-for-byte. Immutable `test_orchestrate.py` unchanged and still
  green (single-worker behaviour identical: attempt 0→1, fence matches). Suite = **35 passed,
  2 deselected (live), 0 skipped** (was 30+2); ruff clean. Also restored the
  `Proactive Autoscaling.pdf` fixture the immutable parse test references (committed tracked
  this time). **Committed & pushed as `729e651`.**
- **2026-07-10 (housekeeping)** — landed the two files held back from the M7 commit
  (`8fa0bd5`): `backend/pyproject.toml` (the `fastapi.File` ruff B008 exemption, so
  `ruff check backend` is clean) and `infra/db/explore.sql` (the DB browsing queries), plus a
  doc refresh. Pushed; `main` in sync with `origin/main`.
- **2026-07-10 (verification + tooling)** — M7 proven live + DB inspection set up. Ran the opt-in
  `test_orchestrate_..._live` against **real Gemini** through the whole pipeline on the Aurelia
  fixture: 16 chunks written, doc `ready`, timings ≈ parse 4.5 s / embed 3.6 s / write 26 ms /
  total 8.2 s — the M4 one-`Content`-per-input batch fix holds against the live API (16 distinct
  768-dim unit vectors). Clarified the two-DB reality: the suite's **testcontainers** Postgres is
  `TRUNCATE`d + destroyed per run (ephemeral), while the persistent **dev DB** (`prodrag-postgres`,
  volume `infra_pgdata`, `localhost:5432`) currently holds an earlier ingest of the same fixture
  (1 `documents` row `ready` + 16 `chunks`). Added a **DB GUI**: VS Code **SQLTools** + PostgreSQL
  driver extensions, a connection in git-ignored `.vscode/settings.json` (localhost:5432,
  prodrag/prodrag/prodrag), and a new tracked **`infra/db/explore.sql`** — 6 read-only browsing
  queries (documents/chunks overview, table `text_as_html`, embedding peek, chunk↔document join),
  all validated against the dev DB. No app-code change — verification + dev tooling only.
- **2026-07-10** — **M7 (upload endpoint) complete — Phase 1's ingestion path is now wired
  end to end from an HTTP request.** New `app/api/documents.py` router (mounted in `main.py`):
  `POST /documents` reads the upload, rejects an empty file with `400` *before* any write,
  saves the blob (M1), inserts a `documents` row `pending` filling `byte_size`/`checksum`
  (sha256)/`content_type` — the columns M6 left to the row's creator — commits so the row is
  durable, then kicks `orchestrate(doc.id)` via FastAPI **`BackgroundTasks`** and returns
  **202 + doc id**. `GET /documents/{id}` is the status poll (`404` if unknown). The
  `BackgroundTasks` kick is the deliberate Celery seam — `.add_task(orchestrate, id)` →
  `orchestrate_task.delay(id)` is the only change when the worker lands, endpoint + client
  contract unchanged. Architect chose `BackgroundTasks` over inline `await` (would block the
  202 on a tens-of-seconds hi_res parse) and over `asyncio.create_task` (fire-and-forget,
  droppable on shutdown). **Test = 4 immutable** (`test_upload.py`): drives the ASGI app with
  `httpx.AsyncClient` + `ASGITransport` (which runs background tasks as part of the request,
  so the faked `orchestrate` is provably called by the time POST returns) over the real
  testcontainers Postgres via the committing `session_factory` + a `get_session` dependency
  override; `orchestrate` and `get_storage` are faked (no pipeline, no Gemini). Cases:
  happy-path 202 + full row + blob + job kicked; empty upload → 400, no row, no job; poll
  known → 200 + status; poll unknown → 404. New dep `python-multipart==0.0.20` (FastAPI needs
  it for `UploadFile`; not pulled by the bare `fastapi` install) and `fastapi.File` added to
  the ruff B008 immutable-calls whitelist (same FastAPI DI idiom as `Depends`). Suite now
  **30 passed, 2 deselected (live), 0 skipped** (was 26+2); all touched files ruff+black clean.
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
