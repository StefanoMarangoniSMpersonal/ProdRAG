# CLAUDE.md — Project Constitution

> This file is read by Claude Code at the start of every session. It is the single
> source of truth for how this repo works. Keep it short, true, and current.
> When a decision below is still `TODO`, that decision must be made (by me, the human)
> before code depends on it.

## Project

A production-oriented **RAG system**, built as a solo learning project. Not deployed at
scale and single-tenant, but engineered with production patterns so I learn every layer
properly. **The goal is understanding, not just a working demo.**

**Build with scaling in mind.** The system will not *operate* at scale, but every layer must
be *shaped* so it could: the data model, keys, tenancy columns, and interfaces take their
scaled form now, while implementations stay naive/local behind seams — so scaling later is a
drop-in swap, never a reshape of data or callers.

## How I want you (Claude Code) to work with me

This is a **learn-by-building** project. You are the implementer and tutor; **I am the
architect**. Therefore:

- **Explain non-obvious choices as you make them, and name the alternatives you rejected
  and why.** A silent correct implementation is worth less to me than a slightly slower
  one I understand.
- **I'm learning — always define the technical terms you use.** Don't avoid jargon (I need
  to learn the real vocabulary), but the first time a technical word or acronym shows up in
  an explanation, briefly say what it means, even if it costs a few extra tokens. Assume I
  may not already know it rather than assuming I do.
- **For any non-trivial change, use plan mode first.** Propose the plan, wait for me to
  read and adjust it, then implement. Do not jump straight to large edits.
- **I make the architecture/design decisions** (chunking strategy, embedding model, when
  to rerank, graph structure, schema). You implement what I've decided. If a design
  decision is required and isn't recorded here, ask — don't silently pick one.
- On boring/glue work (scaffolding, config, wiring, frontend components), move fast.
  On the RAG core (ingestion, retrieval, evaluation), slow down and teach.
- When you implement something in the RAG core, prefer the **naive version first**, get
  it working end to end, then we improve it together. Don't over-engineer up front.

## Stack (fixed)

- **Frontend:** Next.js (App Router) · TypeScript · Tailwind · Supabase Auth (auth)
- **Backend:** Python · FastAPI (async) · Celery + Redis (task queue / broker / cache)
- **Data:** PostgreSQL + pgvector · Supabase (host) · AWS S3 (blob storage)
- **AI/ML:** Gemini (LLM) · LangChain (used *thinly*) · LangGraph (orchestration) ·
  Unstructured (parsing/OCR) · Guardrails AI (I/O validation)
- **Infra:** Docker / docker-compose (local) · AWS Fargate (one-time deploy only) · GitHub
- **Observability:** Sentry (errors) · LangSmith (LLM tracing/eval) · structured logging

## Locked decisions (upstream of the schema — settled; revisit only with eval evidence)

- **Document processing:** **Unstructured** for parsing/OCR. It partitions every format
  (PDF, DOCX, HTML, PPTX, Markdown, email) into typed elements before chunking, so the
  chunking strategy below is format-agnostic.
- **Chunking strategy:** Unstructured **`by_title`** as the universal default across ALL
  formats (it respects section boundaries regardless of source type). Settings:
  - `combine_under_n_chars = 500` — merges fragments when a short line is mis-detected as
    a Title (the main knob to tune).
  - `max_characters` capped so no chunk exceeds the embedding input limit (see below);
    Tables are always isolated by Unstructured — expect them as standalone chunks.
  - Exceptions, applied per-source ONLY when `/ingest-inspect` shows `by_title` failing:
    `by_page` for page-as-unit docs (slide decks, scanned forms); `by_similarity` for
    flat/dense text with no headings (transcripts, plain .txt, scraped articles).
- **Embedding model:** **`gemini-embedding-2`** with **`output_dimensionality = 768`**.
  - Why 768 (not the 3072 default): pgvector's HNSW index caps at **2000 dims** on the
    standard `vector` type, so 3072 can't be HNSW-indexed. 768 fits, saves storage, and is
    Google's recommended starting point (scale to 1536 later only if eval demands it).
  - **Asymmetric task types (mandatory):** `RETRIEVAL_DOCUMENT` at ingest,
    `RETRIEVAL_QUERY` at query time. On the Gemini API pass via the `task_type` config;
    on the Vertex multimodal endpoint pass it as a prompt instruction instead — confirm
    against current docs for the surface we call.
  - **Input cap:** ~8,192 tokens per input — `max_characters` above must stay under this.
  - Model is multimodal (text/image/doc/audio/video → one space). Not used in v1, but
    leaves the door open to embedding page images for scanned/image-heavy PDFs later.
- **Retrieval:** hybrid (pgvector semantic + Postgres full-text/BM25) fused with
  **Reciprocal Rank Fusion**, then **cross-encoder rerank** down to 5–10 chunks.
- **Vector index:** pgvector **HNSW** (not IVFFlat), on a 768-dim `vector` column.
- **Auth & isolation:** **Supabase Auth (GoTrue)** issues the JWT — no third-party identity
  provider. Frontend uses **`@supabase/ssr`** in the App Router (cookie sessions +
  middleware token refresh); client-side uses the **publishable** key, never `service_role`.
  Backend (FastAPI) verifies the JWT **asymmetrically via the project JWKS endpoint (ES256)**
  — no shared secret held by the backend.
  - **RLS (learn-once goal):** single-tenant for now, but implement the per-user **Row-Level
    Security** pattern once so I understand it. Policies use `(select auth.uid()) = <owner>`
    with `TO authenticated`; UPDATE policies need both `USING` and `WITH CHECK`; keep
    authorization data in **`app_metadata`**, never the user-editable `user_metadata`.
  - **Pooler nuance to revisit at build time:** the backend hits Postgres directly via
    SQLAlchemy/asyncpg through Supavisor (privileged role), which **bypasses RLS** unless we
    connect as a role subject to RLS and set the request's JWT claims per transaction. This
    is the mechanism worth learning — flag it, don't solve it now.

## Conventions

- Python: async everywhere; **Pydantic** models for all API and LLM structured I/O; type
  hints required. Format with `ruff` / `black`. No secrets in code — everything via env.
- DB access under Celery goes through the **Supabase pooler (Supavisor), transaction
  mode** — never direct connections (they exhaust Postgres under worker load).
- LLM/prompt rule: instruct Gemini to answer **only from provided context** and to say it
  doesn't know when the context lacks the answer. Return **citations** to source spans.
- Log every RAG query: user message, retrieved chunk IDs, reranked order, final context,
  the answer, and token/cost usage. This log is the raw material for evaluation.
- Anything with side effects (deploys, destructive migrations) is **manual-invoke only** —
  never auto-run it because the code "looks ready."
- **Ingestion is job-shaped, not request-shaped** (a consequence of "build with scaling in
  mind"): the orchestrator (M6) is a self-contained coroutine keyed on `document_id` that
  returns nothing to a waiting caller; the upload endpoint (M7) persists the file, inserts the
  `documents` row as `pending`, kicks off that job, and returns **202 + the doc id** — the
  client polls `status` until `ready`. Runs synchronously now (naive-first), but structured as
  enqueue-and-process so adding Celery is a one-line swap (`await orchestrate(id)` →
  `orchestrate.delay(id)`), never a rewrite of the endpoint or the client contract.
- Keep LangChain thin: use it for integrations, let **LangGraph** own control flow.

## Commands

> Fill in as they stabilize; keep this section accurate so you don't re-derive them.

```bash
# Local stack
docker compose up -d           # Postgres+pgvector, Redis, (optional) localstack for S3
# Backend
cd backend && uvicorn app.main:app --reload
celery -A app.worker worker -l info
# Frontend
cd frontend && npm run dev
# Quality
ruff check backend && black --check backend
pytest                          # unit + integration
# Eval (see /eval-run skill)
python -m eval.run --golden eval/golden.jsonl
```

## Repo layout

```
/backend   FastAPI app, Celery workers, RAG core (ingest / retrieve / generate)
/frontend  Next.js app
/infra     docker-compose, IaC (Terraform/CDK) for the one-time Fargate deploy
/eval      golden set (golden.jsonl) + RAGAS harness
/docs      decisions (ADRs), notes
.claude    agents + skills for Claude Code (this project's tooling)
```

## Out of scope for v1 (do not build unless I ask)

Multi-region / HA, load testing, full B2B org/team management, autoscaling tuning,
compliance (SOC2/HIPAA/GDPR). Touch Fargate exactly once to learn it, then tear it down.
