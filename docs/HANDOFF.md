# Session Handoff — resume here next time

_Last updated: 2026-07-10. **Phase 1's ingestion PATH is complete AND now production-shaped:**
a file travels from an HTTP upload all the way to retrievable chunks (M0–M7), and that job
now runs on a **real Celery + Redis worker** with a **fenced stuck-job reaper**. Suite =
**35 pass + 2 deselected (live)**. M0–M7 + housekeeping are committed & pushed (`main` at
`8fa0bd5`, in sync with `origin/main`); the **async layer is built & green but not yet
committed** (see Git state)._

_This file keeps only **what's live + what's next**. Settled detail lives in the code,
`docs/PROGRESS.md` (layer-by-layer % tracker), and auto-memory (`phase1-ingestion.md`,
loaded every session). Read this + `CLAUDE.md` (the constitution) to pick up._

## Where we are

- **Done & verified:** Phase 0 spine (Next.js → FastAPI → Postgres/pgvector → back),
  Phase 1 **M0–M7** (schema → storage → parse → chunk → embed → write → orchestrator →
  upload endpoint), and the **async layer** (Celery worker + Redis broker + fenced reaper).
  Suite = **35 offline pass + 2 deselected live** (the 2 live tests —
  `test_embed_live_real_gemini` + `test_orchestrate_..._live` — both pass against real
  Gemini; deselected by default, not skipped). Per-milestone detail: `PROGRESS.md` +
  memory `phase1-ingestion`.
- **The pipeline is proven live (2026-07-10):** `test_orchestrate_..._live` ran the whole
  thing against real Gemini on the Aurelia fixture — 16 chunks, doc `ready`, ~8.2 s. The
  M7 upload endpoint's own tests fake the enqueue; an end-to-end HTTP-through-worker curl
  smoke is in "Suggested first moves".
- **The async layer landed this session (built, green, uncommitted).** The M7
  `BackgroundTasks` kick is now `orchestrate_task.delay(str(id))` onto a Celery worker
  consuming from the (previously idle) Redis; a Celery-Beat **reaper** recovers rows stuck
  in `processing`. What made the reaper *safe* is a new **attempt-fence** (a fencing-token /
  generation-number column `documents.attempt`): `orchestrate`'s three transactions now
  claim-and-increment the token, then gate the results/failure commits on it, so a reaper
  requeue of a presumed-dead worker can never corrupt a worker that's actually still
  writing (the superseded run writes zero chunks). Redis is **broker-only** — no result
  backend; `documents.status` stays the single source of truth. See "Seams" + memory
  `phase1-ingestion`.
- **Next up — Phase 2 retrieval** (the remaining live option; the async layer was the
  other): the query pipeline reads M7's embedded chunks — embed query (`as_retrieval_query`,
  already built) → hybrid pgvector + full-text → RRF → cross-encoder rerank → generate →
  guard. The higher-learning RAG-core work.
- **Test-first & immutable is a hard rule:** write the failing test first; once written a
  test is immutable — fix the code, never the test; if the spec is wrong, stop and ask.
  Full text in `CLAUDE.md`; memory `testing-test-first-immutable`.

## How to run & test (assume nothing is running in a fresh session)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts all 3 pieces
.\stop.ps1    # stops Postgres/Redis containers  (.\stop.ps1 -Wipe drops the pgdata volume)
```
`dev.ps1` now opens **three** windows — API (:8000), the **Celery worker + Beat/reaper**
(`celery -A app.worker worker -l info --pool=solo -B`), and frontend (:3000); verify with
`curl http://localhost:8000/health/db`. Manual steps + troubleshooting in `README.md`.
(`--pool=solo` is mandatory on Windows — Celery's default prefork pool is broken there.)

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
  `infra_pgdata`) — where a real `POST /documents` lands and what survives restarts. It
  currently holds one Aurelia ingest (1 `documents` + 16 `chunks`).
- **Inspecting the dev DB.** CLI: `docker exec -it prodrag-postgres psql -U prodrag -d
  prodrag`. GUI: **VS Code SQLTools** + PostgreSQL driver are installed (connection in
  git-ignored `.vscode/settings.json`). Ready browsing queries: **`infra/db/explore.sql`**
  (run a statement with `Ctrl+E Ctrl+E`). Note: use `vector_dims(embedding)` /
  `substring(embedding::text for N)` to peek at vectors, and `->>` not the JSONB `?`
  operator (SQLTools reads `?` as a bind param).

## Repo state (what exists)

- **`/infra`** — `docker-compose.yml`: Postgres (`pgvector/pgvector:pg16`) + Redis.
  `db/init/001_pgvector.sql` enables the extension; schema in `db/migrations/002_schema.sql`
  (applied by `apply-migrations.ps1`); `db/explore.sql` = read-only browsing queries. Redis
  is now **wired** — it's the Celery broker for the ingestion worker (`app/worker.py`).
- **`/backend`** — FastAPI (pip + venv). `GET /health` + `/health/db`; the **M7 ingestion
  API** in `app/api/documents.py` (`POST /documents`, `GET /documents/{id}`), mounted in
  `app/main.py`. `app/db.py` (SQLAlchemy 2.x + asyncpg), `app/config.py`, `app/models.py`
  (`Document` + `Chunk`), `app/storage/` (M1 seam), `app/ingest/` (`parse` M2, `chunk` M3,
  `embed` M4, `write` M5, `orchestrate` M6, `inspect` the `/ingest-inspect` CLI), and
  **`app/worker.py`** — the Celery app (`orchestrate_task` bridges the sync task to the
  async `orchestrate` via `asyncio.run`; `reap_stuck_documents` is the Beat reaper). Deps:
  `unstructured[md,pdf]==0.23.1`, `pgvector`, `google-genai==2.10.0`,
  `python-multipart==0.0.20` (M7 `UploadFile`), **`celery[redis]==5.6.3`** (async layer);
  `testcontainers[postgres]==4.14.2` in `requirements-dev.txt`. `tests/conftest.py` = the
  testcontainers harness (session container + rolled-back per-test `db_session` + committing
  `session_factory`).
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4); fetches `/health/db`, builds clean.
- **Tooling** — `dev.ps1` / `stop.ps1`; `README.md` (GitHub front page); `.gitignore`
  hardened (`backend/.env` git-ignored).

## Git state

- Remote **github.com/StefanoMarangoniSMpersonal/ProdRAG**, branch `main`. **`main` is in
  sync with `origin/main`** at **`8fa0bd5`** — M0–M7 + housekeeping all committed & pushed.
  Recent: `aeaacfc` (M6) → `232564a` (M4 batch fix) → `e78ea6b` (M7 upload endpoint) →
  **`8fa0bd5` (housekeeping: ruff `File` whitelist, `infra/db/explore.sql`, doc refresh —
  the two previously-held-back tooling files landed here)**. No `Co-Authored-By` trailer
  (user preference). `ruff check backend` is now clean (the B008 exemption is in).
- **UNCOMMITTED — the async layer (built & green this session; next commit):**
  new `backend/app/worker.py` + `backend/tests/test_worker.py` (5 fence/bridge/reaper tests);
  `backend/tests/fixtures/Proactive Autoscaling.pdf` (see below); and edits to
  `backend/app/api/documents.py` (enqueue seam), `backend/app/ingest/orchestrate.py` (the
  three-txn attempt-fence), `backend/app/models.py` + `infra/db/migrations/002_schema.sql`
  (the `attempt` column), `backend/app/config.py` (Celery/reaper settings),
  `backend/requirements.txt` (`celery[redis]`), `backend/tests/test_upload.py` (the
  authorized faked-enqueue revision), and `dev.ps1` (worker window). **Git-ignored, will
  NOT commit:** `.vscode/settings.json` (SQLTools connection).
- **The `Proactive Autoscaling.pdf` fixture — resolved.** The immutable
  `test_parse_pdf_hi_res_infers_table_structure` references
  `tests/fixtures/Proactive Autoscaling.pdf`, but that file had only ever been *untracked*,
  so a working-tree cleanup deleted it and the test broke. It has been **restored and will
  be committed (tracked) with the async layer** so it can't vanish again — the test is green
  again (98 s hi_res run). The test's two `Falcon-9X` sanity lines stay **commented out**
  (immutable — not ours to edit). `quarterly_report.pdf` remains a committed fixture but is
  now **unused** by any test.
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
  `owner_id`, `UNIQUE(document_id, ordinal)`. The **HNSW index + `tsvector` keyword column
  are deferred** (commented in `002_schema.sql`) — both derive from stored data, so Phase 2
  adds them with **no re-ingest**.
- **Query-side embedding is already built.** `as_retrieval_query(text)` + `embed_texts`
  (`app/ingest/embed.py`) are the exact calls the retrieval step makes — role is a text
  **prefix**, `gemini-embedding-2` has no `task_type`; each input must be its own
  `types.Content` (a bare `list[str]` fuses to one vector — the 2026-07-07 batch fix).
- **The Celery seam — now realized.** The endpoint calls `orchestrate_task.delay(str(doc.id))`
  (`app/api/documents.py`); the worker (`app/worker.py`) bridges the sync Celery task to the
  async `orchestrate` with `asyncio.run`. Two gotchas the code already handles: (1) each
  `asyncio.run` makes a fresh event loop, so reusing a pooled asyncpg connection across loops
  raises "Future attached to a different loop" — the worker installs a **NullPool**
  `SessionLocal` at startup (via `worker_process_init`) onto orchestrate's monkeypatch seam;
  (2) UUIDs cross the JSON broker as `str`, re-parsed in the task. **Still deferred (no data
  reshape):** `database_url` may split into pooled-worker + session URLs once Celery+RLS land
  and we point the worker at Supavisor transaction-mode instead of a direct connection.
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

1. **Commit + push the async layer** (one commit, no `Co-Authored-By`): the full uncommitted
   set in Git state — `app/worker.py`, `test_worker.py`, the fence (`orchestrate.py`,
   `models.py`, `002_schema.sql`), the endpoint seam, `config.py`, `requirements.txt`,
   `test_upload.py`, `dev.ps1`, **and `Proactive Autoscaling.pdf` (tracked this time)** — plus
   these doc refreshes. Optional follow-up: a formatting-only commit for the `test_parse.py`
   `black` debt (see Open items).
2. **HTTP-through-worker smoke** (proves the async layer live end to end): `.\dev.ps1` (now
   also starts the worker), then
   `curl -F "file=@backend/tests/fixtures/rag_test_document.md" http://localhost:8000/documents`
   → 202 + id; watch the **worker window** log the job; poll
   `curl http://localhost:8000/documents/<id>` until `status` = `ready`, then watch the rows
   via `infra/db/explore.sql`. Needs `GEMINI_API_KEY` in `backend/.env` and the docker-compose
   Postgres + Redis up (not the testcontainer). Reaper check: shorten
   `ingest_stuck_after_seconds`, leave a `processing` row with an old `updated_at`, watch Beat
   requeue it and `attempt` increment.
3. **Next milestone — Phase 2 retrieval** (the async layer was the other option, now done):
   embed query → hybrid pgvector + full-text → RRF → cross-encoder rerank → generate → guard.
4. **Naive-first but still test-first** — the red/green net exists before the naive code. Slow
   down and teach on the RAG core.
