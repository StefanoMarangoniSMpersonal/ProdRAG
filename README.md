# ProdRAG

A production-patterned **RAG** system, built solo as a deep learning project. The goal is
**understanding every layer**, not just shipping a demo — it's single-tenant and not deployed
at scale, but engineered with production patterns (async throughout, hybrid retrieval,
evaluation-as-a-substrate, observability) so each piece is learned properly.

> **RAG** (Retrieval-Augmented Generation): answer a question by first *retrieving* the most
> relevant passages from your own documents, then having an LLM write a **grounded, cited**
> answer from only those passages.

See [`CLAUDE.md`](./CLAUDE.md) for the project constitution (the rules this repo is built by)
and [`rag-system-architecture.md`](./rag-system-architecture.md) for a full component-by-component
walkthrough.

---

## Status

| Phase | Scope | State |
|------|-------|-------|
| **Phase 0** | Stack spine — Next.js → FastAPI → Postgres/pgvector health path travels end to end | ✅ done |
| **Phase 1 · M0–M7** | Ingestion path — schema → storage → parse → chunk → embed → write → orchestrator → upload endpoint (`POST /documents` → 202 + poll) | ✅ done |
| **Phase 1 · M8** | Async layer — Celery worker over Redis + fenced stuck-job reaper; **proven live** end to end (upload → worker → `ready`) | ✅ done |
| **Phase 2** | Query pipeline — hybrid retrieve → RRF fusion → rerank → grounded answer + eval harness | ⏳ next |

Redis is now **wired** — it's the Celery broker for the ingestion worker (`backend/app/worker.py`).
A file uploaded to `POST /documents` is parsed → chunked → embedded → written to `chunks` by the
worker off the queue; the client polls `GET /documents/{id}` until `status` = `ready`.

For a layer-by-layer completion breakdown (the 10 architectural layers, each scored code-only vs.
code + design), see [`docs/PROGRESS.md`](./docs/PROGRESS.md) — the living development tracker.

---

## Architecture

A RAG system is two pipelines sharing one datastore:

```
 Ingestion (offline, once per doc)          Query (real-time, every question)
 ─────────────────────────────────          ─────────────────────────────────
 upload → store raw → parse → chunk          question → embed → hybrid retrieve
        → embed → write chunks+vectors                 → RRF fuse → rerank
                     │                                 → grounded answer + citations
                     ▼                                          ▲
             ┌───────────────────────────────────────────────────────┐
             │      PostgreSQL + pgvector   ·   raw files (disk→S3)    │
             └───────────────────────────────────────────────────────┘
```

**One-line mental model:** *ingestion turns documents into searchable meaning; query turns a
question into the right pieces of that meaning plus a grounded answer.*

---

## Tech stack

Legend: **wired** = in code today · *planned* = decided, arrives in a later phase.

| Layer | Choice |
|------|--------|
| Frontend | **Next.js (App Router) · TypeScript · Tailwind** · *Supabase Auth* |
| Backend | **Python · FastAPI (async) · Celery + Redis** |
| Data | **PostgreSQL + pgvector** · *Supabase (host)* · *AWS S3 (raw files; local disk in dev)* |
| AI/ML | *Gemini (LLM)* · **`gemini-embedding-2` @ 768d** · **Unstructured (parse/OCR)** · *LangGraph (orchestration)* · *Guardrails* |
| Infra | **Docker / docker-compose** · *AWS Fargate (one-time)* · GitHub |
| Observability | *Sentry* · *LangSmith* · structured logging |

---

## Prerequisites

- Docker + Docker Compose
- Python 3.12+
- Node 18+ / npm
- **For parsing PDFs with OCR/layout (the `hi_res` / `ocr_only` strategies) —** two
  system binaries on your PATH: **poppler** (renders PDF pages to images) and **tesseract**
  (the OCR engine). On Windows: `choco install poppler tesseract`. Not needed for text
  formats (`.md`/`.txt`/`.html`), for digital PDFs via `--strategy fast`, or to run `pytest`.

## Quick start (one command, Windows/PowerShell)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env files, DB migrations), then starts every piece
.\stop.ps1    # stops the Postgres/Redis containers
```

`dev.ps1` is idempotent: the first run also does first-time setup, applies DB migrations, and
opens **four** windows each with live logs — the **API**, the **Celery worker**, **Celery Beat**
(the stuck-job reaper scheduler), and the **frontend**; later runs just restart the stack. Use
`.\dev.ps1 -Reinstall` to force-reinstall dependencies. The manual steps below are the same thing
broken out — useful when a step fails or on macOS/Linux.

> **Windows note:** the worker runs `--pool=solo` (Celery's default prefork pool is broken on
> Windows) and **Beat runs as its own process** — embedded Beat (`-B`) errors with "does not work
> on Windows", so worker and Beat are two separate windows. Also use **`curl.exe`** (not bare
> `curl`, which PowerShell aliases to `Invoke-WebRequest`) for file uploads with `-F`.

## Run it manually (4 pieces)

**1. Infra (Postgres + pgvector, Redis)**
```bash
cd infra && docker compose up -d          # both containers should report healthy
# apply the schema against the running DB (PowerShell):
.\db\migrations\apply-migrations.ps1
```
`infra/db/init/001_pgvector.sql` enables the `vector` extension on first boot; the schema itself
lives in `infra/db/migrations/` (see **Database & migrations** below).

**2. Backend (FastAPI)**
```bash
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # Windows: copy .env.example .env  — then fill in values
uvicorn app.main:app --reload             # http://localhost:8000
```
Check: `curl http://localhost:8000/health` → `{"status":"ok"}` ·
`curl http://localhost:8000/health/db` → postgres + pgvector versions.

**3. Worker (Celery) + Beat (reaper)** — consumes ingestion jobs off Redis. Two processes,
from the same `backend/` venv (so they load the same `.env`, incl. `GEMINI_API_KEY`):
```bash
celery -A app.worker worker -l info --pool=solo    # Windows: --pool=solo is required
celery -A app.worker beat -l info                  # separate process: -B is unsupported on Windows
```
On macOS/Linux you can fuse them with `celery -A app.worker worker -l info -B` instead.

**4. Frontend (Next.js)**
```bash
cd frontend
npm install
cp .env.local.example .env.local           # Windows: copy .env.local.example .env.local
npm run dev                                # http://localhost:3000
```
Open <http://localhost:3000> — the page renders the live JSON from FastAPI. That page load is
the Phase 0 "hello world" crossing the whole stack.

---

## Database & migrations

Schema is managed as **plain numbered SQL** (no ORM migration tool yet):

- DDL lives in `infra/db/migrations/` (`002_schema.sql` is the first schema). It's idempotent
  (`IF NOT EXISTS`) and applied against the **running** database by
  `infra/db/migrations/apply-migrations.ps1` (also auto-run by `dev.ps1`).
- This is **separate** from `infra/db/init/`, which only runs once on a fresh volume — so
  evolving the schema never requires wiping your data.
- `backend/app/models.py` holds SQLAlchemy models that mirror the SQL for typed access. There's
  no autogeneration yet, so **SQL and models are kept in sync by hand**. (We'll adopt Alembic
  later once the schema churns enough to justify it.)

---

## Configuration & secrets

- **No secrets are committed.** All config is read from environment via `pydantic-settings`.
- Copy the templates and fill them in locally — the real files are git-ignored:
  - `backend/.env`      ← `backend/.env.example`      (`DATABASE_URL`, `CORS_ORIGINS`)
  - `frontend/.env.local` ← `frontend/.env.local.example` (`NEXT_PUBLIC_API_URL`)
- The local Postgres password (`prodrag`) in `docker-compose.yml` is a **dev-only default** for
  the disposable local container — not a real secret. Production/Supabase credentials will come
  from env, never a hardcoded default.

---

## Project layout

```
backend/    FastAPI app + RAG core (app/models.py = ORM; app/db.py, app/config.py, app/main.py)
frontend/   Next.js app (App Router)
infra/      docker-compose.yml; db/init (extension) + db/migrations (schema + runner)
docs/       HANDOFF.md (session state) · PROGRESS.md (layer-by-layer tracker) · decisions
eval/        golden set + eval harness            (arrives in Phase 1/2)
.claude/    Claude Code agents + skills for this repo
CLAUDE.md   project constitution · rag-system-architecture.md · SETUP.md
dev.ps1 / stop.ps1   one-command local dev
```

## Quality

```bash
cd backend
pip install -r requirements-dev.txt
ruff check . && black --check .
```

---

*Solo learning project — single-tenant by design. Not intended for production reuse as-is.*
