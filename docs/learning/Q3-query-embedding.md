# Q3 — Query embedding + retrieve orchestrator: the first end-to-end read

_Phase 2, milestone Q3. The stage that turns a query **string** into the ranked chunks
that answer it — the first call that closes embed → search into one._

Q3 is `app/retrieve/retrieve.py::retrieve(query: str, *, k=None, owner_id=DEV_OWNER_ID) ->
RetrievalResult`. Q2's `search_semantic` takes a query **vector** and stays a pure DB
operation; Q3 is the caller above it that embeds the query string (in the query role),
owns the session, runs the search, and returns a result carrying the chunks + per-stage
timings. Still naive-first — semantic only — but it's the seam every later stage (lexical
Q5, RRF Q6, rerank Q7, generation Q8) slots behind without changing the signature.

---

## 1. Query/document asymmetry — and why it's a *prefix*, not a `task_type`

An embedding model turns text into a vector, but it embeds the **same text differently
depending on its role**: a stored passage to be *found*, versus a search query *looking*
for one. Matching those two roles is what makes a query's vector land near the passages
that answer it — get it wrong and similarity degrades silently (you still get a vector,
just a worse-matched one).

The two roles have standard names: **`RETRIEVAL_DOCUMENT`** (ingest) and
**`RETRIEVAL_QUERY`** (query time). At ingest, M6 wrapped each chunk with
`as_retrieval_document`; here we wrap the query with `as_retrieval_query`:

```python
query_embedding = (await embed_texts([as_retrieval_query(query)]))[0]
```

**Why a prefix and not a field.** The older `gemini-embedding-001` took the role as a
`task_type` *config value* on the API call. **`gemini-embedding-2` removed that field** —
the role is now encoded as an instruction **prefix** prepended to the text itself
(Google's documented v2 format):

```
document →  "title: none | text: <chunk text>"
query    →  "task: search result | query: <the question>"
```

`as_retrieval_query` / `as_retrieval_document` (in `app/ingest/embed.py`) are the single
home for that format; `embed_texts` itself is **role-agnostic** — it embeds whatever
string it's handed. So the wrapping is the *call site's* responsibility, exactly as the
old required `task_type` argument was. Forgetting it is the easy silent bug, so it's
pinned by a test: `test_retrieve_wraps_query_in_query_role` asserts on the *actual string
handed to embed_texts* — `[as_retrieval_query(query)]` — so dropping the wrapper, or using
the document wrapper, goes red.

---

## 2. `embed_texts([...])[0]` — the batch-of-one, and the fusion gotcha

`embed_texts` is **batch-shaped**: it takes a *list* of texts and returns a list of
vectors, one per input — because at ingest we embed many chunks per call. At query time we
have exactly one text, so we pass a one-element list and take `[0]`.

This isn't just cosmetic. There's a live gotcha behind it (from M4, revalidated against
the real API): the SDK reads a **bare `list[str]` as the many Parts of ONE input** and
returns a **single fused vector**. `embed_texts` defends against that by wrapping each
input in its own `types.Content`. Calling it with `[wrapped_query]` rides that same
correct path and gets back a list of one real 768-d vector — never a fused blob. (This is
also why `explain.py`'s display-embed passes a list, not a scalar.)

---

## 3. The orchestrator owns the session; the search stays pure

`search_semantic(session, ...)` takes a `session` as an argument — it's a pure query and
shouldn't care where the connection came from (that's what let Q2 test it against a
throwaway container by just handing it one). `retrieve` is the **orchestrator**, so — like
the ingest-side `orchestrate` — it opens the session itself:

```python
async with SessionLocal() as session:
    chunks = await search_semantic(session, query_embedding, k=k, owner_id=owner_id)
```

A request handler (`POST /ask`, Q10) or the `explain.py` CLI just calls `retrieve(query)`;
connection lifecycle is not their problem. The `owner_id` passthrough keeps Q2's
visibility-predicate seam unbroken all the way up.

**`k` default.** `k=None` resolves to `Settings.retrieval_k` (10) inside the function —
the clean idiom for "default from config", since a default argument is evaluated once at
import time and couldn't read live settings. Today `k` is the final result size; once
rerank lands (Q7) the same `k` becomes the *candidate-pool* size that rerank truncates
down to 5–10. One knob, meaning shifts as the pipeline grows — no signature change.

---

## 4. The monkeypatch-seam pattern (why the collaborators are module globals)

`retrieve.py` imports `SessionLocal`, `embed_texts`, `as_retrieval_query`, and
`search_semantic` as **module-level names**, not reached through their packages at each
call site. That's deliberate: a test can then monkeypatch *this module's copy* —

```python
monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)   # test container
monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(...))  # no Gemini
```

— so the whole read path runs offline and deterministically: the fake embedder returns a
known axis vector, and the real pgvector search then orders known-axis chunks in a way we
can assert exactly (`test_retrieve_orders_by_similarity_end_to_end`). It costs nothing in
production and is the same seam `orchestrate.py` and `documents.py` already use. (Note the
fake *records its input* — that's what powers the query-role assertion in §1.)

---

## 5. Why the result carries timings (eval is a substrate)

`retrieve` returns a **`RetrievalResult`** (frozen dataclass in `app/retrieve/types.py`,
beside `ScoredChunk` — the shared result-vocabulary module, so `/ask` and the eval harness
import one canonical type):

```python
query: str                      # echoed back — a result identifies what it answered
chunks: list[ScoredChunk]       # ranked, best first
timings_ms: dict[str, float]    # embed_ms, search_ms, total_ms (more as stages land)
```

Retrieval is the part of RAG hardest to reason about blind, so it must **never run
silently** — the same "eval is a substrate" discipline that made `IngestResult` carry
timings. Those numbers feed logs, the `explain-retrieval` tracer, and (Q4) the eval
harness. `test_retrieve_result_shape_and_timings` pins the timing keys and the score
direction (cosine similarity, higher = nearer — consistent with `ScoredChunk`).

---

## 6. Wiring `explain-retrieval`

`app/retrieve/explain.py` (`python -m app.retrieve.explain "<query>"`) is the live tracer
the `/explain-retrieval` skill drives — the retrieval analogue of `app/ingest/inspect.py`.
It **drives the real `retrieve()`** rather than reimplementing any stage, so as Q5–Q8 land
inside `retrieve` the tracer shows them with no rework; today it prints the query, the
role-wrapped string, the embedding (model/dims/unit-norm check), and the semantic
candidates, then lists the not-yet-wired stages so the output shape stays stable.

---

## Alternatives rejected

- **Embedding the raw query string** (no role wrapper) — simplest, but throws away the
  query/document asymmetry that makes retrieval work. The wrapper is mandatory
  (CLAUDE.md), so it's the call site's job and a test guards it.
- **`retrieve` taking a `session` argument** (like `search_semantic`) — would push
  connection lifecycle onto every caller (`/ask`, CLI, eval). The orchestrator owning the
  session is the whole point of the layer; `search_semantic` stays pure beneath it.
- **`k` as a plain default argument** (`k: int = 10`) — a literal hard-codes the value and
  ignores config/env. `k=None` → resolve from settings keeps `RETRIEVAL_K` live.
- **`RetrievalResult` defined in `retrieve.py`** — but Q10's `/ask` and Q4's eval both
  consume it; putting it in `types.py` beside `ScoredChunk` keeps one canonical definition
  and dodges an import cycle (same reasoning `ScoredChunk` documents).

## Gotcha to remember

`explain.py` embeds the query **twice** by design: once itself (to *display* the vector's
shape and confirm it's unit-length) and once inside `retrieve()` (the real run, reported as
`embed_ms`). That redundant call is fine — it's a dev-only tracer and the point is to make
the embedding step *visible* rather than hidden. Don't "optimize" it away by having
`explain` reach past `retrieve` into `search_semantic`; driving `retrieve()` is what keeps
the tracer honest as the pipeline grows.
