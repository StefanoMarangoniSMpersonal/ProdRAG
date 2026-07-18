# Project Development Tracker

> A living, layer-by-layer map of how far ProdRAG is built. Update the scorecard and the
> milestone tables as milestones land — don't re-derive from scratch each time.
> Companion to `docs/CHANGELOG.md` (append-only history), `docs/HANDOFF.md` (session
> state), and `CLAUDE.md` (the constitution).
>
> _Last updated: 2026-07-18 — **Phase 2 retrieval: Q1–Q7 COMPLETE; cross-encoder rerank landed and its
> eval delta RECORDED.** Q7 added `app/retrieve/rerank.py` — a **local** cross-encoder
> (`cross-encoder/ms-marco-MiniLM-L-6-v2`) loaded through the **already-installed** `transformers`
> (torch is present via `unstructured[pdf]`) → **zero new pip deps**, ~90 MB one-time model download.
> `retrieve()` gained a gated pool-widen → rerank → truncate branch (fetch `retrieval_candidate_k`=50, RRF
> fuse, cross-encoder re-scores, top-`k` out); **gated OFF by default** so rerank-off is byte-identical to
> Q6 and the immutable Q3/fuse specs stayed green untouched. Eval (rerank ON,
> `eval/results/retrieval-20260718T192246_887014Z.json`): **MRR 0.927 · hit@1 0.896 · hit@3 0.938 · hit@5
> 1.000 · hit@10 1.000** vs the Q6 hybrid baseline (0.895 / 0.812 / 0.979 / 0.979 / 1.000): **+0.032 MRR,
> +0.084 hit@1** (5 Q promoted to rank 1), hit@5 perfected — **accepted a −0.041 hit@3 regression** (2 Q
> dropped from top-3: id 22 rank 1→4, id 47) as a net win on the target lever (architect decision). Provider
> ADR: `docs/adr/0001-reranker-provider.md`; note: `docs/learning/Q7-rerank.md`. Offline suite = **81 pass +
> 3 deselected (live), 0 skip**; ruff + black clean. Q1–Q6 committed through `716c4a2`; **Q7 uncommitted at
> time of writing.** **Next = Q8** (grounded generation + citations — carries a generation-model ADR)._

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
| 5 | Query / retrieval pipeline | embed → hybrid search → RRF → rerank → generate → guard | ~50% | **~58%** |
| 6 | Data & storage | Postgres+pgvector, schema, blob store | ~65% | **~76%** |
| 7 | AI / ML | Gemini LLM, embeddings, reranker, Guardrails, LangGraph | ~20% | **~35%** |
| 8 | Auth & isolation | Supabase JWT (ES256/JWKS) + RLS | ~5% | **~30%** |
| 9 | Deployment / Infra | Docker local + one-time Fargate | ~30% | **~40%** |
| 10 | Observability & Evaluation | Sentry, LangSmith, structured logs, RAGAS golden set | ~15% | **~28%** |

**Weighted overall: ~32–34% (code-only) · ~49% (code + design).**

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
   Celery app, `orchestrate_task` (a sync task bridging to async `orchestrate`), and
   `reap_stuck_documents` (the Beat reaper). Broker-only: no result backend —
   `documents.status` in Postgres is the source of truth. The **attempt-fence**
   (`documents.attempt`, gated in orchestrate's three txns) makes the reaper safe. **Proven live
   2026-07-11** — the HTTP-through-worker smoke test ran a real upload through Redis → worker →
   `ready` (16 chunks, `attempt=1`). ✅ **Cross-loop bug fixed & COMMITTED 2026-07-17 (`6730c34`),
   now live-proven.** The bridge originally ran each task under its own `asyncio.run` (fresh event loop
   per task) + a NullPool session factory to dodge the asyncpg cross-loop trap — but the `google-genai`
   embed client's httpx keep-alive pool has the *same* trap, and it surfaced on the first
   *multi-document* worker run (single-doc smokes never crossed loops): "Event loop is closed"
   tearing down a connection opened under a now-closed loop. Fix (Option C): the worker runs
   every task on **one persistent event loop** per process (created in `worker_process_init`,
   reused via `_run`; `asyncio.run` fallback in eager tests), so every pooled connection
   (asyncpg *and* httpx) opens and closes on the same live loop. NullPool retained as belt-and-
   braces. Verified offline **and live** — this session's 8-doc corpus ingest (→ 9 docs in the dev DB)
   ran on the fixed worker with no crash. Remaining gap to 100%: the reaper's requeue path is still
   only unit-tested, no result introspection/Flower, and the worker still uses a direct DB connection
   (Supavisor pooled-worker URL is the documented later swap).
4. **Ingestion** — the critical path. Milestone detail below.
5. **Query pipeline** — **Phase 2 in progress (Q1–Q6 COMPLETE; hybrid baseline recorded).** Q1 landed the
   *storage substrate* (HNSW + `tsv`, see Layer 6); Q2 was the first query CODE (`search_semantic`, naive
   cosine vector search over a query *vector*); **Q3 is the first end-to-end read** —
   `app/retrieve/retrieve.py` `retrieve(query: str) → RetrievalResult` turns a query *string* into
   ranked chunks (embeds it in the RETRIEVAL_QUERY role, owns `SessionLocal`, carries per-stage timings),
   and the `app/retrieve/explain.py` CLI now backs the `explain-retrieval` skill (proven live). Q4 landed
   the **eval ruler** (`/eval`: pure hit@k/MRR metrics + `run.py` harness over the real `retrieve()` +
   `build_golden.py` regenerator) and the semantic-only baseline (2026-07-17, 48 Q / 9 docs / 97 chunks):
   MRR 0.881 · hit@1 0.792 · hit@10 1.000. **Q5 built the lexical arm** (`app/retrieve/lexical.py`
   `search_lexical` — `websearch_to_tsquery` `@@` + `ts_rank_cd` over `chunks.tsv`; standalone, MRR 0.344
   alone — weak by design, its value is *complementary*). **Q6 fused the two signals** —
   `app/retrieve/fuse.py` `reciprocal_rank_fusion` (rank-based, scale-free, `k_constant=60`) with
   `retrieve()` rewired to run both arms CONCURRENTLY (`asyncio.gather`, one session per arm) and fuse by
   RRF; the score on each `ScoredChunk` is now the RRF fused score, not cosine. **Hybrid baseline RECORDED**
   (2026-07-18, `eval/results/retrieval-20260718T140608_833817Z.json`): **MRR 0.895 · hit@1 0.812 · hit@3
   0.979 · hit@10 1.000** — **+0.014 MRR / +0.020 hit@1 over semantic, zero regression**. **Q7 then added
   the cross-encoder rerank** (`app/retrieve/rerank.py`, a local `ms-marco-MiniLM-L-6-v2` via the
   already-installed `transformers`; gated OFF by default, so rerank-off = Q6): `retrieve()` fetches a wider
   pool (`retrieval_candidate_k`=50), RRF fuses it, the cross-encoder re-scores query+passage jointly, top-`k`
   out. Eval (rerank ON): **MRR 0.927 · hit@1 0.896 · hit@5 1.000** (+0.032 MRR / +0.084 hit@1, 5 Q promoted
   to rank 1), with an **accepted −0.041 hit@3 regression** (net win on the target lever; architect decision).
   Entire pipeline shape is locked (hybrid → RRF → cross-encoder → generate); approved Q1–Q10 breakdown in
   `docs/PHASE2-PLAN.md`. **Next = Q8 (grounded generation + citations).**
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
   caught 2026-07-07 by the live end-to-end run, invisible to the offline mock. **The reranker is now
   built (Q7)** — a local cross-encoder (`ms-marco-MiniLM-L-6-v2`) run through the already-installed
   `transformers` (zero new deps), the first non-embedding ML model in the query path. LLM generation,
   Guardrails, LangGraph still unbuilt (Q8+); the generation client (Q8) reuses the `google-genai` SDK.
8. **Auth & isolation** — biggest design-vs-code gap. JWKS/ES256 verification, the RLS policy
   pattern, the Supavisor-bypasses-RLS caveat, and `owner_id NOT NULL` are all decided &
   documented; only enforcement code is missing.
9. **Deployment / Infra** — local `docker-compose` (Postgres+Redis) + `dev.ps1`/`stop.ps1` are
   real. Fargate is a deliberate one-time learning touch, not yet done (and mostly out of scope).
10. **Observability & Eval** — **retrieval eval now has code AND a recorded baseline** (Q4): the `/eval`
    package with pure hit@k/MRR metrics, the `run.py` harness that scores the real `retrieve()` and writes
    baseline result files, and the source-scoped `build_golden.py`. The golden set source (the 5-column
    `golden_questions.md`, 48 Q + 10 traps) is turned into `golden.jsonl` by the regenerator, and **two
    baselines are now recorded** in `eval/results/`: the semantic-only ruler
    (2026-07-17, `retrieval-20260717T124400_884165Z.json`: MRR 0.881 · hit@1 0.792 · hit@10 1.000) and the
    hybrid `retrieve()` after Q6 (2026-07-18, `retrieval-20260718T140608_833817Z.json`: MRR 0.895 · hit@1
    0.812 · hit@10 1.000). Still missing: RAGAS answer-eval (Q9), tracing
    (LangSmith/Sentry), the per-query query log (Q10). Logging contract + "eval as substrate" strategy
    predate this.

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
| Q1 | Schema switch-on: HNSW index + generated `tsv tsvector` + GIN index (migration `003`; `Chunk.tsv` mapped; conftest globs all migrations) | ✅ complete & verified, committed `f53a596` — `test_retrieve_schema.py`; note `docs/learning/Q1-indexes.md`; dev DB migrated in place, no re-ingest |
| Q2 | Semantic retrieval — `app/retrieve/semantic.py` `search_semantic → list[ScoredChunk]`, cosine order, `ef_search` knob | ✅ complete & verified, committed — new `app/retrieve/` pkg (`types.py` shared `ScoredChunk`; `semantic.py`); score = cosine similarity (`1 − dist`); `ef_search` via `set_config`; owner = visibility seam; `test_semantic.py` (4 tests) + `docs/learning/Q2-semantic-search.md`; suite 42+2 |
| Q3 | Retrieval orchestrator + query embedding (`retrieve() → RetrievalResult`); wire `explain-retrieval` semantic stage | ✅ complete & verified — `app/retrieve/retrieve.py` `retrieve(query, *, k=None, owner_id)` wraps query in RETRIEVAL_QUERY role → owns `SessionLocal` → `search_semantic`; `RetrievalResult` (query/chunks/timings) in shared `types.py`; `Settings.retrieval_k=10`; module seams; `explain.py` CLI backs the skill; `test_retrieve.py` (4 tests) + note `Q3-query-embedding.md`; suite 46+2; live trace ran on dev DB. Committed `3fcc2f7` (pushed) |
| Q4 | Retrieval eval baseline — `/eval` package, `golden.jsonl`, hit@k / MRR (woven eval begins) | ✅ **COMPLETE — baseline recorded 2026-07-17.** Harness pushed (`77e831b` corpus + `e69af7a` harness): `eval/metrics.py` (pure hit@k/RR/MRR), `eval/run.py` (`python -m eval.run` → hit@{1,3,5,10}+MRR, timestamped results JSON), `eval/build_golden.py` (regenerator: expected-answer→chunk_id, **source-scoped** to the answer's own file, re-run after re-ingest); traps deferred to Q8; source = 5-column `eval/golden_questions.md` (48 Q + 10 traps); `pytest.ini` `pythonpath = . backend`; `test_eval_metrics.py` (13) + `test_build_golden.py` (5); note `Q4-retrieval-eval.md`; offline suite **63+3**. **Corpus-expansion gate SATISFIED** (15→48 Q, 1→9 docs). **Live 2026-07-17:** 8 corpus docs ingested (needed the worker cross-loop fix `6730c34` — Layer 3), `eval/golden.jsonl` built + hand-approved, and `eval.run` ran clean over 48 Q → `eval/results/retrieval-20260717T124400_884165Z.json`: **MRR 0.881 · hit@1 0.792 · hit@3 0.979 · hit@5 0.979 · hit@10 1.000** (semantic-only; recall maxed at k=10, gap is hit@1). `golden.jsonl` + result JSON committed in `849452d` (do NOT re-ingest — the approved chunk-ids must stay valid) |
| Q5 | Lexical retrieval — `search_lexical` via `websearch_to_tsquery` + `ts_rank_cd` over `chunks.tsv` | ✅ complete & verified, committed `e64dfbf` — `app/retrieve/lexical.py`: `websearch_to_tsquery('english', …) @@ tsv` match (returns ONLY matches — may be <k or 0) ranked by `ts_rank_cd` (cover-density), seams mirror `semantic.py`; wired into `explain.py` side-by-side but NOT into `retrieve()` yet (that's Q6). 5 tests (suite 68+3, 0-skip) + note `docs/learning/Q5-full-text.md` (incl. how PG full-text differs from BM25). Standalone eval MRR 0.344 / hit@1 0.333, 30/48 zero-match — weak alone, value is *complementary* (paid out only when fused) |
| Q6 | Hybrid fusion — Reciprocal Rank Fusion (semantic + lexical concurrently) | ✅ complete & verified, committed `716c4a2` — `app/retrieve/fuse.py` pure `reciprocal_rank_fusion(rankings, *, k_constant=60)` (rank-based, scale-free, dedup-by-summing = cross-list agreement); `retrieve()` REWIRED hybrid: embed once + `asyncio.gather(_semantic, _lexical)` **one session per arm** + fuse + top-k, graceful-degrades to semantic when lexical empty. Score is now the RRF fused value, not cosine → **architect-authorized** revision of one immutable Q3 assert. `k_constant` wired as `Settings.retrieval_rrf_k_constant`. 7 tests (suite 75+3, 0-skip) + note `docs/learning/Q6-rrf.md`. **Hybrid eval MRR 0.895 · hit@1 0.812** (+0.020 hit@1 / +0.014 MRR vs semantic, zero regression) |
| Q7 | Cross-encoder rerank — retrieve-wide → rerank to top-k; **provider decision (ADR)** | ✅ complete & verified (uncommitted) — `app/retrieve/rerank.py`: **local** `cross-encoder/ms-marco-MiniLM-L-6-v2` via the already-installed `transformers` (**zero new deps**; ~90 MB model, lazy `@lru_cache` load, `asyncio.to_thread` off the loop, `_get_reranker` seam). `retrieve()` rewired with a **gated** pool-widen → rerank → truncate branch (`rerank_enabled` default OFF → byte-identical to Q6; `retrieval_candidate_k=50` pool; score becomes the reranker logit). `k` stays the FINAL size, so callers + the 4 immutable Q3 specs + 7 fuse specs unchanged. **ADR `docs/adr/0001-reranker-provider.md`** (rejected managed API / Gemini-as-reranker); note `docs/learning/Q7-rerank.md`. 6 tests (`test_rerank.py` 5 + `test_retrieve_rerank.py` 1); suite **81+3**, 0-skip. **Eval (rerank ON):** MRR 0.895→**0.927**, hit@1 0.812→**0.896**, hit@5 0.979→**1.000**, hit@3 0.979→0.938 (accepted 2-Q regression) — `eval/results/retrieval-20260718T192246_887014Z.json` |
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
- **M0–M8 + Phase 2 Q1–Q6 are committed** — the retrieval chain runs `716c4a2` (Q6 hybrid fusion) →
  `e64dfbf` (Q5 lexical) → `849452d` (Q4 baseline artifacts) → `6730c34` (worker persistent-loop fix) →
  `e69af7a` (Q4 harness) → `77e831b` (eval corpus: 8 docs + 48-Q golden set) → `10d305c` (de-contaminate
  eval corpus) → `3fcc2f7` (Phase 2 Q3) → `557d71e` (changelog extraction) → `8efed87` (Q2) → `f53a596`
  (Q1) → `729e651` (M8). **`main` and the `origin/main` tracking ref are in sync at `716c4a2`** (verified
  this session — the Q4 baseline / worker fix that were previously unpushed have landed). **Q5** (`e64dfbf`)
  = `backend/app/retrieve/lexical.py` + `test_lexical.py` + `explain.py` wiring + `docs/learning/Q5-full-text.md`;
  **Q6** (`716c4a2`) = `backend/app/retrieve/fuse.py` + `test_fuse.py`, `retrieve.py` rewired hybrid, the
  authorized 1-assert revision in `test_retrieve.py`, `config.py` `retrieval_rrf_k_constant`, `explain.py`
  stage restructure, `docs/learning/Q6-rrf.md`, and `eval/results/retrieval-20260718T140608_833817Z.json`
  (the hybrid baseline). **This session:** these `PROGRESS.md`/`HANDOFF.md` refreshes (docs-only; left
  uncommitted for review). (`.vscode/settings.json` — SQLTools connection + Pylance
  `python.analysis.extraPaths` for the `app.*`/`eval.*` import roots — is git-ignored.)

## Changelog

The milestone-by-milestone history moved to its own file — see **`docs/CHANGELOG.md`**
(newest first). This tracker keeps only the current scorecard, per-layer notes, and
milestone tables; append new entries to `CHANGELOG.md` as milestones land.
