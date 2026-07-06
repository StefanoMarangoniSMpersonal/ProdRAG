# Session Handoff — resume here next time

_Last updated: 2026-07-06 (M4 embed landed test-first, then corrected — `gemini-embedding-2`
has no `task_type`, role is now a text prefix; M2+M3 committed and **pushed**, M4 + the parse
concurrency-footgun note in the working tree, uncommitted)._
_Read this + `CLAUDE.md` (the constitution) to pick up. Companions: `docs/PROGRESS.md`
(layer-by-layer % tracker) and auto-memory (`MEMORY.md`, loaded every session) for
cross-session decisions. This file keeps only what's live + what's next — settled detail
lives in the code, PROGRESS.md, and memory._

## Where we are

- **Done & verified:** Phase 0 spine (Next.js → FastAPI → Postgres/pgvector → back),
  and Phase 1 **M0** (schema), **M1** (storage seam), **M2** (parse), **M3** (chunk),
  **M4** (embed). Suite = **16 tests** green + **1 live deselected** (6 storage + 3 parse
  + 2 chunk + 5 embed). Per-milestone shapes are in "Reference" below; % in `PROGRESS.md`.
- **Next up: Phase 1 · M5 — write.** Map each chunk → a `Chunk` ORM row + its vector: set
  `content`, `embedding` (from M4), `ordinal`, `element_type`, `section_title`,
  `page_number`, and carry `text_as_html` into `chunks.metadata` (JSONB). Persist under the
  DEV owner for now (RLS comes later). The `embed_text` seam is M6's concern (it assembles
  the strings M4 embeds); M5 just writes what it's given. Then M6 orchestrator
  (parse→chunk→embed→write, drives `documents.status`) → M7 upload endpoint.
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

## Repo state (what exists)

- **`/infra`** — `docker-compose.yml`: Postgres (`pgvector/pgvector:pg16`) + Redis.
  `db/init/001_pgvector.sql` enables the `vector` extension; schema in
  `db/migrations/002_schema.sql`, applied by `apply-migrations.ps1`. Redis is up but
  **unwired** (Celery arrives with async ingestion, later in Phase 1).
- **`/backend`** — FastAPI (pip + venv). `GET /health` + `/health/db`; async DB in `app/db.py`
  (SQLAlchemy 2.x + asyncpg); `app/config.py` settings; `app/models.py` (`Document` + `Chunk`);
  `app/storage/` (M1 seam); `app/ingest/` (`parse.py` M2, `chunk.py` M3, `embed.py` M4,
  `inspect.py` the `/ingest-inspect` CLI). `unstructured[md,pdf]==0.23.1` + `pgvector` +
  `google-genai==2.10.0` in `requirements.txt`.
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4); fetches `/health/db`, builds clean.
- **Tooling** — `dev.ps1` / `stop.ps1`; `README.md` is the GitHub front page; `.gitignore`
  hardened (no `.env*` leaks — `backend/.env` is git-ignored).

## Git state

- Remote: **github.com/StefanoMarangoniSMpersonal/ProdRAG**, branch `main`. **Local `main` is
  in sync with `origin/main`** (all pushed).
- **Pushed** history: `7030286` (README stub) → `869aaef` (Phase 0 + M0) → `771be9a` (M1) →
  `b252671` (scaling principle + job-shaped ingestion docs) → `3751bcb` (M2: parse + inspect
  CLI) → `fa8f3ff` (M3: chunk + inspect chunk view; docs/tracker updates rode in here). No
  `Co-Authored-By` trailer (user preference).
- **Uncommitted working-tree edits** (not yet on any commit): the **M4 embed** work
  (`app/ingest/embed.py`, `tests/test_embed.py`, `config.py` fields, `requirements.txt` +
  `google-genai`, `pytest.ini` `live` marker), the **M2 parse concurrency-footgun** comment, and
  the HANDOFF/PROGRESS/memory refreshes — commit when ready. Plus `backend/tests/fixtures/Proactive
  Autoscaling.pdf`, a personal corpus doc no test references, left untracked on purpose.
- **Credentials:** none in the repo. The PAT used for an earlier push was exposed in chat —
  **revoke it**; use `gh auth login` or SSH next time.
- Auto-memory files live outside the repo (not in git).

## Open items

- **`app/models.py` lint nit (rode into the M2 commit):** an M0 inline comment is >88 chars,
  so repo-wide `ruff check backend` / `black --check backend` are NOT clean. Every other file
  passes — shortening that one comment turns the repo-wide lint green.

## Reference — shapes & seams the upcoming steps read/write

**M0 schema (M5 writes here).** `documents` (uuid PK) + `chunks` (**bigint-identity PK**,
`document_id` uuid FK cascade). `chunks` columns: `content`, `embedding vector(768)`,
`char_count`, `token_count`, `element_type`, `section_title`, `page_number`, `metadata jsonb`,
`owner_id`, `UNIQUE(document_id, ordinal)`. HNSW index + `tsvector` keyword column are
**deferred** (commented in the SQL) — both derived from stored data → addable later with no
re-ingest. Rationale in `app/models.py` + memory `schema-migrations-convention`.

**M1 storage (M6 reads bytes here).** `Storage` Protocol + `LocalDiskStorage`; async
`save`/`load`/`exists`/`delete`. URI = `file://<uuid4>/<safe-name>` (relative key, so
`documents.source_uri` stays portable → S3 later = `s3://<bucket>/<key>`, no schema change).
`get_storage()` is the cached accessor (nobody names the concrete class); `delete` idempotent;
`load`/`exists`/`delete` reject foreign-scheme + path-traversal URIs. M6 computes
`byte_size`/`checksum`/`content_type` from the same bytes.

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
parallel later), empty → `[]` with 0 calls. **`_l2_normalize` is kept as a defensive no-op** —
`gemini-embedding-2` AUTO-normalizes even truncated (768-dim) vectors (Matryoshka training), so
the old "un-normalized <3072" rationale is false for v2; the guard stays only in case a future
model doesn't self-normalize. Config: `gemini_api_key` (env, `""` default),
`embedding_model="gemini-embedding-2"` (**ID confirmed valid by the live test**),
`embedding_dimensions=768`, `embedding_batch_size=100`. **Test = mock + opt-in live:**
`test_embed.py` monkeypatches the client to a fake recorder → 5 offline tests pin the contract
(order, batching, `output_dimensionality`+**no** `task_type` reach the SDK, prefix-helper formats,
empty→[], unit-length); 1 `@pytest.mark.live` test hits real Gemini (passed 2026-07-06),
**deselected** (not skipped) by `pytest.ini addopts = -m "not live"`, run via `pytest -m live` +
`GEMINI_API_KEY` (must be in the **process env** — the test reads `os.getenv`, which `.env`
does not populate; and `env_file=".env"` resolves against CWD, so a real `Settings` call also
needs CWD=`backend/`).

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
  rework" true). M6 = a self-contained coroutine keyed on `document_id` (reads bytes via
  `get_storage().load()`, does parse→chunk→embed→write, drives `documents.status`), returns
  nothing to a caller. M7 = persist file + insert `documents` row `pending` + kick the job +
  return **202 + doc id**; client polls `status` until `ready`. Celery swap is one line
  (`await orchestrate(id)` → `orchestrate.delay(id)`). Watch (no data reshape): `database_url`
  may split into pooled-worker + session URLs once Celery+RLS land. Memory `phase1-ingestion`.
- **Table-structure + deferred M-enrich:** `infer_table_structure=True` always; `text_as_html`
  carried to `chunks.metadata`. A deferred **M-enrich** stage (between M3 and M4, eval-gated)
  will embed an LLM summary of each Table/Image chunk via the per-chunk **`embed_text`** seam
  (defaults to `content`, so filling it never reshapes M4/M5); raw stays as `content` for
  display/citation. Image extraction is v1-deferred and rides the same path. Full text in
  `CLAUDE.md` → "Locked decisions".
- **Eval is a substrate, not a phase:** golden set + ingestion-side metrics start now; the
  RAGAS harness + per-query log switch on in Phase 2. The M6 orchestrator must emit structured
  metrics, not run silently.
- **Schema mechanism:** Option A — hand-written numbered SQL + hand-mirrored models; defer
  Alembic. **Keys:** UUID `documents` (exposed), bigint-identity `chunks` (internal). **owner_id**
  NOT NULL, dev-user default; RLS becomes a policy change later. **Auth:** Supabase Auth (not
  Clerk), JWKS/ES256 + RLS caveats (memory `auth-supabase`). **Retrieval:** own `chunks` +
  hand-write hybrid SQL (LangChain thin); `tsvector`-vs-`pg_search` deferred to Phase 2.

## Suggested first moves next session

1. **M5 (write), test-first:** failing write test first, then map each chunk → a `Chunk` ORM row
   + vector: `content`, `embedding` (from M4), `ordinal`, `element_type`, `section_title`,
   `page_number` (mine the last three from `metadata.orig_elements`), and `text_as_html` into the
   `metadata` JSONB. DEV owner for now. Needs a DB session seam (async SQLAlchemy); the M4
   `embed_text` selection is M6's job, not M5's — M5 writes what it's handed.
2. **M6 orchestrator:** self-contained coroutine keyed on `document_id` that runs
   parse→chunk→embed→write and drives `documents.status`; assembles the strings M4 embeds (today
   just each chunk's content — the `embed_text` seam). Then **M7** upload endpoint (202 + doc id).
3. **Live embed test — already confirmed once (2026-07-06):** `gemini-embedding-2` is a valid ID
   and v2 accepts the prefixed input with no `task_type`. To re-run: `GEMINI_API_KEY` must be in
   the **process env** (not just `backend/.env` — `os.getenv` won't read the file), then
   `pytest -m live`.
4. **Naive-first but still test-first** — the red/green net exists before the naive code, so
   improving it later stays safe. Slow down and teach on the RAG core (write/orchestrate).
