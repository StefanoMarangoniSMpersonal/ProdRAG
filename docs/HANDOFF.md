# Session Handoff — resume here next time

_Last updated: 2026-07-17. **Phase 1 is DONE**; **Phase 2 retrieval — Q1–Q4 COMPLETE; the semantic-only
eval baseline is RECORDED.** Q1–Q3 committed (`f53a596`/`8efed87`/`3fcc2f7`); eval corpus de-contaminated
(`10d305c`); Q4 harness pushed in `77e831b` (corpus + 48-Q `golden_questions.md`) + `e69af7a` (`/eval`
package). **This session:** the 8 corpus docs were **ingested successfully** (dev DB holds **9 docs / 97
chunks**), `build_golden` ran, `eval/golden.jsonl` was hand-reviewed and APPROVED, and the baseline
`eval.run` **ran clean over all 48 questions** — the first result JSON is
`eval/results/retrieval-20260717T124400_884165Z.json`:_

> **MRR 0.881 · hit@1 0.792 · hit@3 0.979 · hit@5 0.979 · hit@10 1.000** (48 Q, 9 docs / 97 chunks,
> semantic-only). Recall is already maxed at k=10 (every relevant chunk always lands in the top 10); the
> entire gap is **top-rank precision** (hit@1), exactly what Q5 (lexical) / Q6 (RRF) / Q7 (rerank) exist to
> close. This is the ruler each later stage's delta is measured against.

_**The worker cross-loop fix is now COMMITTED (`6730c34`) and live-proven** — the first *multi-document*
worker run had crashed with "Event loop is closed" (the google-genai embed client's httpx keep-alive pool
reused across the fresh-per-task loops `asyncio.run` creates); Option C runs every task on **one persistent
event loop** per process (see below), and this session's successful 9-doc ingest is its live proof.
Verified offline too (worker+embed suites green, ruff+black clean). Offline suite = **63 pass + 3 deselected
(live), 0 skip**. `main` was at `e69af7a`; the worker fix landed as `6730c34`, and the baseline artifacts
(`eval/golden.jsonl` + the result JSON + these doc refreshes + a 2-line `semantic.py` comment) land in the
next commit. **Next = Q5** (lexical retrieval). Approved Q1–Q10 breakdown: `docs/PHASE2-PLAN.md`._

_This file keeps only **what's live + what's next**. Settled detail lives in the code,
`docs/PROGRESS.md` (layer-by-layer % tracker), and auto-memory (`phase1-ingestion.md`,
loaded every session). Read this + `CLAUDE.md` (the constitution) to pick up._

## Where we are

- **Done & verified:** Phase 0 spine (Next.js → FastAPI → Postgres/pgvector → back),
  Phase 1 **M0–M7** (schema → storage → parse → chunk → embed → write → orchestrator →
  upload endpoint), the **async layer** (Celery worker + Redis broker + fenced reaper), and
  **Phase 2 Q1–Q4** (indexes → semantic → retrieve() → eval harness). Offline suite = **63 pass +
  3 deselected live** (35 through M8 + 3 Q1 + 4 Q2 + 4 Q3 + 12 Q4 + 5 build_golden; the 3 live tests —
  `test_embed_live_real_gemini`, `test_orchestrate_..._live`, `test_harness_runs_live_end_to_end` —
  hit real Gemini/dev DB; deselected by default, not skipped). Per-milestone detail:
  `PROGRESS.md` + memory `phase1-ingestion` / `phase2-retrieval`.
- **The pipeline is proven live (2026-07-10):** `test_orchestrate_..._live` ran the whole
  thing against real Gemini on the Aurelia fixture — 16 chunks, doc `ready`, ~8.2 s.
- **The async layer is now proven live end to end (2026-07-11):** the HTTP-through-worker
  smoke test ran for the first time — `POST /documents` with `rag_test_document.md` →
  `202 + id` → Celery worker picked the job off Redis → `pending → processing → ready`,
  and the dev DB confirmed **16 chunks · 768-dim vectors · `attempt=1`** (the clean
  single-worker fence path). This closes the last unexercised seam of M8; the enqueue →
  broker → worker → NullPool-session → commit path all held.
- **The async layer landed this session (committed & pushed, `729e651`).** The M7
  `BackgroundTasks` kick is now `orchestrate_task.delay(str(id))` onto a Celery worker
  consuming from the (previously idle) Redis; a Celery-Beat **reaper** recovers rows stuck
  in `processing`. What made the reaper *safe* is a new **attempt-fence** (a fencing-token /
  generation-number column `documents.attempt`): `orchestrate`'s three transactions now
  claim-and-increment the token, then gate the results/failure commits on it, so a reaper
  requeue of a presumed-dead worker can never corrupt a worker that's actually still
  writing (the superseded run writes zero chunks). Redis is **broker-only** — no result
  backend; `documents.status` stays the single source of truth. See "Seams" + memory
  `phase1-ingestion`.
- **✅ Worker cross-loop bug fixed & COMMITTED 2026-07-17 (`6730c34`), now live-proven.**
  The bridge originally ran each task under its own `asyncio.run` — a *fresh event loop per
  task*. That's safe for asyncpg (NullPool dodges it) but NOT for the google-genai embed
  client: its httpx **keep-alive connection pool** is cached once per process (`embed._get_client`
  is `@lru_cache`d) and its connections bind to the loop that opened them. On the first
  *multi-document* worker run (every prior smoke ingested a single doc, so loops never crossed),
  task N reused a connection opened under task N-1's now-closed loop → `RuntimeError: Event loop
  is closed` during connection teardown, and ~half the corpus docs failed. **Fix (Option C, my
  recommendation, architect-approved):** the worker creates **one persistent event loop** at
  `worker_process_init` and runs every task on it via a `_run(coro)` helper
  (`loop.run_until_complete`), so both the asyncpg and httpx pools open/close on the same live
  loop. `_run` falls back to `asyncio.run` when `_worker_loop` is None (eager-mode tests, where
  `worker_process_init` never fires) — which keeps the immutable bridge test green. NullPool
  retained (harmless belt-and-braces). `worker.py` only; `embed.py` untouched (the immutable
  embed tests pin `_get_client` and their fake has no `aclose`, so the fix could NOT live in
  `embed_texts`). Green offline: `test_worker.py` + `test_embed.py` = 10 pass / 1 live deselected;
  ruff + black clean. **Live proof:** this session's multi-doc corpus ingest (all 8 `eval/corpus/`
  docs → 9 docs in the dev DB, no "Event loop is closed") ran on the fixed worker.
- **Phase 2 retrieval — IN PROGRESS. Q1–Q4 COMPLETE (baseline recorded 2026-07-17); next = Q5.** The query
  pipeline reads M7's embedded chunks — embed query (`as_retrieval_query`, already built) → hybrid
  pgvector + full-text → RRF → cross-encoder rerank → generate. Full milestone breakdown (Q1–Q10)
  approved in **`docs/PHASE2-PLAN.md`**. Locked decisions: **eval is woven** (retrieval hit@k/MRR at
  Q4, full RAGAS after generation); **LangGraph wrapped later** (plain async functions first, a
  Phase-2.5 refactor); **reranker provider decided at Q7**; **Phase 2 scope = a working `POST /ask`** —
  LangGraph, Guardrails, streaming are Phase 2.5.
  - **Q1 (retrieval indexes) COMPLETE & committed (`f53a596`).** Append-only
    `infra/db/migrations/003_retrieval_indexes.sql`: `idx_chunks_embedding_hnsw` (HNSW,
    `vector_cosine_ops`), generated `tsv tsvector` column, `idx_chunks_tsv` (GIN). `Chunk.tsv`
    mapped read-only (`Computed(..., persisted=True)`; `write.py` untouched). `conftest.py` now
    globs all `*.sql` migrations (was hardcoded to `002`). Test-first `test_retrieve_schema.py` (3
    tests) + teaching note `docs/learning/Q1-indexes.md`. Applied to the dev DB in place, no
    re-ingest (verified).
  - **Q2 (naive semantic search) COMPLETE & committed.** New `app/retrieve/` package (`__init__.py`
    + `types.py` + `semantic.py`). `search_semantic(session, query_embedding, *, k,
    owner_id=DEV_OWNER_ID) -> list[ScoredChunk]`: one `select(Chunk, distance).order_by(distance)
    .limit(k)` via `Chunk.embedding.cosine_distance` (`<=>`), owner-filtered, under the
    `hnsw.ef_search` recall knob (issued per query via `set_config(...)` — plain `SET` can't take a
    bound param). **`ScoredChunk`** (frozen `slots=True`: `chunk` + `score`) lives in the shared
    `types.py` (Q5/Q7 import it too); **score = cosine similarity** (`1 - distance`, higher = more
    relevant) so all stages point the same way. `owner_id` = the visibility-predicate seam
    (generalizes to role/clearance later; deferred to auth). New config
    `retrieval_hnsw_ef_search=40`. Test-first `test_semantic.py` (4 tests: order / score direction /
    `k` truncation / owner filter) + teaching note `docs/learning/Q2-semantic-search.md`. Suite 42+2.
  - **Q3 (retrieval orchestrator + query embedding) COMPLETE & committed (`3fcc2f7`).**
    `app/retrieve/retrieve.py` — `retrieve(query: str, *, k=None, owner_id=DEV_OWNER_ID) ->
    RetrievalResult`, the first end-to-end read: embeds the query STRING via the existing seam
    `embed_texts([as_retrieval_query(q)])[0]` (RETRIEVAL_QUERY role — a text prefix, not a
    `task_type`), owns its own `SessionLocal` (the `orchestrate.py` pattern), calls the built
    `search_semantic`, returns a frozen **`RetrievalResult`** (`query`, `chunks`, `timings_ms`)
    beside `ScoredChunk` in the shared `types.py`. Collaborators are module-level monkeypatch seams.
    `k=None` resolves to **`Settings.retrieval_k=10`** inside the fn. **`explain-retrieval` wired:**
    `app/retrieve/explain.py` CLI. `test_retrieve.py` (4 tests) + note `Q3-query-embedding.md`.
  - **Q4 (retrieval eval baseline) — code COMMITTED & PUSHED (`77e831b` corpus + `e69af7a` harness); only the live baseline remains.** Repo-root **`/eval` package**
    (so `python -m eval.run` matches the `eval-run` skill): **`metrics.py`** — pure, DB-free
    `hit_at_k` / `reciprocal_rank` (1/rank-of-first) / `mrr` / `hit_rate_at_k` (empty → 0, no
    divide-by-zero); **`run.py`** — `load_golden` + `async evaluate_golden(golden, *, ks=(1,3,5,10))`
    (retrieves ONCE per Q at max-k via the `retrieve` module-seam, slices smaller cutoffs) +
    `write_report` (timestamped JSON: summary + per-query + git-sha/model/corpus-counts) + CLI
    `main(--golden --out)`; **`build_golden.py`** — the regenerator: parses the **5-column**
    `eval/golden_questions.md` (`# | Q | Source File | Category | Expected Answer`), loads dev chunks,
    normalized substring-match expected-answer → chunk.id **scoped to the answer's own Source File**,
    writes DRAFT `golden.jsonl` + flags `source-missing` / `0-match` / `>1-match` rows for human
    review (Category parsed past, not stored). **Architect decisions:** (1) relevance key =
    **generator + chunk_ids** (DERIVED — chunk ids are `BIGINT IDENTITY`, churn on re-ingest → re-run
    the builder); (2) traps **deferred to Q8** (Q4 golden = the 48 answerable Qs; 10 traps live in
    `golden_questions.md`); (3) **corpus-expansion gate SATISFIED** — 15 → 48 Q, 1 → 9 docs (8 new
    adversarial docs staged under `eval/corpus/`, on disk / not yet ingested). **Wiring:** `pytest.ini`
    `pythonpath = . backend`; `run.py`/`build_golden.py` bootstrap `sys.path` + `load_dotenv(backend/.env)`
    so the CLI runs from the repo root with `GEMINI_API_KEY`. Test-first: `test_eval_metrics.py` (13:
    pure-metric asserts + golden loader + offline harness smoke w/ fake `retrieve` + 1 live e2e) +
    `test_build_golden.py` (5: 5-column parser drops Category + skips trap table; source-scoped matcher;
    money/comma normalization; absent-source → empty) + note `docs/learning/Q4-retrieval-eval.md`.
    Offline suite **63+3**; ruff + black clean. The code is in two pushed commits (`77e831b` corpus +
    `e69af7a` harness). **✅ Baseline RECORDED 2026-07-17:** the 8 `eval/corpus/` docs were ingested
    (9 docs / 97 chunks in the dev DB), `python -m eval.build_golden` produced `eval/golden.jsonl`
    (hand-reviewed + APPROVED), and `python -m eval.run --golden eval/golden.jsonl --out eval/results/`
    ran clean over all 48 questions → `eval/results/retrieval-20260717T124400_884165Z.json`:
    **MRR 0.881 · hit@1 0.792 · hit@3 0.979 · hit@5 0.979 · hit@10 1.000** (semantic-only). Reading:
    recall is maxed at k=10 (relevant chunk always in top 10); the whole gap is top-rank precision (hit@1),
    which Q5/Q6/Q7 target — 8 of the 10 near-misses had the right chunk at rank 2, beaten by an adjacent
    sibling chunk (worst: id=40 "Thornfield acceptance rate", first-relevant at rank 9, dragging MRR).
    `golden.jsonl` + the result JSON + these doc refreshes land in the baseline commit.
- **Test-first & immutable is a hard rule:** write the failing test first; once written a
  test is immutable — fix the code, never the test; if the spec is wrong, stop and ask.
  Full text in `CLAUDE.md`; memory `testing-test-first-immutable`.

## How to run & test (assume nothing is running in a fresh session)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts every piece
.\stop.ps1    # stops Postgres/Redis containers  (.\stop.ps1 -Wipe drops the pgdata volume)
```
`dev.ps1` opens **four** windows — API (:8000), the **Celery worker**
(`celery -A app.worker worker -l info --pool=solo`), a **separate Celery Beat/reaper**
(`celery -A app.worker beat -l info`), and frontend (:3000); verify with
`curl http://localhost:8000/health/db`. Manual steps + troubleshooting in `README.md`.
Two Windows gotchas, both handled by `dev.ps1`: `--pool=solo` is mandatory (Celery's default
prefork pool is broken on Windows), and **Beat must be its own process** — embedded Beat
(`-B`) errors out with "does not work on Windows", so the worker and Beat are two windows.
Beat writes a local `celerybeat-schedule*` file in `backend/` (git-ignored).
- **Curl on Windows:** use **`curl.exe`**, not bare `curl` — in PowerShell `curl` is an alias
  for `Invoke-WebRequest`, which doesn't understand `-F`/`@file` and will error on the upload.

- **Tests:** `pytest.ini` is at the **repo root** — run from there, via the project venv
  explicitly: `backend\.venv\Scripts\python.exe -m pytest` (the ambient `python` may be
  another project's venv). A **SKIP exits 0 → treat skips as a false green** (memory
  `testing-skips-are-not-passes`).
- **Docker daemon must be up.** The write/orchestrate/upload tests spin up a throwaway
  `pgvector/pgvector:pg16` container via **testcontainers** — they ERROR (never SKIP) if
  the daemon is down. Install the dev dep once: `backend\.venv\Scripts\pip install -r
  backend\requirements-dev.txt`.
- **Two PDF `hi_res` tests need poppler + tesseract** (both installed; see README
  Prerequisites) and download the table model on first run — they ERROR loudly, never SKIP,
  if the binaries are absent. Text tests use the committed Markdown corpus
  `backend/tests/fixtures/rag_test_document.md`.
- **The 2 live tests** run with `pytest -m live` + a `GEMINI_API_KEY`. Nuance: the M4 live
  test reads the **process env** (export the key; `.env` does NOT populate it), while the
  orchestrate live test loads from `.env` — both need CWD=`backend/`.
- **Two DBs, don't confuse them.** The suite's testcontainers Postgres is **ephemeral**
  (`TRUNCATE`d per test, container destroyed on exit). The **persistent dev DB** is
  `prodrag-postgres` (docker-compose, `localhost:5432`, db/user/pass all `prodrag`, volume
  `infra_pgdata`) — where a real `POST /documents` lands and what survives restarts.
- **✅ Eval-corpus de-contamination (2026-07-14) — DB purged + cleanly re-ingested.** The
  fixture `rag_test_document.md` used to embed the "Golden Question Set" (answer key) after §18;
  ingesting it put the oracle into the retrievable store (**test-set leakage**). The fixture is
  now **stripped to §1–18** (the golden set lives in `eval/golden_questions.md`, never ingested).
  The old contaminated content (2 `documents` + 32 `chunks`, the same pre-strip file ingested
  twice — a 2026-07-10 direct-orchestrate run + a 2026-07-11 M8 Celery smoke; two rows for
  identical content is expected since **upload dedup is deferred**) was dropped via
  `DELETE FROM documents WHERE filename='rag_test_document.md';` (cascaded to chunks) and the
  stripped fixture re-ingested once through the curl flow. Right after de-contamination the dev DB
  held **1 clean `documents` row + 13 `chunks`**, with **0** chunks containing "Golden Question Set" /
  "Expected Answer" — verified. The store was safe for retrieval eval (Q4). **(Since 2026-07-17 it holds
  9 docs — the original + the 8 `eval/corpus/` docs ingested for the Q4 baseline.)**
- **Inspecting the dev DB.** CLI: `docker exec -it prodrag-postgres psql -U prodrag -d
  prodrag`. GUI: **VS Code SQLTools** + PostgreSQL driver are installed (connection in
  git-ignored `.vscode/settings.json`). Ready browsing queries: **`infra/db/explore.sql`**
  (run a statement with `Ctrl+E Ctrl+E`). Note: use `vector_dims(embedding)` /
  `substring(embedding::text for N)` to peek at vectors, and `->>` not the JSONB `?`
  operator (SQLTools reads `?` as a bind param).

## Repo state (what exists)

- **`/infra`** — `docker-compose.yml`: Postgres (`pgvector/pgvector:pg16`) + Redis.
  `db/init/001_pgvector.sql` enables the extension; schema in `db/migrations/002_schema.sql`
  **+ `003_retrieval_indexes.sql`** (Q1: HNSW + `tsv`/GIN), both applied in filename order by
  `apply-migrations.ps1`; `db/explore.sql` = read-only browsing queries. Redis is **wired** —
  the Celery broker for the ingestion worker (`app/worker.py`).
- **`/backend`** — FastAPI (pip + venv). `GET /health` + `/health/db`; the **M7 ingestion
  API** in `app/api/documents.py` (`POST /documents`, `GET /documents/{id}`), mounted in
  `app/main.py`. `app/db.py` (SQLAlchemy 2.x + asyncpg), `app/config.py`, `app/models.py`
  (`Document` + `Chunk`), `app/storage/` (M1 seam), `app/ingest/` (`parse` M2, `chunk` M3,
  `embed` M4, `write` M5, `orchestrate` M6, `inspect` the `/ingest-inspect` CLI), the
  **`app/retrieve/`** package (`types.py` = shared `ScoredChunk` + `RetrievalResult`; `semantic.py`
  = `search_semantic` (Q2); `retrieve.py` = `retrieve()` orchestrator (Q3); `explain.py` = the
  `explain-retrieval` CLI (Q3)), and **`app/worker.py`** — the Celery app (`orchestrate_task` bridges the
  sync task to the
  async `orchestrate` via a persistent per-process event loop / `_run` helper — see the
  Celery-seam note; `reap_stuck_documents` is the Beat reaper). Deps:
  `unstructured[md,pdf]==0.23.1`, `pgvector`, `google-genai==2.10.0`,
  `python-multipart==0.0.20` (M7 `UploadFile`), **`celery[redis]==5.6.3`** (async layer);
  `testcontainers[postgres]==4.14.2` in `requirements-dev.txt`. `tests/conftest.py` = the
  testcontainers harness (session container + rolled-back per-test `db_session` + committing
  `session_factory`); as of Q1 it **globs & applies every `infra/db/migrations/*.sql`** in order
  (was hardcoded to `002`), so the test schema matches what `apply-migrations.ps1` builds.
- **`/eval`** — retrieval eval package (repo root, so `python -m eval.run` matches the `eval-run`
  skill). `metrics.py` (pure hit@k/MRR), `run.py` (harness + CLI over the real `retrieve()`),
  `build_golden.py` (the source-scoped golden.jsonl regenerator), `golden_questions.md` (the
  never-ingested oracle — 5-column table, 48 answerable Q&A + 10 trap Qs), `corpus/` (the 8 new
  adversarial docs, now **ingested**), `results/` (baseline result JSONs accumulate here).
  **`golden.jsonl` EXISTS + is hand-approved, and the first baseline
  `results/retrieval-20260717T124400_884165Z.json` is recorded** (2026-07-17) — both land in the
  baseline commit.
- **`/docs`** — decisions/notes; `PROGRESS.md` (layer tracker), `PHASE2-PLAN.md` (approved Q1–Q10),
  and **`docs/learning/`** (per-milestone teaching notes; holds `Q1-indexes.md`,
  `Q2-semantic-search.md`, `Q3-query-embedding.md`, `Q4-retrieval-eval.md`).
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4); fetches `/health/db`, builds clean.
- **Tooling** — `dev.ps1` / `stop.ps1`; `README.md` (GitHub front page); `.gitignore`
  hardened (`backend/.env` git-ignored).

## Git state

- Remote **github.com/StefanoMarangoniSMpersonal/ProdRAG**, branch `main`. Recent: `557d71e` (changelog
  extraction) → **`3fcc2f7` (Phase 2 Q3)** → `10d305c` (eval de-contamination) → **`77e831b` (expand
  eval corpus: 8 docs + 48-Q golden set)** → **`e69af7a` (Phase 2 Q4: eval harness)** → **`6730c34`
  (fix: worker one-persistent-loop-per-process)**. **Q4's code landed in two commits, both pushed:**
  `77e831b` = the corpus data (8 new docs under `eval/corpus/`, the 5-column `eval/golden_questions.md`,
  deletion of the old `eval/golden_source.md`); `e69af7a` = the harness (`eval/` package — `metrics.py`,
  `run.py`, source-scoped `build_golden.py`, `__init__.py`, `results/.gitkeep`),
  `backend/tests/test_eval_metrics.py` + `test_build_golden.py`, `pytest.ini` (the `pythonpath` line),
  `docs/learning/Q4-retrieval-eval.md`, and the `PROGRESS.md`/`PHASE2-PLAN.md`/`CHANGELOG.md`/`HANDOFF.md`
  refreshes. **`6730c34`** = the Option C worker persistent-loop fix (`backend/app/worker.py` only),
  live-proven by this session's 9-doc ingest. **Committed locally, not yet pushed:** `6730c34` + the
  baseline commit below. **The baseline commit (about to land) holds:** `eval/golden.jsonl` (built +
  hand-approved this session), `eval/results/retrieval-20260717T124400_884165Z.json` (the recorded
  baseline), these `PROGRESS.md`/`HANDOFF.md` refreshes, and a 2-line Q2 learning-comment in
  `backend/app/retrieve/semantic.py` (folded in). No `Co-Authored-By` trailer (user preference).
  `ruff` + `black` clean.
- **Git-ignored, never committed:** `.vscode/settings.json` (SQLTools connection **+
  `python.analysis.extraPaths=[".","backend"]`** so Pylance resolves the runtime-bootstrapped
  `app.*`/`eval.*` imports — without it the editor false-flags them as unresolved; pytest is
  unaffected, it reads `pythonpath` from `pytest.ini`); `backend/celerybeat-schedule*` (Beat's
  local schedule DB).
- **The `Proactive Autoscaling.pdf` fixture — resolved.** The immutable
  `test_parse_pdf_hi_res_infers_table_structure` references
  `tests/fixtures/Proactive Autoscaling.pdf`, but that file had only ever been *untracked*,
  so a working-tree cleanup deleted it and the test broke. It is now **restored and committed
  (tracked) in `729e651`** so it can't vanish again — the test is green (98 s hi_res run). The
  test's two `Falcon-9X` sanity lines stay **commented out** (immutable — not ours to edit).
  `quarterly_report.pdf` remains a committed fixture but is now **unused** by any test.
- **Credentials:** none in the repo. A PAT used for an earlier push was exposed in chat —
  **revoke it**; use `gh auth login` or SSH next time.

## Open items

- **Pre-existing lint debt (all `app/` source clean).** One **immutable** file remains not
  `black`-clean: `tests/test_parse.py` (two commented-out lines). Left untouched on purpose
  (reformatting an immutable spec is a deliberate call) — worth a separate formatting-only
  commit.

## Seams the next step reads

_Full milestone shapes + rationale + Windows/Unstructured gotchas are in memory
`phase1-ingestion` and the code; only the live seams the next work touches are here._

- **`chunks` schema (what retrieval reads).** `chunks` (bigint-identity PK, `document_id`
  uuid FK): `content`, `embedding vector(768)`, `char_count`, `token_count`, `element_type`,
  `section_title`, `page_number`, `metadata jsonb` (holds a table's `text_as_html`),
  `owner_id`, `UNIQUE(document_id, ordinal)`. **As of Q1 the HNSW index + `tsv` `tsvector`
  column (+ GIN index) are LIVE** (migration `003`; `Chunk.tsv` mapped read-only). `tsv` is
  `GENERATED ALWAYS AS (to_tsvector('english', content)) STORED` — the DB owns it, never written
  by the app. So Q2 semantic search reads `embedding` via `cosine_distance` (`<=>`), and Q5
  lexical search reads `tsv` via `@@ websearch_to_tsquery`.
- **Query-side embedding is already built.** `as_retrieval_query(text)` + `embed_texts`
  (`app/ingest/embed.py`) are the exact calls the retrieval step makes — role is a text
  **prefix**, `gemini-embedding-2` has no `task_type`; each input must be its own
  `types.Content` (a bare `list[str]` fuses to one vector — the 2026-07-07 batch fix).
- **The Celery seam — now realized (persistent-loop bridge as of 2026-07-17).** The endpoint
  calls `orchestrate_task.delay(str(doc.id))` (`app/api/documents.py`); the worker
  (`app/worker.py`) bridges the sync Celery task to the async `orchestrate`. The bridge runs
  every task on **one persistent event loop** per worker process — created in
  `worker_process_init`, reused via the `_run(coro)` helper (`loop.run_until_complete`), with an
  `asyncio.run` fallback for eager-mode tests. This replaced the original `asyncio.run`-per-task
  bridge, which created a fresh loop each task and broke pooled connections that outlived their
  loop (see the cross-loop bug bullet under "Where we are"). Gotchas the code handles: (1) both
  the asyncpg pool (a **NullPool** `SessionLocal` installed at startup onto orchestrate's
  monkeypatch seam) and the google-genai httpx pool now bind to the single persistent loop, so
  neither is used across a closed loop; (2) UUIDs cross the JSON broker as `str`, re-parsed in
  the task. **Still deferred (no data reshape):** `database_url` may split into pooled-worker +
  session URLs once Celery+RLS land and we point the worker at Supavisor transaction-mode.
- **The reaper + attempt-fence — now built.** `documents.attempt` is a fencing token bumped
  once per claim; `orchestrate` gates its results/failure commits on it, so the Beat reaper
  (`reap_stuck_documents`, every `ingest_reaper_interval_seconds`) can flip a stale
  `processing` row back to `pending` and requeue it without a false requeue corrupting a
  still-live worker (the superseded run writes nothing). Past `ingest_max_processing_attempts`
  the reaper marks the doc `failed` (poison-pill cap). `write.py` (M5) was left untouched —
  the fence guarantees the winner always writes into an empty chunk set (fence-only
  idempotency; delete-then-insert would only be needed if we ever re-process an existing
  `document_id`).
- **The deferred enrich seam.** Table/image chunks will embed an LLM summary via the
  per-chunk **`embed_text`** (defaults to `content`, so filling it never reshapes M4/M5);
  raw stays as `content` for display/citation. Eval-gated; needs a generation LLM client.

## Decisions still constraining upcoming work (full text: CLAUDE.md + memory)

- **Phase 1 = local disk, local Unstructured, real `gemini-embedding-2` @768d — and now
  async (Celery/Redis) instead of sync.** Still deferred with no rework: S3, keyword/BM25
  index, RLS enforcement, observability, the whole Phase-2 query pipeline.
- **Ingestion is JOB-SHAPED, not request-shaped** — the guardrail that makes "Celery later =
  no rework" true (memory `phase1-ingestion`).
- **Eval is a substrate, not a phase** — ingestion-side metrics (`IngestResult.timings_ms` +
  `ingest.*` logs) are live now; the RAGAS harness + per-query log switch on in Phase 2.
- **Schema:** hand-written numbered SQL + hand-mirrored models, no Alembic (memory
  `schema-migrations-convention`). **Auth:** Supabase Auth, JWKS/ES256 + RLS (memory
  `auth-supabase`). **`owner_id` NOT NULL, dev-user default; RLS becomes a policy change later.**

## Suggested first moves next session

0. **✅ Q4 baseline DONE — nothing left here.** The 8 corpus docs are ingested (9 docs / 97 chunks),
   `eval/golden.jsonl` is approved, and the semantic-only baseline is recorded in
   `eval/results/retrieval-20260717T124400_884165Z.json` (MRR 0.881 · hit@1 0.792 · hit@10 1.000). The
   worker fix is committed (`6730c34`); the baseline artifacts commit is landing now. **⚠ Still DO NOT
   re-ingest and DO NOT wipe** — `golden.jsonl` is keyed to those exact chunk-ids in the `infra_pgdata`
   volume; a re-ingest churns them and invalidates the approved golden set (you'd have to re-run
   `build_golden.py`). Re-running `eval.run` itself is safe and repeatable. To reproduce:
   `docker compose -f infra/docker-compose.yml up -d` (Postgres only — `eval.run` needs Postgres +
   `GEMINI_API_KEY`, not the API/worker/Beat), then
   `backend\.venv\Scripts\python.exe -m eval.run --golden eval/golden.jsonl --out eval/results/`
   (runs from the repo root — bootstraps `sys.path`/`.env` itself). **Also unpushed:** `6730c34` + the
   baseline commit — `git push` when ready.
1. **Q5 — lexical retrieval (next).** `app/retrieve/lexical.py` `search_lexical` via
   `websearch_to_tsquery` + `ts_rank_cd` over `chunks.tsv`, then re-run the eval and record the delta
   against the baseline above (watch **hit@1 / MRR** — hit@10 is already 1.000, so the lever is top-rank
   precision, exactly where 8/10 near-misses lost to an adjacent sibling chunk). Each milestone ships
   test-first code **plus** a teaching note `docs/learning/Qx-*.md`. Slow down and teach — this is the RAG core.
2. **Reaper still unverified live** (the M8 smoke test proved the happy path, not the reaper):
   with Beat in its own `dev.ps1` window, shorten `ingest_stuck_after_seconds`, leave a
   `processing` row with an old `updated_at`, and watch Beat requeue it + `attempt` increment.
   Optional — the fence logic is under `test_worker.py`, so this is a live-confidence check.
3. **Optional cleanup:** a formatting-only commit for the `test_parse.py` `black` debt (see
   Open items).
4. **Naive-first but still test-first** — the red/green net exists before the naive code.

To re-run the ingestion smoke test: `.\dev.ps1`, then (note **`curl.exe`**, not `curl`):
`curl.exe -F "file=@backend/tests/fixtures/rag_test_document.md" http://localhost:8000/documents`
→ 202 + id; poll `curl.exe http://localhost:8000/documents/<id>` until `ready`; inspect via
`infra/db/explore.sql`. Needs `GEMINI_API_KEY` in `backend/.env` + docker-compose Postgres +
Redis up (not the testcontainer). **Careful:** a fresh upload adds another `rag_test_document.md`
row (dedup deferred) — if you want the golden `chunk_id`s to stay valid, don't re-ingest without
re-running `build_golden.py`.
