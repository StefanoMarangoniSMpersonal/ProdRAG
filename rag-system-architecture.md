# RAG System — Architecture & Component Reference

A complete walkthrough of the stack: what each component is, why it's here, what it does,
and its trade-offs — followed by the two pipelines (ingestion and query) that tie
everything together, with the exact point each component enters.

This is a **single-tenant, production-oriented learning build**, so a few components
(Clerk, Fargate, ELK) are included to learn the pattern rather than because scale demands
them. That context matters when reading the trade-offs below.

---

## 1. The system at a glance

A RAG system is really **two pipelines** that share one datastore:

- **Ingestion (offline / async):** documents come in, get parsed, chunked, embedded, and
  stored. Runs in the background, once per document.
- **Query (online / real-time):** a user question comes in, the system retrieves the most
  relevant chunks, and the LLM writes a grounded, cited answer. Runs on every message.

```
                              ┌─────────────────────────────────────────────┐
                              │                 FRONTEND                     │
                              │   Next.js + TypeScript + Tailwind + Clerk    │
                              └───────────────┬─────────────────────────────┘
                                              │  HTTPS (auth'd requests)
                              ┌───────────────▼─────────────────────────────┐
                              │                 BACKEND API                  │
                              │                  FastAPI                     │
                              └───────┬───────────────────────┬─────────────┘
                                      │                       │
                 upload / ingest      │                       │  ask question
                                      ▼                       ▼
        ┌──────────────────────────────────┐   ┌──────────────────────────────────────┐
        │      INGESTION (async)           │   │        QUERY (real-time)             │
        │  Celery workers + Redis broker   │   │   LangGraph workflow orchestration   │
        │                                  │   │                                      │
        │  S3 (raw file)                   │   │  Guardrails (input)                  │
        │  Unstructured (parse/OCR)        │   │  query embed (gemini-embedding-2, Q) │
        │  chunk by_title                  │   │  hybrid retrieve (pgvector + BM25)   │
        │  embed (gemini-embedding-2, DOC) │   │  RRF fusion → cross-encoder rerank   │
        │  write chunks + vectors ─────────┼──►│  Gemini LLM (answer + citations)     │
        │                                  │   │  Guardrails (output)                 │
        └───────────────┬──────────────────┘   └──────────────────┬───────────────────┘
                        │                                          │
                        ▼                                          ▼
        ┌───────────────────────────────────────────────────────────────────────────┐
        │                        DATA & STORAGE                                      │
        │   PostgreSQL + pgvector  (on Supabase)          AWS S3 (raw documents)     │
        └───────────────────────────────────────────────────────────────────────────┘

   Cross-cutting, everywhere:  Docker (packaging) · GitHub (VCS/CI) · Fargate (deploy)
   Observability:  Sentry (errors) · ELK (logs) · LangSmith (LLM traces + eval)
```

The rest of this document explains every box, then walks the two pipelines step by step.

---

## 2. Frontend

### Next.js
**What it is.** A React framework for building web apps, with server-side rendering,
routing, and streaming built in (App Router).
**Role here.** Serves the chat UI and document-management screens; streams the LLM's answer
to the user token by token; talks to the FastAPI backend.
**Pros.** First-class streaming support (essential for a chat product), server components,
strong ecosystem, easy deployment. A natural fit for RAG specifically because of streaming.
**Limitations.** More moving parts than a plain single-page app; the App Router has a
learning curve; some features assume a Node hosting environment.

### TypeScript
**What it is.** JavaScript with static types.
**Role here.** The language for all frontend code; catches type errors before runtime and
makes the API contract between frontend and backend explicit.
**Pros.** Fewer runtime bugs, better editor autocomplete, safer refactors, self-documenting.
**Limitations.** Extra compile step and some type-wrangling overhead; near-zero real downside
for a project this size.

### Tailwind CSS
**What it is.** A utility-first CSS framework — you style with small classes in your markup.
**Role here.** All styling for the UI.
**Pros.** Fast to build with, consistent spacing/design tokens, no context-switching between
files, small production CSS.
**Limitations.** Markup gets verbose ("class soup"); a learning curve if you're used to
traditional CSS.

### Clerk
**What it is.** A managed authentication/identity provider (sign-in, sessions, MFA, orgs).
**Role here.** Handles user auth; every request to the backend carries a Clerk-issued token
identifying the user.
**Pros.** Fast to integrate, handles the hard/risky parts of auth (sessions, MFA, security)
so you don't build them, good developer experience.
**Limitations.** It's a third-party dependency with its own cost/lock-in, **and it doesn't
integrate natively with Supabase's Postgres Row-Level Security** — bridging Clerk's tokens
into Supabase RLS (for per-user data isolation) is extra plumbing and a permanent seam.
This is the one frontend choice with a real architectural cost in this stack.

---

## 3. Backend

### Python
**What it is.** The backend language.
**Role here.** Runs the API, the Celery workers, and all the AI/RAG logic.
**Pros.** The default language for AI/ML — every library you need (LangChain, Unstructured,
embeddings SDKs) is Python-first. Readable, huge ecosystem.
**Limitations.** Slower than compiled languages (rarely the bottleneck here — the LLM and DB
dominate latency); packaging/dependency management can be fiddly.

### FastAPI
**What it is.** A modern, async Python web framework for building APIs.
**Role here.** Exposes the HTTP endpoints the frontend calls (upload, ask, list documents);
streams responses; validates request/response bodies.
**Pros.** Async-native (great for streaming and concurrent LLM/DB calls), automatic
validation and API docs via Pydantic, excellent performance for Python, minimal boilerplate.
**Limitations.** Younger than heavyweight frameworks; you assemble your own conventions (no
"batteries included" admin/ORM like Django).

### Celery
**What it is.** A distributed task queue — runs slow jobs in the background, outside the
request/response cycle.
**Role here.** Runs document ingestion (parse → OCR → chunk → embed → store) asynchronously,
so an upload returns immediately and the heavy work happens in workers.
**Pros.** Battle-tested, mature, handles retries/scheduling/scaling of background work,
large ecosystem.
**Limitations.** Heavyweight and built around a synchronous worker model, which sits slightly
awkwardly next to an otherwise fully-async FastAPI app. Lighter async-native alternatives
exist (ARQ, Dramatiq) — but Celery's maturity is a real advantage and worth it here.

### Redis
**What it is.** An in-memory data store, used as a cache and a message broker.
**Role here.** Two jobs: (1) the **broker** that carries tasks between FastAPI and Celery
workers; (2) a **cache** — including, later, a *semantic cache* for LLM/embedding calls to
cut latency and cost.
**Pros.** Extremely fast, simple, does double duty (broker + cache), ubiquitous.
**Limitations.** In-memory means data is volatile unless you configure persistence; you must
watch memory usage; it's another service to run.

---

## 4. Data & Storage

### PostgreSQL
**What it is.** A mature, powerful relational (SQL) database.
**Role here.** The system of record — users, documents, chunks, metadata, and (via pgvector)
the embeddings all live here.
**Pros.** Rock-solid, ACID transactions, rich SQL, huge ecosystem, extensible.
**Limitations.** Vertical-scaling model (one primary); connection limits matter under many
concurrent workers (hence the pooler note below).

### pgvector
**What it is.** A PostgreSQL extension that adds a vector data type and similarity search
(nearest-neighbor) directly inside Postgres.
**Role here.** Stores chunk embeddings and runs the semantic-search half of retrieval, right
next to your relational data.
**Pros.** Vectors live with your business data — one system to run, transactional
consistency, and you can filter vector search with normal SQL `WHERE` clauses (metadata
filtering). The right default for most production RAG.
**Limitations.** Not as fast as dedicated vector engines (Qdrant/Weaviate/Milvus) at very
large scale (tens of millions of vectors). **Its HNSW index caps at 2000 dimensions on the
standard `vector` type** — which is exactly why this build truncates the 3072-dim Gemini
embeddings to 768. Use **HNSW** indexing, not IVFFlat.

### Supabase
**What it is.** A managed platform built around Postgres (hosted DB + auth + storage + APIs).
**Role here.** Hosts the PostgreSQL/pgvector database so you don't operate it yourself.
**Pros.** Fast to start, managed Postgres with pgvector available, built-in auth and Row-Level
Security, generous free tier.
**Limitations.** Managed-service constraints and cost as you grow; **its native auth/RLS
integration is bypassed if you use Clerk** (see Clerk); under Celery load you must connect
through the Supabase pooler (Supavisor) in transaction mode, or you'll exhaust connections.

### AWS S3
**What it is.** Object (blob) storage for files.
**Role here.** Stores the raw uploaded documents (the originals), separate from the DB.
**Pros.** Cheap, durable, effectively unlimited, the industry standard; keeps large binaries
out of your database.
**Limitations.** AWS-specific config/IAM to get right; eventual-consistency and egress-cost
nuances; one more account/service to manage.

---

## 5. AI / ML

### Gemini (LLM)
**What it is.** Google's large language model family — the generation engine.
**Role here.** Takes the retrieved chunks + the user's question and writes the final answer,
grounded in that context and with citations.
**Pros.** Very large context window (generous retrieval budgets), competitive pricing, native
multimodality (useful for documents with images/tables), strong quality.
**Limitations.** Vendor lock-in (mitigated by keeping the LangChain abstraction clean);
API-based means per-call cost and dependence on an external service.

### Embeddings — `gemini-embedding-2`
**What it is.** The model that turns text (and, here, potentially images) into vectors — the
numeric representations that make semantic search possible. Largely determines retrieval
quality.
**Role here.** Embeds each chunk at ingestion (`RETRIEVAL_DOCUMENT` task type) and each user
query at search time (`RETRIEVAL_QUERY`), at **768 dimensions** (truncated from the 3072
default via Matryoshka Representation Learning to fit the pgvector HNSW limit).
**Pros.** Same ecosystem as the Gemini LLM; multimodal and unified vector space; flexible
dimensions via MRL; asymmetric query/document modes that measurably improve retrieval.
**Limitations.** ~8,192-token input cap (chunks must stay under it); API cost and dependency;
choosing the wrong dimensionality is a real quality/storage trade-off you must validate.

### Unstructured
**What it is.** A document-processing library that turns messy files (PDF, DOCX, HTML, PPTX,
etc.) into clean, typed elements (Title, NarrativeText, Table, ListItem…) — including OCR.
**Role here.** The first step of ingestion: parse each uploaded file into elements that the
chunker can then split intelligently.
**Pros.** Handles many formats with one API; produces structure (element types) that enables
`by_title` chunking; includes OCR.
**Limitations.** Can be slow and resource-hungry; quality on complex layouts (dense tables,
multi-column) varies; open-source vs hosted-API versions differ. Document-parsing quality is
upstream of everything, so it's worth benchmarking against alternatives (Docling, LlamaParse,
AWS Textract) on your real documents.

### Chunking — `by_title` (via Unstructured)
**What it is.** A chunking *strategy*, not a separate tool — it groups Unstructured's elements
into chunks while respecting section boundaries.
**Role here.** Turns parsed elements into retrieval-sized chunks. Used as the format-agnostic
default; `by_page` / `by_similarity` are exceptions for documents without headings.
**Pros.** Keeps each chunk within a single section (no mixing topics), which improves
retrieval precision; format-independent because it works on element types.
**Limitations.** Depends on partitioning correctly detecting Titles — short lines can be
mis-tagged as headings, fragmenting chunks (mitigated with `combine_under_n_chars`). Flat,
heading-less documents need a different strategy.

### LangChain
**What it is.** A framework of building blocks and integrations for LLM apps (loaders,
retrievers, model wrappers, chains).
**Role here.** Provider abstraction and integrations — used **thinly**, not as the owner of
core control flow.
**Pros.** Superb for prototyping; huge integration catalog; abstracts away provider APIs so
swapping models is cheap.
**Limitations.** Heavy abstractions can be hard to debug and reason about under load; frequent
breaking changes; "magic" that hides what's happening. The mature pattern is to lean on it
lightly and let LangGraph own the flow.

### LangGraph
**What it is.** A library for building LLM workflows as explicit state machines / graphs
(nodes and edges), rather than opaque chains.
**Role here.** Orchestrates the query pipeline: input-guard → query-process → retrieve →
rerank → generate → output-guard, as inspectable steps.
**Pros.** Explicit, debuggable, controllable; ideal for multi-step / agentic RAG; native
pairing with LangSmith. The right production direction.
**Limitations.** More upfront structure than a simple chain; another concept to learn — but
the debuggability pays off as soon as the flow has branches.

### Guardrails AI
**What it is.** A library that validates and constrains LLM inputs and outputs (PII checks,
structure/format validation, safety rules).
**Role here.** Checks the user's input and the model's output on the way through the LangGraph
flow.
**Pros.** Adds a safety/validation layer; enforces output structure; catches PII and unwanted
content.
**Limitations.** Every check adds latency and sits in the critical path; added complexity — be
deliberate about which checks are worth the milliseconds. (Alternatives: NeMo Guardrails,
Llama Guard.)

---

## 6. Deployment & Infrastructure

### Docker
**What it is.** Containerization — packages each service with its dependencies into a portable
image.
**Role here.** Runs the whole stack locally via docker-compose (Postgres, Redis, backend,
frontend) and packages services for deployment.
**Pros.** "Works on my machine" becomes "works everywhere"; reproducible environments; local
stack matches production.
**Limitations.** A learning curve; image size/build times to manage; adds a layer between you
and the raw process.

### AWS Fargate
**What it is.** A serverless way to run containers on AWS — no servers/clusters to manage.
**Role here.** The one-time cloud deployment target (you deploy once to learn it, then tear
it down).
**Pros.** No cluster management, scales with load, pay-for-what-you-use, good for variable
traffic.
**Limitations.** **No GPU support** (fine while all models are API-based, but blocks
self-hosting embeddings/rerankers later); more expensive than reserved EC2 at sustained high
load; watch for lingering billable resources (NAT gateways, load balancers) after teardown.

### GitHub
**What it is.** Git hosting + collaboration + CI/CD (Actions).
**Role here.** Version control for all code; runs automated checks/tests and (optionally)
deploys via Actions.
**Pros.** Industry standard, great tooling, free for this scale, CI/CD built in.
**Limitations.** None material for this project; just remember to keep secrets out of the repo.

---

## 7. Monitoring & Observability

### Sentry
**What it is.** Error and exception tracking.
**Role here.** Captures backend (and frontend) errors with stack traces and context so you
know when something breaks and why.
**Pros.** Easy to integrate, rich error context, good free tier, alerts.
**Limitations.** Focused on errors — not metrics/latency (APM) or full logs; another service.

### ELK stack (Elasticsearch, Logstash, Kibana)
**What it is.** A logging stack: Elasticsearch stores/searches logs, Logstash ingests them,
Kibana visualizes them.
**Role here.** Centralized, searchable application logs.
**Pros.** Very powerful log search and dashboards; industry-proven.
**Limitations.** **Heavy to self-host and operate** — Elasticsearch is resource-hungry and
becomes a system you maintain, which is a lot for a solo project. Managed alternatives
(Grafana Loki, Datadog, BetterStack, Axiom, Elastic Cloud) are worth weighing; here it's
mainly included to learn the pattern.

### LangSmith
**What it is.** Observability and evaluation built specifically for LLM apps (traces, prompt
inspection, eval datasets).
**Role here.** Traces every step of the LangGraph pipeline — what was retrieved, what prompt
went to Gemini, what came back — and backs your evaluation loop.
**Pros.** Purpose-built for LLM debugging; native LangChain/LangGraph integration; makes the
otherwise-opaque LLM flow inspectable; supports eval.
**Limitations.** Another (partly paid) service; tracing is not the same as evaluation — you
still need an eval framework (e.g. RAGAS) on top for measuring quality.

---

## 8. The ingestion pipeline (offline / async)

Runs once per document, in the background. Numbers show the order; the component doing the
work is named at each step.

```
[1] User uploads a document
      Next.js UI (auth'd by Supabase)
                │
                ▼
[2] Receive upload, return immediately
      FastAPI endpoint
                │
                ├──────────────► [3] Store the raw original file
                │                      AWS S3
                │
                ▼
[4] Enqueue an ingestion job
      Redis (broker) ──► Celery worker picks it up
                │
                ▼
[5] Parse the file into typed elements (+ OCR if needed)
      Unstructured
                │
                ▼
[6] Chunk the elements, respecting section boundaries
      by_title strategy   (combine_under_n_chars=500; size < embedding input cap)
                │
                ▼
[7] Embed each chunk into a 768-dim vector
      gemini-embedding-2   (task_type = RETRIEVAL_DOCUMENT)
                │
                ▼
[8] Store chunks + vectors + metadata
      PostgreSQL + pgvector (on Supabase)   → HNSW index for fast search later
                │
                ▼
[9] Mark document "ready"; UI shows processing complete

  Throughout: LangSmith may trace the steps · Sentry captures failures · logs → ELK
```

Result: the document's content is now searchable as embedded chunks.

---

## 9. The query pipeline (real-time)

Runs on every user question. This is the flow **LangGraph orchestrates** as explicit nodes.

```
[1] User asks a question
      Next.js chat UI (auth'd by Supabase), expecting a streamed answer
                │
                ▼
[2] Receive the question
      FastAPI endpoint  ──►  hand off to the LangGraph workflow
                │
                ▼
      ┌───────────────────── LangGraph workflow ─────────────────────┐
      │                                                              │
      │ [3] Validate input (PII, safety, format)                     │
      │       Guardrails AI                                          │
      │                │                                             │
      │                ▼                                             │
      │ [3.5] (optional) check a semantic cache for a known answer   │
      │       Redis                                                  │
      │                │                                             │
      │                ▼                                             │
      │ [4] Process/rewrite the query, then embed it                 │
      │       gemini-embedding-2  (task_type = RETRIEVAL_QUERY, 768d)│
      │                │                                             │
      │                ▼                                             │
      │ [5] Hybrid retrieve candidate chunks                         │
      │       pgvector (semantic)  +  Postgres full-text / BM25      │
      │                │                                             │
      │                ▼                                             │
      │ [6] Fuse the two result lists                                │
      │       Reciprocal Rank Fusion (RRF)                           │
      │                │                                             │
      │                ▼                                             │
      │ [7] Rerank and keep only the best 5–10                       │
      │       cross-encoder reranker                                 │
      │                │                                             │
      │                ▼                                             │
      │ [8] Assemble context + prompt, generate the answer           │
      │       Gemini LLM  (answer ONLY from context, with citations) │
      │                │                                             │
      │                ▼                                             │
      │ [9] Validate output (grounding, safety, format)              │
      │       Guardrails AI                                          │
      │                                                              │
      └──────────────────────────────┬───────────────────────────────┘
                                     │
                                     ▼
[10] Stream the answer + citations back to the user
       FastAPI (streaming) ──► Next.js UI

  Throughout: LangSmith traces the whole graph · Sentry logs errors · ELK stores logs
  · token/cost usage recorded · every query logged (chunk IDs, order, context, answer)
```

Result: a grounded, cited answer, with a full trace of how it was produced.

---

## 10. Where each component enters — quick map

| Component | Ingestion | Query | Always-on / cross-cutting |
|---|---|---|---|
| Next.js / TypeScript / Tailwind | upload UI | chat UI, streaming | — |
| FastAPI | receives upload | receives question, streams | API surface |
| Celery + Redis | runs ingestion jobs | (Redis) semantic cache | queue + cache |
| AWS S3 | stores raw file | — | blob storage |
| Unstructured | parse / OCR | — | — |
| `by_title` chunking | split into chunks | — | — |
| gemini-embedding-2 | embed chunks (DOC) | embed query (QUERY) | — |
| PostgreSQL + pgvector | write chunks/vectors | semantic + BM25 search | system of record |
| Supabase | hosts the DB | hosts the DB | managed Postgres |
| RRF + reranker | — | fuse + rerank candidates | — |
| Gemini LLM | — | generate answer | — |
| Guardrails AI | — | validate in/out | safety |
| LangGraph | — | orchestrates the flow | — |
| LangChain | integrations | integrations | thin abstraction |
| Docker | packaging | packaging | environments |
| GitHub | — | — | VCS + CI/CD |
| AWS Fargate | — | — | deployment host |
| Sentry | error capture | error capture | error tracking |
| ELK | logs | logs | log storage/search |
| LangSmith | trace ingestion | trace query + eval | LLM observability |

---

## 11. The one-line mental model

**Ingestion turns documents into searchable meaning; query turns a question into the right
pieces of that meaning plus a grounded answer.** Everything in the stack exists to serve one
of those two jobs — or to package, deploy, secure, or observe them.
