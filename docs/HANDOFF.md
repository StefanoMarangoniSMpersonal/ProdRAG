# Session Handoff — resume here next time

_Last updated: 2026-07-03. Read this + `CLAUDE.md` to pick up where we left off._

## Where we are

**Phase 0 is complete and verified.** A "hello world" request travels the whole stack:
Next.js page → FastAPI → Postgres/pgvector → back. Next up is **Phase 1 (ingestion)**, but
several design decisions are owed first (see "Open decisions" below).

## What exists and works

- **`/infra`** — `docker-compose.yml` runs Postgres (`pgvector/pgvector:pg16`) + Redis.
  `db/init/001_pgvector.sql` enables the `vector` extension on first boot. Redis is running
  but **not wired to any code yet** (Celery arrives with ingestion).
- **`/backend`** — FastAPI skeleton (pip + venv). Endpoints: `GET /health` (liveness) and
  `GET /health/db` (queries Postgres, confirms pgvector present). Async DB layer in
  `app/db.py` (SQLAlchemy 2.x async + asyncpg). Config via `pydantic-settings` in
  `app/config.py`. `ruff` + `black` pass clean.
- **`/frontend`** — Next.js (App Router, TypeScript, Tailwind v4). One page fetches
  `/health/db` and renders the JSON. Builds clean.
- **`README.md`** — exact run commands for all three pieces.

## Current running state (from end of last session)

Both containers were left **up and healthy**, and the two dev servers were **still running**
(API on :8000, frontend on :3000). If your machine has rebooted since, they're gone —
restart with the commands below.

### Restart the stack
```bash
cd infra && docker compose up -d                 # Postgres + Redis
cd backend && .venv\Scripts\Activate.ps1 && uvicorn app.main:app --reload   # API on :8000
cd frontend && npm run dev                        # UI on :3000
```
### Tear down
```bash
# Ctrl-C the two dev servers, then:
cd infra && docker compose down                   # add -v to also wipe the pgdata volume
```

## Decisions locked this session

- **Auth: Supabase Auth, not Clerk.** Recorded in `CLAUDE.md` (Stack + "Auth & isolation").
  Frontend will use `@supabase/ssr`; backend verifies the JWT **asymmetrically via the
  project JWKS endpoint (ES256)** — no shared secret on the backend. RLS pattern noted for
  the auth phase, including the caveat that direct SQLAlchemy access through the pooler
  **bypasses RLS** unless we set JWT claims per transaction. Also saved as a memory
  (`auth-supabase`).
- **Working style:** always define technical terms even while using them (I'm learning).
  Recorded in `CLAUDE.md` → "How I want you to work with me".

## Discussed but NOT yet locked (decide next session)

- **Retrieval implementation approach.** We talked through why LangChain's
  `db.as_retriever()`, `BM25Retriever`, and `EnsembleRetriever` don't fit the locked hybrid
  design (in-memory keyword index, RLS-blind, own schema). **My recommendation:** own the
  `chunks` table and hand-write the hybrid retrieval SQL, using LangChain thinly only for
  embeddings + reranker. **You haven't ratified this yet** — it's the first thing to confirm.
- **Keyword-search engine (open sub-decision):** native Postgres full-text (`tsvector` +
  `ts_rank_cd`) vs the `pg_search`/ParadeDB extension for true BM25. I offered to lay out
  the trade before we design the schema — pick up here.

## Open decisions owed before ingestion can be built

(From `CLAUDE.md` — these are yours to make as architect.)

1. **Database schema** — `documents` / `chunks` tables, the 768-dim `vector` embedding
   column + HNSW index, the `tsvector` keyword column, and what metadata each chunk carries.
2. **How a document enters the system** — upload API, which formats first, and where raw
   files live (S3 vs local disk for dev).
3. **Unstructured: hosted API vs local library** (parsing/OCR).
4. **A concrete `max_characters` value** for chunking (must stay under ~8,192 tokens).
5. **Corpus + golden set** — real docs + 15–25 Q/A pairs in `/eval/golden.jsonl`.
6. **Gemini API access + secrets** provisioned so ingestion can embed.

## Suggested first moves next session

1. Ratify (or adjust) the retrieval approach + pick the keyword engine.
2. Design the `documents`/`chunks` schema together (I'll teach each column choice).
3. Then scaffold ingestion the naive way first, per the project's "naive version first" rule.
