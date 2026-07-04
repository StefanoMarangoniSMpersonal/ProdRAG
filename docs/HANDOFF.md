# Session Handoff — resume here next time

_Last updated: 2026-07-04 (M1 complete; locked "ingestion is job-shaped" + "build with scaling
in mind" principle). Read this + `CLAUDE.md` to pick up where we left off._
_Cross-session decisions also live in auto-memory (`MEMORY.md` is loaded every session)._

## Where we are

- **Phase 0 (stack spine): complete & verified.** A request travels the whole stack:
  Next.js → FastAPI → Postgres/pgvector → back.
- **Phase 1 · M0 (ingestion schema): complete & verified.** `documents` + `chunks` tables
  exist in the DB, mirrored by SQLAlchemy models; a round-trip + cascade test passed.
- **Phase 1 · M1 (storage interface): complete & verified.** `Storage` Protocol +
  `LocalDiskStorage` behind a clean seam (`save`/`load`/`exists`/`delete`); 6 pytest tests
  green. See "M1 storage" below.
- **Next up: Phase 1 · M2 — parse** (local Unstructured, inspect via `/ingest-inspect`).
  Then M3 chunk → M4 embed → M5 write → M6 orchestrator → M7 upload endpoint.

The repo is on GitHub (see "Git state").

## What exists and works

- **`/infra`** — `docker-compose.yml` runs Postgres (`pgvector/pgvector:pg16`) + Redis.
  `db/init/001_pgvector.sql` enables the `vector` extension on first boot. Redis is up but
  **not wired to code yet** (Celery arrives with async ingestion, later in Phase 1).
  Schema lives in **`db/migrations/002_schema.sql`**, applied by **`apply-migrations.ps1`**.
- **`/backend`** — FastAPI (pip + venv). Endpoints `GET /health` and `GET /health/db`.
  Async DB in `app/db.py` (SQLAlchemy 2.x + asyncpg), config in `app/config.py`.
  **`app/models.py`** = `Document` + `Chunk` ORM models mirroring the schema. `pgvector`
  added to `requirements.txt`. All deps verified importable; `ruff` + `black` clean.
  **`app/storage/`** = the M1 blob-store seam (`base.py` Protocol, `local.py` LocalDiskStorage,
  `__init__.py` `get_storage()`). `storage_dir` setting added to `config.py`; raw files land in
  `backend/storage/` (git-ignored).
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4). Page fetches `/health/db`, renders
  the JSON. Builds clean.
- **Tooling** — `dev.ps1` (one-command setup+start, auto-applies migrations) / `stop.ps1`.
  `README.md` rewritten as a full GitHub front page. `.gitignore` hardened (no `.env*` leaks).
- **Tests** — `pytest` + `pytest-asyncio` (in `requirements-dev.txt`). Config is **`pytest.ini`
  at the repo root** (not `backend/`), so `pytest` works from any dir. Run from the repo root;
  currently 6 storage tests. NB: a SKIP exits 0 — treat skips as a false green (memory
  `testing-skips-are-not-passes`).

## How to run (assume nothing is running in a fresh session)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts all 3 pieces
.\stop.ps1    # stops Postgres/Redis containers   (.\stop.ps1 -Wipe also drops the pgdata volume)
```
`dev.ps1` opens the API (:8000) and frontend (:3000) each in its own window. Manual steps and
troubleshooting are in `README.md`. Verify: `curl http://localhost:8000/health/db`.

## Git state

- Initialized `main`, pushed to **github.com/StefanoMarangoniSMpersonal/ProdRAG**.
- History has two commits: `7030286` (GitHub's README stub, root) then `869aaef` (our real
  Phase 0 + M0 work). Optional cleanup: squash the stub into one initial commit (needs a
  force-push → ask first).
- **No credentials stored in the repo.** Push auth was ephemeral. The PAT used was exposed in
  chat and should be revoked; use `gh auth login` or SSH next time.
- Commits: **no `Co-Authored-By` trailer** (user preference).

## M0 schema — the shape everything downstream reads/writes

`documents` (uuid PK) and `chunks` (**bigint-identity PK**, `document_id` uuid FK, cascade).
`chunks` carries: `content`, `embedding vector(768)`, `char_count`, `token_count`,
`element_type`, `section_title`, `page_number`, `metadata jsonb`, `owner_id`,
`UNIQUE(document_id, ordinal)`. HNSW index and the `tsvector` keyword column are **deferred**
(commented in the SQL) — both are derived from stored data, so they can be added later with
no re-ingest. Full rationale in `app/models.py` + memory `schema-migrations-convention`.

## M1 storage — the blob-store seam (parse reads through this)

`Storage` **Protocol** (`app/storage/base.py`) = the contract; `LocalDiskStorage`
(`app/storage/local.py`) = the local-disk impl. Methods (all async): `save(bytes, filename)
-> uri`, `load(uri) -> bytes`, `exists(uri) -> bool`, `delete(uri)`. **URI format** is
backend-qualified + relative: `file://<uuid4>/<safe-name>` (key relative to the storage root,
NOT an absolute path) — so `documents.source_uri` stays portable and S3 later is just an
`s3://<bucket>/<key>` scheme with no schema change. `delete` is **idempotent** (S3-like) and
prunes the empty uuid dir; `load`/`exists`/`delete` reject foreign-scheme and path-traversal
URIs. `get_storage()` (in `__init__.py`) is the cached accessor callers use — nobody names the
concrete class. Deferred with no rework: `S3Storage` behind the same Protocol; `byte_size` /
`checksum` / `content_type` computed by M6 from the same bytes.

## Decisions locked (this project so far)

- **Phase 1 approach:** synchronous first (no Celery yet), **local disk** for raw files (not
  S3 yet), **local Unstructured** library, **real** `gemini-embedding-2` @ 768d
  (`RETRIEVAL_DOCUMENT`). Deferred with no rework: Celery/Redis async, S3, keyword engine +
  BM25 index, RLS enforcement, observability, the whole query pipeline (Phase 2).
- **Ingestion must be JOB-SHAPED, not request-shaped** (decided 2026-07-04; this is what makes
  the "Celery later = no rework" claim actually true). M6 orchestrator = a self-contained
  coroutine keyed on `document_id`, returns nothing to a waiting caller (reads bytes via
  `get_storage().load()`, does parse→chunk→embed→write, drives `documents.status`). M7 upload
  endpoint = persist file + insert `documents` row as `pending` + kick off the job + return
  **202 + doc id**; client polls `status` until `ready`. Synchronous execution for now, but
  enqueue-and-process in shape, so Celery is a one-line swap (`await orchestrate(id)` →
  `orchestrate.delay(id)`) — NOT an endpoint/contract rewrite. The `documents.status` machine
  and `checksum` column already anticipate this. (This is the guardrail behind the CLAUDE.md
  "build with scaling in mind" principle; see memory `phase1-ingestion`.)
  - Watch-list (not blockers, don't reshape data): config's single `database_url` may grow a
    second (pooled Supavisor/transaction-mode worker URL + session URL) once Celery + RLS land;
    build M4 embedding **batched** (N chunks/request + retry/backoff) from the start — same
    shape locally and at scale, touches only the orchestrator.
- **Evaluation = a substrate, not a phase:** golden set + ingestion-side metrics start now;
  the RAGAS harness + per-query log switch on in Phase 2. The M6 orchestrator must emit
  structured metrics. (Memory: `phase1-ingestion`.)
- **Schema mechanism:** Option A — hand-written numbered SQL + hand-mirrored models; **defer
  Alembic** (adopt later via `alembic stamp`). Keep SQL ↔ models in sync by hand.
- **Keys:** UUID `documents` (exposed), bigint-identity `chunks` (internal/high-volume).
- **owner_id:** present now, NOT NULL, dev-user default; RLS becomes a policy change later.
- **Auth:** Supabase Auth (not Clerk); JWKS/ES256, RLS caveats. (Memory: `auth-supabase`.)
- **Retrieval:** own the `chunks` table + hand-write hybrid SQL (LangChain only thinly).
  The native-`tsvector`-vs-`pg_search` sub-decision is **deferred to Phase 2** (not blocking
  ingestion, since it's derived from `chunks.content`).

## Still-open decisions (owed before the deeper ingestion steps)

1. **`max_characters`** for chunking — proposed ~2000 chars (well under the ~8,192-token cap);
   confirm/tune with `/ingest-inspect` at M3.
2. **Gemini surface** — Gemini API (`google-genai`) vs Vertex; confirm exact param names for
   `task_type` / `output_dimensionality` against current docs before wiring M4. Key is
   provisioned; ensure it's in `backend/.env` (git-ignored) before M4.
3. **First test corpus + first format** — 1–2 real docs to exercise ingestion (start simple:
   `.md`/`.txt`, then a PDF). Full golden set (15–25 Q/A) can grow into Phase 2 eval.

## Suggested first moves next session

1. Build **M2 (parse)** with local Unstructured: read raw bytes back via
   `get_storage().load(uri)`, partition into typed elements, inspect via `/ingest-inspect`.
2. Settle the still-open decisions it touches: first test corpus/format, and `max_characters`
   (relevant once M3 chunking lands).
3. Keep the "naive version first, end to end, then improve" rule; slow down and teach on the
   RAG core (parse/chunk/embed).
