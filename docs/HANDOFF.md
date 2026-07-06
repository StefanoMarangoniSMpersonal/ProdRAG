# Session Handoff — resume here next time

_Last updated: 2026-07-06 (M3 chunk landed test-first: `app/ingest/chunk.py` groups M2's
elements via Unstructured `by_title` with `max_characters=1500` / `combine_text_under_n_chars
=500`; the isolated Table chunk keeps its `text_as_html`; `/ingest-inspect` grew a chunk view;
2 immutable chunk tests green — suite now 11. M2 revisit before it: `infer_table_structure=True`
always + PDF strategy defaults to `hi_res`, proven by an automated red→green test.)
Read this + `CLAUDE.md` to pick up where we left off._
_Cross-session decisions also live in auto-memory (`MEMORY.md` is loaded every session)._
_For the layer-by-layer completion breakdown (10 architectural layers, code-only vs. code +
design), see `docs/PROGRESS.md` — the living development tracker._

## Where we are

- **Phase 0 (stack spine): complete & verified.** A request travels the whole stack:
  Next.js → FastAPI → Postgres/pgvector → back.
- **Phase 1 · M0 (ingestion schema): complete & verified.** `documents` + `chunks` tables
  exist in the DB, mirrored by SQLAlchemy models; a round-trip + cascade test passed.
- **Phase 1 · M1 (storage interface): complete & verified.** `Storage` Protocol +
  `LocalDiskStorage` behind a clean seam (`save`/`load`/`exists`/`delete`); 6 pytest tests
  green. See "M1 storage" below.
- **Phase 1 · M2 (parse): complete & verified — incl. PDF `hi_res` table path.**
  `app/ingest/parse.py` turns raw bytes into typed Unstructured elements; `app/ingest/inspect.py`
  is the `/ingest-inspect` dry-run CLI. Parse now always passes `infer_table_structure=True`, so
  tables keep their grid as `metadata.text_as_html` (not a flattened blob); that only fires under
  `hi_res`, so the PDF strategy **defaults to `hi_res`** via `Settings.ingest_pdf_strategy`.
  **9 pytest tests green** (6 storage + 3 parse), ruff/black clean on the touched files. The new
  `test_parse_pdf_hi_res_infers_table_structure` drives a committed table PDF and asserts the grid
  survives — so the suite now **depends on poppler/tesseract** for that one test (both installed;
  it ERRORs loudly, never SKIPs, if they're absent). See "M2 parse" below + the Windows gotchas.
- **Phase 1 · M3 (chunk): complete & verified.** `app/ingest/chunk.py` groups M2's elements
  into retrieval-sized chunks via Unstructured `by_title` — `async chunk_document(...)` wrapping
  `chunk_by_title` in `asyncio.to_thread` (an interface-shaping choice: a future heavy chunker
  like `by_similarity` drops in behind the same `await` seam). Config knobs
  `ingest_chunk_max_characters=1500` + `ingest_chunk_combine_text_under_n_chars=500`. The
  isolated Table chunk keeps `metadata.text_as_html` (the M3 requirement). `/ingest-inspect`
  grew a chunk view (`--max-chars` / `--combine`). **2 immutable chunk tests green** (built
  test-first, red→green); suite now 11. ruff/black clean on the touched files. See "M3 chunk".
- **Next up: Phase 1 · M4 — embed** (`gemini-embedding-2` @768d, `RETRIEVAL_DOCUMENT`, batched
  N chunks/request + retry/backoff). Build the per-chunk **`embed_text`** seam (defaults to
  `content`) so the deferred **M-enrich** stage drops in without reshaping M4/M5. Then M5 write
  → M6 orchestrator → M7 upload endpoint. **Gemini surface decided:** the Gemini Developer API
  via **`google-genai`**, API-key auth (see "Still-open decisions" → resolved). Remaining prep:
  add `google-genai` to `requirements.txt`, put `GEMINI_API_KEY` in `backend/.env`, and confirm
  the `task_type`/`output_dimensionality` param names against current SDK docs. **Build M4
  test-first** (rule below).
- **New hard rule (2026-07-06): test-first & test-immutable.** Every new module/feature is
  written test-first (red → green); once written, a test is **immutable** — if code fails you
  fix the code, never the test (skipping/`xfail`/deleting/loosening all count as editing it).
  The one exception — a test that encodes the wrong spec — is a human decision: stop and ask,
  don't silently edit. Full text in `CLAUDE.md` → "Testing — test-first and test-immutable";
  memory `testing-test-first-immutable`. This governs M3 onward (M0–M2 tests already exist).

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
  **`app/ingest/`** = the M2 parse stage: `parse.py` (`parse_document(bytes, filename, *,
  strategy) -> list[Element]`, wraps Unstructured `partition` in `asyncio.to_thread`, always
  passes `infer_table_structure=True`), `chunk.py` (the M3 stage: `chunk_document(elements, *,
  max_characters, combine_text_under_n_chars) -> list[Element]`, async seam over Unstructured
  `chunk_by_title`), and `inspect.py` (`python -m app.ingest.inspect <path> [--strategy ...]
  [--max-chars N] [--combine N]`, dry-run showing BOTH the element view and the M3 chunk view;
  prints an `[html]` marker on elements/chunks carrying `text_as_html`). `ingest_pdf_strategy`
  (`= "hi_res"`), `ingest_chunk_max_characters` (`= 1500`), and
  `ingest_chunk_combine_text_under_n_chars` (`= 500`) settings in `config.py`.
  `unstructured[md,pdf]==0.23.1` in `requirements.txt`.
- **`/frontend`** — Next.js (App Router, TS, Tailwind v4). Page fetches `/health/db`, renders
  the JSON. Builds clean.
- **Tooling** — `dev.ps1` (one-command setup+start, auto-applies migrations) / `stop.ps1`.
  `README.md` rewritten as a full GitHub front page. `.gitignore` hardened (no `.env*` leaks).
- **Tests** — `pytest` + `pytest-asyncio` (in `requirements-dev.txt`). Config is **`pytest.ini`
  at the repo root** (not `backend/`), so `pytest` works from any dir. Run from the repo root;
  currently **11 tests** (6 storage + 3 parse + 2 chunk). NB: a SKIP exits 0 — treat skips as a false green
  (memory `testing-skips-are-not-passes`). Two parse tests run on the Markdown corpus
  `backend/tests/fixtures/rag_test_document.md` (no system binaries → can't silently skip),
  asserting stable facts ("Aurelia Robotics", "Falcon-9X"). The third,
  `test_parse_pdf_hi_res_infers_table_structure`, drives the committed
  `backend/tests/fixtures/quarterly_report.pdf` under `hi_res` and **needs poppler + tesseract**
  (downloads the table-transformer model on first run) — it ERRORs loudly, never SKIPs, if they're
  missing. The **2 M3 chunk tests** (`backend/tests/test_chunk.py`) mirror this split:
  `test_chunk_by_title_groups_and_caps` runs on the Markdown corpus (grouping happened +
  every chunk ≤ 1500 chars), and `test_chunk_pdf_table_keeps_text_as_html` reuses the PDF
  fixture under `hi_res` (same poppler/tesseract dependency, ERRORs-not-SKIPs) to prove the
  isolated Table chunk keeps `text_as_html`. `sample.md` in that dir is orphaned (safe to
  delete). **These are pytest tests — run via
  `python -m pytest`, not `python test_parse.py`** (the latter can't resolve the `app` package).
  **Use the project venv explicitly:** `backend\.venv\Scripts\python.exe -m pytest` — the shell's
  ambient `python`/`pip` may point at a *different* project's venv.

## How to run (assume nothing is running in a fresh session)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts all 3 pieces
.\stop.ps1    # stops Postgres/Redis containers   (.\stop.ps1 -Wipe also drops the pgdata volume)
```
`dev.ps1` opens the API (:8000) and frontend (:3000) each in its own window. Manual steps and
troubleshooting are in `README.md`. Verify: `curl http://localhost:8000/health/db`.

## Git state

- Initialized `main`, pushed to **github.com/StefanoMarangoniSMpersonal/ProdRAG**.
- History (all pushed): `7030286` (README stub) → `869aaef` (Phase 0 + M0) → `771be9a` (M1
  storage) → `b252671` (scaling principle + job-shaped ingestion docs). Optional cleanup: squash
  the stub into the first real commit (needs a force-push → ask first).
- **M2 AND M3 are NOT committed yet** (user asked to hold at M2; M3 built on top, still
  uncommitted). Uncommitted working tree (current):
  - new (`??`): `backend/app/ingest/` (parse.py, **chunk.py**, inspect.py, __init__.py),
    `backend/tests/test_parse.py`, **`backend/tests/test_chunk.py`**,
    `backend/tests/fixtures/` (`rag_test_document.md` = user's real corpus,
    `quarterly_report.pdf` = the M2/M3 table fixture, plus an orphaned `sample.md`);
    `docs/PROGRESS.md` (layer-by-layer tracker).
  - modified (`M`): `backend/requirements.txt` (`unstructured[md,pdf]`), `backend/app/config.py`
    (`ingest_pdf_strategy` + the two `ingest_chunk_*` knobs), `backend/app/models.py` (see lint
    nit below), `CLAUDE.md` (test-first rule + table-structure/enrich locked decisions),
    `README.md` (poppler/tesseract prereq + PROGRESS.md pointers), `docs/HANDOFF.md` (this file).
  - Three logical commits — keep them separate if you like:
    1. **M2 code** — subject: "Phase 1 M2: parse stage (Unstructured) + ingest-inspect CLI"
       (`parse.py`, `inspect.py` element view, `tests/test_parse.py`, `tests/fixtures/`,
       `ingest_pdf_strategy` in `config.py`, `requirements.txt`, `models.py`, poppler prereq
       in `README.md`).
    2. **M3 code** — subject: "Phase 1 M3: chunk stage (by_title) + inspect chunk view"
       (`backend/app/ingest/chunk.py`, `tests/test_chunk.py`, the `ingest_chunk_*` settings in
       `config.py`, the chunk view + `--max-chars`/`--combine` flags in `inspect.py`).
    3. **Docs/process** — the tracker + testing rule (`docs/PROGRESS.md`, `CLAUDE.md`, the
       PROGRESS pointers in `README.md`/`docs/HANDOFF.md`); commit-able independently of the hold.
  - No `Co-Authored-By` trailer on any (user preference).
  - Not in git (lives outside the repo): auto-memory files — `layer-scoring-rubric`,
    `testing-test-first-immutable`, and this session's `check-env-before-installing` (+ updated
    `phase1-ingestion`).
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

## M2 parse — bytes → typed elements (Unstructured)

`parse_document(data, filename, *, strategy="auto") -> list[Element]` (`app/ingest/parse.py`)
wraps Unstructured's `partition`, run inside `asyncio.to_thread` (it's sync + CPU-heavy under
`hi_res`). It returns **native Unstructured `Element` objects** on purpose: the M3 chunker
(`chunk_by_title`) consumes them, so parse+chunk both speak that type and we map to the `Chunk`
ORM model only at M5 — the DB/retrieval layers never see an Unstructured type. `filename`'s
**extension** is how the format is routed (`.pdf`/`.md`/…), so it must be real even though the
bytes are what's parsed. `strategy` (`fast`/`hi_res`/`ocr_only`/`auto`) only affects PDFs/images;
`parse_document`'s own default stays `"auto"` for library callers, but the real pipeline default
is **`hi_res`** (`Settings.ingest_pdf_strategy`) because `infer_table_structure=True` (always
passed) only runs under `hi_res`. That flag makes a `Table` keep its grid as `metadata.text_as_html`
instead of a flattened blob (which reorders columns — visible in `/ingest-inspect`). `text_as_html`
is carried through M3 into `chunks.metadata` (JSONB) at M5; it is never the embedded text directly.
`inspect.py` = the `/ingest-inspect` dry-run: reads a path directly (no storage/DB writes) and
prints the elements + a summary (with an `[html]` marker on structured elements); chunk-level
checks arrive at M3.

**Windows gotchas discovered (all handled — see `requirements.txt` comments + memory):**
1. **Do NOT install `python-magic`/`python-magic-bin`.** The `-bin` wheel ships a 2014 libmagic
   DLL that **segfaults on load** (access violation) and kills the process. Without it,
   Unstructured falls back to pure-Python **extension**-based filetype detection (a harmless
   "libmagic is unavailable" info line) — which is what our `metadata_filename` already feeds it.
2. **Per-format extras:** `unstructured[pdf]` alone can't parse `.md` (`ModuleNotFoundError:
   markdown`). Use `unstructured[md,pdf]`; add more extras (`docx`, `pptx`, …) when needed.
3. **Lazy import:** `parse.py` imports `partition.auto` *inside* the call (and `Element` under
   `TYPE_CHECKING`), so `import app.ingest.parse` is ~instant; the ~20-30s torch/transformers
   load happens only on the first actual parse, not on app startup / test collection.
4. **ASCII-only CLI output** — the cp1252 console can't encode fancy glyphs (would crash print).

**PDF path now exercised & under automated test.** `test_parse_pdf_hi_res_infers_table_structure`
runs `backend/tests/fixtures/quarterly_report.pdf` through `hi_res` and asserts the Table keeps
`text_as_html`. **poppler** + **tesseract** are installed (verified: poppler 26.02.0 at
`C:\Users\SMarangoni\Poppler\...\Library\bin`, tesseract v5.4.0) and now a hard dependency of that
one test (README Prerequisites). `--strategy fast` (pdfminer, digital PDFs) still needs neither.

## M3 chunk — typed elements → retrieval-sized chunks (Unstructured by_title)

`chunk_document(elements, *, max_characters=1500, combine_text_under_n_chars=500) -> list[Element]`
(`app/ingest/chunk.py`) groups M2's elements into chunks via Unstructured's `chunk_by_title`:
a new chunk starts at each Title, runt sections under `combine_text_under_n_chars` are merged,
every chunk is capped at `max_characters` (a HARD max — an oversized element is split), and
Tables are isolated into their own chunk. Output is still **native Unstructured `Element`s**
(`CompositeElement` for grouped text, `Table`/`TableChunk` for tables) — parse+chunk speak the
same type; we map to the `Chunk` ORM model only at M5. `include_orig_elements=True` (default,
kept) leaves each chunk's source elements in `metadata.orig_elements` for M5 to mine
(section_title, page_number).

**Why `chunk_document` is `async` even though `chunk_by_title` is fast, pure-Python (no I/O,
no ML).** It wraps the call in `asyncio.to_thread` purely to shape the interface: the
`await chunk_document(...)` seam then never has to move when a *heavy* chunker lands later —
`by_similarity` (the locked exception strategy for flat/dense text) runs an embedding model, so
it genuinely wants offloading. Making the seam async now means adding it touches only the impl,
not the M6 orchestrator's call site. (Architect decision this session; the "build with scaling
in mind" rule applied to an interface, not a data shape.)

**The M3 requirement — `text_as_html` survives chunking.** Because Tables are isolated and
`max_characters` caps `text_as_html` too, the HTML grid rides onto the isolated Table chunk
unchanged (a table larger than the cap would split into `TableChunk`s, each still HTML). M5
stores it in `chunks.metadata` (JSONB); it never becomes the embedded text. Proven by
`test_chunk_pdf_table_keeps_text_as_html`.

**Config knobs:** `Settings.ingest_chunk_max_characters` (`= 1500`) and
`ingest_chunk_combine_text_under_n_chars` (`= 500`), both env-overridable; the M6 orchestrator
reads them and passes them to `chunk_document` (same pattern as `ingest_pdf_strategy` for parse).
`chunk_document`'s own defaults mirror those values for library callers, but the immutable test
passes `1500`/`500` explicitly so the spec never depends on config. NB the real Unstructured
param is `combine_text_under_n_chars` — CLAUDE.md's "combine_under_n_chars" is shorthand.

**`/ingest-inspect` grew a chunk view.** After the element view it prints the chunks (with the
same `[html]` marker + a `!` flag on any over-cap chunk) and a chunk summary; `--max-chars` /
`--combine` re-chunk at different sizes to tune by eye without editing config. Verified on the
table PDF: 3 elements → 2 chunks (Title+NarrativeText merged; Table isolated with `[html]`).

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
- **Table structure + AI-summary enrich (2026-07-06):** always `infer_table_structure=True`
  (PDF default `hi_res`); `text_as_html` carried through to `chunks.metadata`. A deferred
  **M-enrich** stage (between M3 and M4, gated on eval) will embed an LLM summary of each
  Table/Image chunk via a per-chunk **`embed_text`** seam (defaults to `content`, so filling it
  never reshapes M4/M5); raw stays as `content` for display/citation. Image extraction stays
  v1-deferred and rides the same path. Full text in `CLAUDE.md` → "Locked decisions" +
  memory `phase1-ingestion`.

## Still-open decisions (owed before the deeper ingestion steps)

_(none blocking M4 — the Gemini surface is now decided; see below.)_

_Resolved 2026-07-06:_ **`max_characters` = 1500** (hard cap) + **`combine_text_under_n_chars`
= 500** for M3 chunking (architect decision; the earlier ~2000 proposal was tuned down). Both
live in `config.py` and are tunable via `/ingest-inspect --max-chars/--combine`.

_Resolved 2026-07-06:_ **Gemini surface = the Gemini Developer API via the `google-genai`
SDK, API-key auth** (NOT Vertex AI). Matches the naive-first / single-tenant posture — one
`GEMINI_API_KEY` in `backend/.env` (git-ignored), no GCP project/IAM/region setup. Concretely
for M4: add `google-genai` to `requirements.txt`; the user has a key (put it in `.env` before
wiring). Still confirm the exact `task_type` (`RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY`) and
`output_dimensionality=768` param names/nesting against current `google-genai` docs at build
time. Vertex remains the surface to graduate to only for the one-time Fargate/prod touch.

_Resolved 2026-07-04:_ **first format = PDF/OCR now** (deps `unstructured[md,pdf]`; needs
poppler+tesseract for `hi_res`); **corpus = user supplies a real doc** for `/ingest-inspect`,
with a tiny committed Markdown fixture for the automated test. A full golden set (15–25 Q/A)
still grows in Phase 2.

_M2 + M3 closed (2026-07-06):_ parse's PDF `hi_res` path and M3's chunker are both proven by
automated tests (table fixture → `text_as_html` survives parse AND chunk); poppler+tesseract
confirmed installed. **Still open — lint nit in `app/models.py`:** the M0 inline comment is >88
chars, so `ruff check backend` / `black --check backend` are NOT repo-wide clean until it's
shortened. (All M2/M3 files — parse.py, chunk.py, config.py, inspect.py, test_parse.py,
test_chunk.py — are clean; `models.py` is the one blocker to a green repo-wide lint, and it
predates this session.)

## Suggested first moves next session

1. **Build M4 (embed) test-first:** write the failing embed test first (the immutable spec),
   then embed each chunk's **`embed_text`** (a per-chunk seam defaulting to `content`) with
   `gemini-embedding-2` @768d, `task_type=RETRIEVAL_DOCUMENT`, **batched** (N chunks/request +
   retry/backoff — same shape locally and at scale). The `embed_text` seam is what lets the
   deferred **M-enrich** stage overwrite table/image chunks' embedded text later without
   reshaping M4/M5. Surface is decided — **`google-genai`, API key**: add it to
   `requirements.txt`, put `GEMINI_API_KEY` in `backend/.env`, confirm the exact
   `task_type`/`output_dimensionality` param names against current SDK docs.
2. **M5 write** then maps chunks → `Chunk` ORM rows + vectors: sets `element_type`,
   `section_title`, `page_number` (mine these from `metadata.orig_elements`), and stores
   `text_as_html` in `chunks.metadata` (JSONB).
3. Keep "naive version first, end to end, then improve" — but naive-first is still **test-first**:
   the red/green net exists before the naive code, so improving it later stays safe. Slow down and
   teach on the RAG core (parse/chunk/embed).
