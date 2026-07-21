# Q10 — `POST /ask`: the query endpoint and the per-query log

_Phase 2, milestone 10 (the last one). Closes the read path: the pipeline built over
Q1–Q9 is now reachable over HTTP, and every query it answers leaves a record._

---

## 1. Two shapes of work, two shapes of API

This project now has both kinds of endpoint, and the contrast is the lesson.

| | ingestion (`POST /documents`, M7) | query (`POST /ask`, Q10) |
|---|---|---|
| duration | tens of seconds (hi_res parse, OCR, embedding) | ~1–3 s |
| shape | **job-shaped**: 202 Accepted + poll `GET /documents/{id}` | **request-shaped**: the answer is in the response |
| who waits | nobody — a worker runs it | the caller, deliberately |
| failure lands in | `documents.status` / `error` | the HTTP status of this request |

The rule that decides between them is not "which is more modern" — it's **how long the
work takes relative to what an HTTP connection can comfortably hold open.** Tens of
seconds invites proxy/load-balancer timeouts and pins a connection and a worker for the
whole job, so ingestion defers. A query is short and the caller has nothing to do until
it finishes, so deferring it would only add a poll and a round trip for no benefit.

Vocabulary check:
- **Request-shaped** (a.k.a. synchronous): one request → the work runs → the result is
  the response body.
- **Job-shaped**: the request only *registers* work and returns a handle; the result is
  fetched later (poll, webhook, or push).

**Streaming is the real upgrade to this, and it's deliberately Phase 2.5.** Today the
caller waits ~1 s for generation and then gets the whole answer at once. Server-Sent
Events (SSE) would emit tokens as the model produces them, which cuts *perceived*
latency dramatically without changing a single pipeline stage — it changes the
transport, not the RAG. Building it now would have coupled a transport experiment to
the milestone that establishes the contract.

## 2. The per-query log — the point of the milestone

CLAUDE.md's standing rule: *"Log every RAG query: user message, retrieved chunk IDs,
reranked order, final context, the answer, and token/cost usage. This log is the raw
material for evaluation."*

Every RAG debugging session starts with the same question — **"what did it actually
retrieve for that query?"** — and if the answer isn't recorded, the question is
unanswerable after the fact. So `/ask` writes to two sinks, because they answer
different questions:

| sink | what it's for |
|---|---|
| a structured `logger.info` line (JSON payload, logger `app.api.ask`) | the **live tail**: "what just happened on that request?" — greppable, no DB round trip |
| a `query_logs` row (migration `004`) | the **durable, queryable history**: "which queries ever retrieved chunk 42?", "what did we spend last week?" — SQL questions a log stream cannot answer |

Three design calls inside that table worth naming:

1. **We store the context's chunk *ids* and its size, not its text.** Chunk content is
   reconstructible by joining `chunks`, so copying it per query would duplicate the
   whole corpus once per question. The trade-off is real and recorded in the migration:
   if a document is deleted, the log can no longer reproduce the exact prompt. It is
   reversible — add a `context_text` column later if reproducibility ever outranks
   storage.
2. **Tokens, not dollars.** Prices change per model and over time, so a stored cost
   figure rots. `prompt_tokens` / `completion_tokens` / `total_tokens` + the model id
   are the facts; cost is *derived* at reporting time.
3. **Ordered `bigint[]` arrays, not JSONB, for the id lists.** Rank order *is* the data
   here (rank 1 vs rank 4), the elements are homogeneous, and Postgres keeps them typed.
   `timings_ms` stays JSONB precisely because its keys *change* as pipeline stages are
   added — a migration per new stage would be the wrong shape.

The row write is **best-effort**: wrapped so that a failing audit write can never turn an
answer the user already paid for into a 500. The audit matters; it does not matter more
than the product.

## 3. Both rankings, or the funnel is invisible

`retrieve()` returned only the *final* ranking. With rerank on, that means the
cross-encoder's output — the earlier, wider ranking is gone. But the logging contract
asks for "retrieved chunk IDs" **and** "reranked order", and the whole Q7 story is that
retrieval fetches ~50 candidates and rerank narrows them to 10. Reporting the final list
under both names would have been a lie that hid exactly what the log exists to show.

So `RetrievalResult` gained one field, `candidate_chunk_ids` — the fused pool's order,
captured before rerank can overwrite it. With rerank off, `pool_k == k`, so it simply
equals the final ids; the field never implies a funnel that didn't happen.

A live example (the exact response from the verification run, rerank ON):

```
retrieved_chunk_ids: [37, 34, 36, 41, 45, 39, 38, 35, 43, 40, ...]   # 50 candidates
reranked_chunk_ids : [37, 38, 34, 39, 36, 45, 41, 98, 43, 162]       # top 10 shown to the LLM
citations          : [36]                                            # what the answer used
```

Chunk 38 was 7th in the fused pool and 2nd after reranking; chunk 98 was outside the
fused top-10 entirely and made the final context. Without the pre-rerank snapshot, none
of that is knowable from the log.

## 4. Where token usage had to live (and where it couldn't)

Usage counts come back on the generation response as `usage_metadata`. The obvious move
— add a `usage` field to `GeneratedAnswer` — is **wrong**, and the reason is a nice trap:
`GeneratedAnswer` *is* the `response_schema` handed to Gemini. Any field added to it
becomes a field **the model is asked to fill in**; we would have been asking an LLM to
invent its own token counts.

Usage is knowledge *about the call*, not part of the answer, so it belongs on a wrapper
the SDK never sees: `GenerationResult(answer, citations, usage, timings_ms)`.

The wrapper is **flat** — `.answer: str`, `.citations: list[int]` reproduce exactly the
attribute surface `GeneratedAnswer` had. That one choice meant both existing callers
(`eval/produce.py`, `eval/refusal.py`) and all of their immutable tests kept passing
untouched; the only casualty was a single `isinstance(...)` assertion in
`test_generate.py`, revised **with the architect's authorization** and with the reasoning
written into the test file (the Q6/Q9 precedent — a test is a spec, and only its owner
may revise it).

Counts degrade to `None`, never to `0`: a fabricated zero silently under-reports spend,
and an `AttributeError` would throw away a good answer over bookkeeping.

## 5. Gotchas hit

- **The log line didn't appear in the real server.** The tests were green — `caplog`
  attaches its own handler — but uvicorn configures only the `uvicorn.*` loggers, so an
  `app.*` logger falls back to Python's *lastResort* handler, which is WARNING-level.
  Every `logger.info` in the app (including this milestone's whole point, and the
  ingestion orchestrator's records when run under the API) was being dropped on the
  floor. Fixed with a `logging.basicConfig(level=INFO, force=True)` in `app/main.py`.
  **The lesson generalizes: a passing test proves the call was made, not that anyone can
  see it.** Only running the real server surfaced this.
- **Cold-start latency ≠ steady-state latency.** The first live `/ask` took 24.8 s;
  the second took 2.3 s. The difference is entirely the lazily-loaded cross-encoder
  (torch import + ~90 MB model into memory, on first use). It's harmless for a
  long-running server and invisible in eval runs, but it would be a terrible first
  impression for a user — a warm-up call at startup is the obvious future fix.
- **`apply-migrations.ps1` "failed" on a stderr NOTICE.** Not a migration bug: piping a
  native exe's stderr through PowerShell (`2>&1`) wraps each line as an ErrorRecord, and
  with `$ErrorActionPreference = "Stop"` psql's harmless `NOTICE: ... already exists`
  became a terminating error. Run it without the redirect. (The tooling note this repo
  already carries: don't redirect a native command's stderr in PowerShell.)
- **Test isolation:** `query_logs` has no FK to `documents`, so the harness teardown's
  `TRUNCATE chunks, documents CASCADE` would never have reached it. Added explicitly —
  committed audit rows would otherwise have leaked between tests.

## 6. What's verified

- Offline suite **144 pass + 5 deselected-live, 0 skip** (was 132+5): `test_ask.py` (6),
  `test_generate_usage.py` (4), `test_retrieve_candidates.py` (2), all red-first.
- Live, against the dev corpus (9 docs / 97 chunks) with `RERANK_ENABLED=true`: three
  real queries answered with citations — including one **correct refusal** ("the text
  names a *president*, Dr. Naomi Chen, not a chancellor"), a blank query rejected 400
  before any spend, the `ask.query` log line emitted, and matching `query_logs` rows
  carrying both rankings and real token counts (1,626 and 1,711 total tokens).
