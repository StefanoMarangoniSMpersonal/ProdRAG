# Session Handoff — resume here next time

_Last updated: 2026-07-10. **Phase 1's ingestion PATH is complete** — a file now travels
from an HTTP upload all the way to retrievable chunks (M0–M7). Suite = **30 pass + 2
deselected (live)**. **M0–M7 are all committed & pushed** (`main` in sync with
`origin/main`); two small tooling files stay uncommitted by choice (see Git state)._

_This file keeps only **what's live + what's next**. Settled detail lives in the code,
`docs/PROGRESS.md` (layer-by-layer % tracker), and auto-memory (`phase1-ingestion.md`,
loaded every session). Read this + `CLAUDE.md` (the constitution) to pick up._

## Where we are

- **Done & verified:** Phase 0 spine (Next.js → FastAPI → Postgres/pgvector → back), and
  Phase 1 **M0–M7** (schema → storage → parse → chunk → embed → write → orchestrator →
  upload endpoint). Suite = **30 offline pass + 2 deselected live** (the 2 live tests —
  `test_embed_live_real_gemini` + `test_orchestrate_..._live` — both pass against real
  Gemini; deselected by default, not skipped). Per-milestone detail: `PROGRESS.md` +
  memory `phase1-ingestion`.
- **The pipeline is proven live (2026-07-10):** `test_orchestrate_..._live` ran the whole
  thing against real Gemini on the Aurelia fixture — 16 chunks, doc `ready`, ~8.2 s. The
  only un-exercised path is the **HTTP endpoint** end to end (M7's unit tests fake
  `orchestrate`); an optional curl smoke is in "Suggested first moves".
- **Next up — architect's call between two live options:**
  - **(a) Celery + Redis async layer** — turn the M7 `BackgroundTasks` kick into a real
    worker (`.add_task(orchestrate, id)` → `orchestrate_task.delay(id)`, the one-line seam
    M7 was built around), plus a reaper for stuck `processing` rows (the M6-deferred item).
    The "make it production-shaped" work.
  - **(b) Phase 2 retrieval** — the query pipeline reads M7's embedded chunks: embed query
    (`as_retrieval_query`, already built) → hybrid pgvector + full-text → RRF →
    cross-encoder rerank → generate → guard. The higher-learning RAG-core work.
- **Test-first & immutable is a hard rule:** write the failing test first; once written a
  test is immutable — fix the code, never the test; if the spec is wrong, stop and ask.
  Full text in `CLAUDE.md`; memory `testing-test-first-immutable`.

## How to run & test (assume nothing is running in a fresh session)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts all 3 pieces
.\stop.ps1    # stops Postgres/Redis containers  (.\stop.ps1 -Wipe drops the pgdata volume)
```
`dev.ps1` opens the API (:8000) and frontend (:3000) each in its own window; verify with
`curl http://localhost:8000/health/db`. Manual steps + troubleshooting in `README.md`.

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
  is up but **unwired** (Celery arrives with async ingestion).
- **`/backend`** — FastAPI (pip + venv). `GET /health` + `/health/db`; the **M7 ingestion
  API** in `app/api/documents.py` (`POST /documents`, `GET /documents/{id}`), mounted in
  `app/main.py`. `app/db.py` (SQLAlchemy 2.x + asyncpg), `app/config.py`, `app/models.py`
  (`Document` + `Chunk`), `app/storage/` (M1 seam), `app/ingest/` (`parse` M2, `chunk` M3,
  `embed` M4, `write` M5, `orchestrate` M6, `inspect` the `/ingest-inspect` CLI). Deps:
  `unstructured[md,pdf]==0.23.1`, `pgvector`, `google-genai==2.10.0`,
  `python-multipart==0.0.20` (M7 `UploadFile`); `testcontainers[postgres]==4.14.2` in
  `requirements-dev.txt`. `tests/conftest.py` = the testcontainers harness (session
  container + rolled-back per-test `db_session` + committing `session_factory`).
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4); fetches `/health/db`, builds clean.
- **Tooling** — `dev.ps1` / `stop.ps1`; `README.md` (GitHub front page); `.gitignore`
  hardened (`backend/.env` git-ignored).

## Git state

- Remote **github.com/StefanoMarangoniSMpersonal/ProdRAG**, branch `main`. **`main` is in
  sync with `origin/main`** — M0–M7 all committed & pushed. Recent: `1a52ade` (M4) →
  `d9f4fd6` (M5) → `aeaacfc` (M6) → `232564a` (M4 batch fix) → **`e78ea6b` (M7 upload
  endpoint, pushed 2026-07-10)**. No `Co-Authored-By` trailer (user preference).
- **UNCOMMITTED (left out of the M7 commit by choice — a later/separate commit):**
  `backend/pyproject.toml` (`fastapi.File` in the ruff B008 whitelist) and
  **`infra/db/explore.sql`** (new, untracked — DB browsing queries). ⚠ Until `pyproject.toml`
  lands, `ruff check backend` flags B008 on `documents.py`'s `File(...)` default.
  **Git-ignored, will NOT commit:** `.vscode/settings.json` (SQLTools connection).
- Also untracked on purpose: `backend/tests/fixtures/Proactive Autoscaling.pdf` (a personal
  corpus doc no test references).
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
- **The Celery seam.** `background_tasks.add_task(orchestrate, doc.id)` in
  `app/api/documents.py` → `orchestrate_task.delay(doc.id)` is the *only* change when the
  worker lands; endpoint + client contract unchanged. `orchestrate(document_id)` already
  takes only an id and returns an `IngestResult` no caller waits on. Watch (no data reshape):
  `database_url` may split into pooled-worker + session URLs once Celery+RLS land. Deferred:
  a stuck-`processing` reaper (no reaper yet — arrives with Celery).
- **The deferred enrich seam.** Table/image chunks will embed an LLM summary via the
  per-chunk **`embed_text`** (defaults to `content`, so filling it never reshapes M4/M5);
  raw stays as `content` for display/citation. Eval-gated; needs a generation LLM client.

## Decisions still constraining upcoming work (full text: CLAUDE.md + memory)

- **Phase 1 = sync-first, local disk, local Unstructured, real `gemini-embedding-2` @768d.**
  Deferred with no rework: Celery/Redis, S3, keyword/BM25 index, RLS enforcement,
  observability, the whole Phase-2 query pipeline.
- **Ingestion is JOB-SHAPED, not request-shaped** — the guardrail that makes "Celery later =
  no rework" true (memory `phase1-ingestion`).
- **Eval is a substrate, not a phase** — ingestion-side metrics (`IngestResult.timings_ms` +
  `ingest.*` logs) are live now; the RAGAS harness + per-query log switch on in Phase 2.
- **Schema:** hand-written numbered SQL + hand-mirrored models, no Alembic (memory
  `schema-migrations-convention`). **Auth:** Supabase Auth, JWKS/ES256 + RLS (memory
  `auth-supabase`). **`owner_id` NOT NULL, dev-user default; RLS becomes a policy change later.**

## Suggested first moves next session

1. **Commit + push the working-tree delta** (one commit, no `Co-Authored-By`): the M7 files +
   `infra/db/explore.sql` + these doc refreshes (full list in Git state). Optional follow-up:
   a formatting-only commit for the `test_parse.py` `black` debt (see Open items).
2. **Optional HTTP smoke** (the one un-exercised path): `.\dev.ps1`, then
   `curl -F "file=@backend/tests/fixtures/rag_test_document.md" http://localhost:8000/documents`
   → 202 + id; poll `curl http://localhost:8000/documents/<id>` until `status` = `ready`, then
   watch the rows via `infra/db/explore.sql`. Needs `GEMINI_API_KEY` in `backend/.env` and the
   docker-compose Postgres up (not the testcontainer).
3. **Pick the next milestone (architect's call):** the **Celery + Redis** async layer or
   **Phase 2 retrieval** — see "Where we are → Next up".
4. **Naive-first but still test-first** — the red/green net exists before the naive code. Slow
   down and teach on the RAG core.
