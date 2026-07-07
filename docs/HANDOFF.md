# Session Handoff — resume here next time

_Last updated: 2026-07-07 (M6 orchestrator landed test-first — `orchestrate(document_id)`
runs parse→chunk→embed→write end to end, drives `documents.status`, owns the commit via a
three-transaction lifecycle; `open_local` added to the storage seam so M6 gets a path, not
bytes. Then a live end-to-end run against **real Gemini** exposed & fixed an M4 batch bug —
v2 folds a raw `list[str]` into one fused vector, so `embed_texts` now sends one `Content`
per input. Suite = 26 pass + 2 deselected (live). M2+M3 **pushed**; M4 (`1a52ade`) + M5
(`d9f4fd6`) committed locally but **not yet pushed**; M6 **+ the M4 batch fix** in the
working tree, **uncommitted**)._
_Read this + `CLAUDE.md` (the constitution) to pick up. Companions: `docs/PROGRESS.md`
(layer-by-layer % tracker) and auto-memory (`MEMORY.md`, loaded every session) for
cross-session decisions. This file keeps only what's live + what's next — settled detail
lives in the code, PROGRESS.md, and memory._

## Where we are

- **Done & verified:** Phase 0 spine (Next.js → FastAPI → Postgres/pgvector → back),
  and Phase 1 **M0** (schema), **M1** (storage seam), **M2** (parse), **M3** (chunk),
  **M4** (embed), **M5** (write), **M6** (orchestrator). Suite = **26 tests** green + **2 live
  deselected** (offline: 9 storage + 3 parse + 2 chunk + 5 embed + 4 write + 3 orchestrate;
  live: `test_embed_live_real_gemini` + `test_orchestrate_..._live` — both pass against real
  Gemini). Per-milestone shapes are in "Reference" below; % in `PROGRESS.md`.
- **M4 batch contract proven against the real API (2026-07-07):** the live end-to-end run caught
  that gemini-embedding-2 reads a raw `list[str]` as one multi-part input → **one fused vector for
  the whole batch**; `embed_texts` now wraps each input in its own `types.Content`, so N texts → N
  vectors (one API call per batch). The offline mock hid this; the mock was authorized-revised to
  match. Detail in "M4 embed" Reference below.
- **Next up: Phase 1 · M7 — upload endpoint.** Persist the file (M1 `save`) + insert the
  `documents` row `pending` (M7 fills `byte_size`/`checksum`/`content_type` from the request
  bytes — M6 stays path-only) + kick the job (`await orchestrate(id)` now; `orchestrate.delay(id)`
  once Celery lands) + return **202 + doc id**; client polls `status` until `ready`. M6's
  `orchestrate` is ready and tested; M7 is the HTTP shell + `pending`-row insert on top of it.
- **Test-first & immutable is a hard rule** (governs M4 on): write the failing test first;
  once written a test is immutable — fix the code, never the test; if the spec is wrong,
  stop and ask. Full text in `CLAUDE.md`; memory `testing-test-first-immutable`.

## How to run & test (assume nothing is running in a fresh session)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts all 3 pieces
.\stop.ps1    # stops Postgres/Redis containers  (.\stop.ps1 -Wipe drops the pgdata volume)
```
`dev.ps1` opens the API (:8000) and frontend (:3000) each in its own window; verify with
`curl http://localhost:8000/health/db`. Manual steps + troubleshooting in `README.md`.

- **Tests:** `pytest.ini` is at the **repo root**, so run `pytest` from the root. **Use the
  project venv explicitly:** `backend\.venv\Scripts\python.exe -m pytest` (the shell's ambient
  `python` may be another project's venv). A **SKIP exits 0 → treat skips as a false green**
  (memory `testing-skips-are-not-passes`). Text tests use the committed Markdown corpus
  `backend/tests/fixtures/rag_test_document.md` (no binaries). The two PDF `hi_res` tests
  (`test_parse_pdf_hi_res_infers_table_structure`, `test_chunk_pdf_table_keeps_text_as_html`)
  drive `fixtures/quarterly_report.pdf` and **need poppler + tesseract** (download the table
  model on first run) — they ERROR loudly, never SKIP, if the binaries are absent.
- **M5 write tests need a running Docker daemon.** `test_write.py`'s DB tests spin up a
  throwaway `pgvector/pgvector:pg16` container via **testcontainers** (`backend/tests/
  conftest.py`), so a plain `pytest` now requires Docker Desktop up — they ERROR (never SKIP)
  if the daemon is down. Install the dev dep once: `backend\.venv\Scripts\pip install -r
  backend\requirements-dev.txt` (adds `testcontainers[postgres]`; no psycopg2 needed).

## Repo state (what exists)

- **`/infra`** — `docker-compose.yml`: Postgres (`pgvector/pgvector:pg16`) + Redis.
  `db/init/001_pgvector.sql` enables the `vector` extension; schema in
  `db/migrations/002_schema.sql`, applied by `apply-migrations.ps1`. Redis is up but
  **unwired** (Celery arrives with async ingestion, later in Phase 1).
- **`/backend`** — FastAPI (pip + venv). `GET /health` + `/health/db`; async DB in `app/db.py`
  (SQLAlchemy 2.x + asyncpg); `app/config.py` settings; `app/models.py` (`Document` + `Chunk`);
  `app/storage/` (M1 seam); `app/ingest/` (`parse.py` M2, `chunk.py` M3, `embed.py` M4,
  `write.py` M5, `inspect.py` the `/ingest-inspect` CLI). `unstructured[md,pdf]==0.23.1` +
  `pgvector` + `google-genai==2.10.0` in `requirements.txt`; `testcontainers[postgres]
  ==4.14.2` in `requirements-dev.txt` (M5's real-DB tests). `tests/conftest.py` is the new
  testcontainers Postgres harness (session container + rolled-back per-test session).
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4); fetches `/health/db`, builds clean.
- **Tooling** — `dev.ps1` / `stop.ps1`; `README.md` is the GitHub front page; `.gitignore`
  hardened (no `.env*` leaks — `backend/.env` is git-ignored).

## Git state

- Remote: **github.com/StefanoMarangoniSMpersonal/ProdRAG**, branch `main`. **Local `main` is
  2 commits ahead of `origin/main`** — M4 and M5 are committed locally but **not yet pushed**;
  M6 is **not committed yet** (working tree).
- **Pushed** history: `7030286` (README stub) → `869aaef` (Phase 0 + M0) → `771be9a` (M1) →
  `b252671` (scaling principle + job-shaped ingestion docs) → `3751bcb` (M2: parse + inspect
  CLI) → `fa8f3ff` (M3: chunk + inspect chunk view; docs/tracker updates rode in here).
- **Committed but UNPUSHED:** `1a52ade` (M4: embed — `gemini-embedding-2`, prefix roles, 768d;
  the parse concurrency-footgun comment rode in here) → `d9f4fd6` (**M5 write**: `write.py`,
  `test_write.py`, `conftest.py` testcontainers harness, `requirements-dev.txt` +
  `testcontainers`). No `Co-Authored-By` trailer (user preference). `git push` when ready.
- **UNCOMMITTED (M6 + the M4 batch fix, in the working tree):** M6 — `app/ingest/orchestrate.py`,
  `app/storage/base.py` + `app/storage/local.py` (`open_local`), `app/models.py` (E501 fix),
  `tests/conftest.py` (`session_factory` fixture), `tests/test_orchestrate.py`, `tests/test_storage.py`
  (`open_local` tests). **M4 batch fix (2026-07-07)** — `app/ingest/embed.py` (one `Content` per
  input + corrected SDK docstring), `tests/test_embed.py` (mock revision: `_sent_texts` + 2
  assertions; also hand-cleaned lint / restored the live-test key guard), and the new
  `@pytest.mark.live test_orchestrate_ingests_document_end_to_end_live` in `tests/test_orchestrate.py`.
  Plus these HANDOFF/PROGRESS refreshes. Consider **two commits**: the M6 milestone, then the M4
  batch-contract correction (it's a distinct fix to already-committed-earlier M4).
- **Otherwise**, `backend/tests/fixtures/Proactive Autoscaling.pdf` — a personal corpus doc no
  test references, left untracked on purpose.
- **Credentials:** none in the repo. The PAT used for an earlier push was exposed in chat —
  **revoke it**; use `gh auth login` or SSH next time.
- Auto-memory files live outside the repo (not in git).

## Open items

- **Pre-existing test-file lint debt (all `app/` source is clean).** The `app/models.py` E501
  nit is fixed (M6 session) and `tests/test_embed.py` was hand-cleaned (2026-07-07, with the batch
  fix — its `import os` is used again now that the live-test key guard is uncommented). What remains
  is one **immutable** file: `tests/test_parse.py` isn't `black`-clean (two commented-out lines).
  Left untouched on purpose — reformatting immutable specs is a deliberate call, not incidental
  churn. Worth a separate formatting-only cleanup commit.

## Reference — shapes & seams the upcoming steps read/write

**M0 schema (M5 writes here).** `documents` (uuid PK) + `chunks` (**bigint-identity PK**,
`document_id` uuid FK cascade). `chunks` columns: `content`, `embedding vector(768)`,
`char_count`, `token_count`, `element_type`, `section_title`, `page_number`, `metadata jsonb`,
`owner_id`, `UNIQUE(document_id, ordinal)`. HNSW index + `tsvector` keyword column are
**deferred** (commented in the SQL) — both derived from stored data → addable later with no
re-ingest. Rationale in `app/models.py` + memory `schema-migrations-convention`.

**M1 storage (M6 gets a PATH here, not bytes).** `Storage` Protocol + `LocalDiskStorage`; async
`save`/`load`/`exists`/`delete` **+ `open_local(uri)`** (added for M6). URI =
`file://<uuid4>/<safe-name>` (relative key, so `documents.source_uri` stays portable → S3 later
= `s3://<bucket>/<key>`, no schema change). `get_storage()` is the cached accessor (nobody names
the concrete class); `delete` idempotent; `load`/`exists`/`delete`/`open_local` reject
foreign-scheme + path-traversal URIs. **`open_local` is the M6 seam:** an `@asynccontextmanager`
yielding a real on-disk `Path` — local yields the blob's own path (zero copy, no cleanup); a
future S3 backend streams to a temp file and unlinks on exit. So M6 never realizes the file into
RAM (matches the path-only M2 revision). `byte_size`/`checksum`/`content_type` are **M7's** job at
insert (it has the request bytes for free); M6 stays path-only.

**M2 parse.** `parse_document(source, *, strategy="auto") -> list[Element]`
(`app/ingest/parse.py`) wraps Unstructured `partition` in `asyncio.to_thread`; returns **native
Unstructured `Element`s** (parse + chunk share the type; map to the `Chunk` ORM only at M5).
`source` is a **local file path** (passed to `partition(filename=…)`), not bytes — revised
2026-07-06 so the caller never realizes the whole file into RAM and poppler/pdfminer get the
real path they want (M6 gets that path from the storage seam's future `open_local`; tests +
`inspect` already hold one). The path **extension** routes the format (so libmagic/`python-magic`
never enters — the Windows ban stays moot). `strategy` affects PDFs/images only; the
pipeline default is **`hi_res`** (`Settings.ingest_pdf_strategy`) because
`infer_table_structure=True` (always passed) only fires under `hi_res` — that keeps a `Table`'s
grid as `metadata.text_as_html` instead of a flattened blob. poppler 26.02.0 + tesseract 5.4.0
installed (README Prerequisites); `--strategy fast` (pdfminer) needs neither.

_Windows/Unstructured gotchas (will recur — all handled):_
1. **Never install `python-magic`/`python-magic-bin`** — the `-bin` wheel's 2014 libmagic DLL
   segfaults on load. Without it Unstructured uses pure-Python extension detection (fed by
   `metadata_filename`); a harmless "libmagic unavailable" info line is expected.
2. **Extras are per-format:** `[pdf]` alone can't parse `.md` — use `unstructured[md,pdf]`.
3. **Lazy import:** `parse.py` imports `partition.auto` inside the call (and `Element` under
   `TYPE_CHECKING`), so import is instant; the ~20-30s torch load happens on first parse only.
4. **ASCII-only CLI output** — the cp1252 console crashes on fancy glyphs.

**M3 chunk.** `chunk_document(elements, *, max_characters=1500, combine_text_under_n_chars=500)
-> list[Element]` (`app/ingest/chunk.py`) over Unstructured `chunk_by_title`: new chunk per
Title, runt sections under the combine value merged, hard cap splits oversized elements, Tables
isolated. Output stays native (`CompositeElement` / `Table`); `include_orig_elements=True`
leaves each chunk's source elements in **`metadata.orig_elements`** for **M5 to mine**
(`section_title`, `page_number`). **`chunk_document` is `async`** (wraps the fast pure-Python
call in `to_thread`) purely to shape the interface — a future heavy chunker (`by_similarity`
runs an embedding model) then drops in behind the same `await` seam without touching M6.
**`text_as_html` survives chunking** (Tables isolated + `max_characters` caps html too) → M5
stores it in `chunks.metadata`; it's never the embedded text. Config knobs
`ingest_chunk_max_characters` / `ingest_chunk_combine_text_under_n_chars` (env-overridable; the
real Unstructured param is `combine_text_under_n_chars`). `/ingest-inspect` prints an element
view + a chunk view (`--max-chars` / `--combine` to tune by eye).

**M4 embed.** `async embed_texts(texts: list[str]) -> list[list[float]]`
(`app/ingest/embed.py`) via Gemini (`google-genai==2.10.0`, `client.aio` async, API-key auth).
**Interface A — pure text embedder:** takes strings not Unstructured elements, so it's reused
verbatim at query time and enrich resolves `embed_text` upstream (M4 stays enrich-oblivious).
**Role is a text PREFIX, not `task_type`** — `gemini-embedding-2` dropped the `task_type` config
field (the backend ignores it; that was `gemini-embedding-001`'s API), so the doc/query
asymmetry is now an instruction prepended to the input: document = `title: {title} | text: …`,
query = `task: search result | query: …`. The caller builds it via the two helpers
`as_retrieval_document(text, *, title=None)` / `as_retrieval_query(text)` (embed.py is the single
home for the format) then hands the finished string to `embed_texts`, which is **role-agnostic**
and embeds exactly what it's given. `title` is a seam for `by_title` headings / enrich; ingest
passes none today. Batches by `embedding_batch_size` (100), sequential (naive; async seam allows
parallel later), empty → `[]` with 0 calls. **Batch shape (fixed 2026-07-07, verified live):**
each input is sent as its **own `types.Content(parts=[Part(text=…)])`**, NOT as a raw `list[str]` —
gemini-embedding-2's SDK reads a bare string list as the many Parts of ONE Content and returns a
single fused vector for the whole batch (the live end-to-end run caught this: 16 chunks → 1
vector → `write_chunks` length-guard fired). One Content per input → one vector per input, still
one call per batch. **`_l2_normalize` is kept as a defensive no-op** —
`gemini-embedding-2` AUTO-normalizes even truncated (768-dim) vectors (Matryoshka training), so
the old "un-normalized <3072" rationale is false for v2; the guard stays only in case a future
model doesn't self-normalize. Config: `gemini_api_key` (env, `""` default),
`embedding_model="gemini-embedding-2"` (**ID confirmed valid by the live test**),
`embedding_dimensions=768`, `embedding_batch_size=100`. **Test = mock + opt-in live:**
`test_embed.py` monkeypatches the client to a fake recorder → 5 offline tests pin the contract
(order, batching, `output_dimensionality`+**no** `task_type` reach the SDK, prefix-helper formats,
empty→[], unit-length; the batch-shape assertions unwrap each sent `Content` via the `_sent_texts`
helper — the mock revised 2026-07-07 to encode the one-Content-per-input contract); 1
`@pytest.mark.live` test hits real Gemini (passed 2026-07-06, re-verified 2026-07-07 after the
batch fix), **deselected** (not skipped) by `pytest.ini addopts = -m "not live"`, run via
`pytest -m live` + `GEMINI_API_KEY` (must be in the **process env** — the test's `assert
os.getenv(...)` guard reads the process env, which `.env` does NOT populate; and `env_file=".env"`
resolves against CWD, so a real `Settings` call also needs CWD=`backend/`). The full pipeline is
exercised live too — `test_orchestrate_..._live` (see M6) runs real parse+chunk+embed+write; it
loads the key from `.env` (via `Settings`), so unlike the M4 live test it does NOT need the
process-env var, only CWD=`backend/`.

**M5 write.** `async write_chunks(session, document_id, chunks, embeddings, *,
owner_id=DEV_OWNER_ID) -> list[Chunk]` (`app/ingest/write.py`) — the **translation boundary**:
maps each M3 chunk `Element` + its index-aligned M4 vector into a `Chunk` ORM row. **Two aligned
lists, not `embed(content)`** — a length mismatch raises `ValueError` *before* any DB work
(mis-pairing vectors↔text is the worst corruption); this shape absorbs the deferred enrich seam
(embed a summary while `content` stays raw) with zero M5 change. **`flush` but NO commit** — M6
owns the transaction so chunk-write + `documents.status` commit together. Pure `_chunk_to_row`
mines `section_title` (first `Title` in `metadata.orig_elements`), `page_number`, `element_type`
(`type(el).__name__` → `CompositeElement`/`Table`), `char_count`, and lands a table's
`text_as_html` in the `metadata` JSONB (never the embedded text); `token_count` stays NULL
(deferred). **Test = 1 offline guard + 3 real-DB round-trips** (`test_write.py`) over a throwaway
`testcontainers` pgvector container (`conftest.py`: session container + rolled-back per-test
`db_session`); schema applied via a **raw asyncpg** connection (SQLAlchemy's asyncpg dialect
can't run a multi-statement `.sql`). Vectors + elements are **synthetic** (no paid Gemini, no
poppler/tesseract) — the end-to-end parse→…→write proof is M6's job.

**M6 orchestrate.** `async orchestrate(document_id: uuid.UUID) -> IngestResult`
(`app/ingest/orchestrate.py`) — the conductor + first true end-to-end run. Reads the `pending`
row's `source_uri`, then `open_local`→`parse_document`(`hi_res` from settings)→`chunk_document`
→ wrap each chunk via `as_retrieval_document(c.text)` (the `embed_text` seam, title=None today)
→`embed_texts`→`write_chunks`, driving `documents.status` `pending`→`processing`→`ready`/`failed`.
**Three short transactions, never one held across the slow work:** Txn1 claims the row
(`processing`, committed alone so progress is observable + no pooled connection is held across the
10s+ parse/embed); the heavy stages run with NO txn open; Txn2 = `write_chunks` flush + `status=
'ready'` in **one atomic commit** (M5 only flushes, M6 owns this commit); on any exception →
rollback + a **fresh** Txn3 writing `failed`+`error` (must survive the poisoned rollback). Returns
an **`IngestResult`** (status, chunk_count, per-stage `timings_ms`, error) a caller doesn't wait on
— Celery ignores it, client polls status — and emits structured logs (`ingest.complete`/`failed`/
`empty`), never silent (eval-as-substrate). Missing doc → `LookupError`; zero-chunk doc → `ready`
+ a warning (naive). **On failure the blob is KEPT** (retry/debug; lifecycle belongs to the row's
creator, M7). Collaborators (`SessionLocal`/`get_storage`/`embed_texts`/…) are **module globals**
so tests monkeypatch this module's copy (M4-test pattern). **Deferred:** a hard crash between Txn1
and Txn2 leaves a stuck `processing` row — no reaper yet (arrives with Celery). **Test = 3
immutable** (`test_orchestrate.py`): real parse+chunk on the md corpus + faked embed + real
Postgres write via a new committing `session_factory` fixture (TRUNCATE teardown — the rollback
`db_session` can't be used since M6 commits) → ready/contiguous-ordinals/768-dim; embed-raises →
`failed`+error+**zero** chunks (atomic proof)+blob kept; missing-doc raises. Plus 3 `open_local`
tests in `test_storage.py`. **Plus 1 opt-in `@pytest.mark.live` sibling**
(`test_orchestrate_ingests_document_end_to_end_live`, added 2026-07-07): the same wiring but
`embed_texts` is left REAL, so it runs the whole pipeline against Gemini — this is the test that
**caught the M4 batch bug**. Deselected by default; needs Docker + CWD=`backend/` (loads the key
from `.env`).

**M2 parse concurrency footgun (documented, deferred — Option A).** parse's `to_thread` uses the
shared default pool; safe because ingestion is job-shaped (1 doc/job, Celery `--concurrency`
bounds cross-doc), but a future in-process `gather` over many parses would OOM under hi_res. A
comment on the seam records the fix (an `asyncio.Semaphore` if a batch caller ever lands). Memory
`phase1-ingestion`.

## Decisions locked (still constraining upcoming work)

- **Phase 1 = sync-first, local disk, local Unstructured, real `gemini-embedding-2` @768d**
  (doc/query role via text prefix — v2 has no `task_type`). Deferred with no rework: Celery/Redis,
  S3, keyword/BM25 index, RLS enforcement, observability, the whole query pipeline (Phase 2).
- **Ingestion is JOB-SHAPED, not request-shaped** (the guardrail that makes "Celery later = no
  rework" true). M6 (done) = a self-contained coroutine keyed on `document_id` (gets a path via
  `get_storage().open_local()` — never loads bytes into RAM — does parse→chunk→embed→write, drives
  `documents.status`), returns an `IngestResult` no caller waits on. M7 (next) = persist file +
  insert `documents` row `pending` + kick the job + return **202 + doc id**; client polls `status`
  until `ready`. Celery swap is one line (`await orchestrate(id)` → `orchestrate.delay(id)`). Watch
  (no data reshape): `database_url` may split into pooled-worker + session URLs once Celery+RLS
  land. Memory `phase1-ingestion`.
- **Table-structure + deferred M-enrich:** `infer_table_structure=True` always; `text_as_html`
  carried to `chunks.metadata`. A deferred **M-enrich** stage (between M3 and M4, eval-gated)
  will embed an LLM summary of each Table/Image chunk via the per-chunk **`embed_text`** seam
  (defaults to `content`, so filling it never reshapes M4/M5); raw stays as `content` for
  display/citation. Image extraction is v1-deferred and rides the same path. Full text in
  `CLAUDE.md` → "Locked decisions".
- **Eval is a substrate, not a phase:** golden set + ingestion-side metrics start now; the
  RAGAS harness + per-query log switch on in Phase 2. The M6 orchestrator emits structured
  metrics (per-stage `timings_ms` + chunk_count via `IngestResult` and `ingest.*` logs), never
  silent — the ingestion-side half of this is live.
- **Schema mechanism:** Option A — hand-written numbered SQL + hand-mirrored models; defer
  Alembic. **Keys:** UUID `documents` (exposed), bigint-identity `chunks` (internal). **owner_id**
  NOT NULL, dev-user default; RLS becomes a policy change later. **Auth:** Supabase Auth (not
  Clerk), JWKS/ES256 + RLS caveats (memory `auth-supabase`). **Retrieval:** own `chunks` +
  hand-write hybrid SQL (LangChain thin); `tsvector`-vs-`pg_search` deferred to Phase 2.

## Suggested first moves next session

1. **Commit the working tree**, ideally as two commits: (a) the **M6 orchestrator** milestone, and
   (b) the **M4 batch-contract fix** (`embed.py` one-Content-per-input + the `test_embed.py` mock
   revision + the new live end-to-end test) — a distinct correction to the already-committed M4.
   Optionally a third formatting-only commit for the pre-existing `test_parse.py` `black` debt (see
   Open items). Then `git push` (M4, M5, M6 all unpushed).
2. **M7 upload endpoint, test-first:** persist file (M1 `save`) → insert `documents` row `pending`,
   filling `byte_size`/`checksum`/`content_type` from the request bytes (M6 is path-only, so this
   is M7's job) → kick the job (`await orchestrate(id)` now; `orchestrate.delay(id)` once Celery
   lands) → return **202 + doc id**; client polls `status` until `ready`. `orchestrate` is done and
   tested — M7 is the HTTP shell + `pending`-row insert on top of it. Watch: M7 is `request`-shaped
   but must NOT block on the pipeline — kick-and-return, don't `await` the whole ingest inline (the
   job-shaped guardrail; today the naive `await` is fine because it's still async off the response).
3. **Live tests — confirmed 2026-07-06 & re-verified 2026-07-07:** `gemini-embedding-2` is a valid
   ID, v2 accepts the prefixed input with no `task_type`, and the batch fix (one `Content` per
   input) is proven end-to-end. To re-run the M4 embed live test: `GEMINI_API_KEY` in the **process
   env** (not just `backend/.env` — its `os.getenv` guard won't read the file), then
   `pytest -m live`. The orchestrate live test loads the key from `.env`, so it only needs
   CWD=`backend/` + Docker up.
4. **Naive-first but still test-first** — the red/green net exists before the naive code, so
   improving it later stays safe. Slow down and teach on the RAG core.
