"""Q3 — retrieve: a query STRING -> the ranked chunks that answer it.

The first *end-to-end* read. Q2's `search_semantic` is a pure DB operation: it takes a
query **vector** and returns the nearest chunks. But a caller has a *question*, not a
vector. `retrieve` is the stage above `search_semantic` that closes that gap — it embeds
the query string, runs the search, and hands back a `RetrievalResult`. It is the single
entry point that answers "what does this query retrieve?", and the seam every later
stage slots behind (lexical Q5, RRF fusion Q6, rerank Q7) without changing this
signature or its callers.

The query/document ASYMMETRY (why we don't just embed the raw string):
    An embedding model turns text into a vector, but it embeds the *same* text
    differently depending on its intended **role** — a stored passage to be found, vs a
    search query looking for one. Matching those roles is what makes a query's vector
    land near the passages that answer it. At ingest, M6 wrapped each chunk with
    `as_retrieval_document`; here we wrap the query with `as_retrieval_query` (which
    gemini-embedding-2 encodes as an instruction PREFIX on the text, since it dropped
    the old `task_type` field — see `app/ingest/embed.py`). Skipping the wrapper still
    yields a vector, just a silently worse-matched one, so wrapping is this call's job.

Why `embed_texts([...])[0]`:
    `embed_texts` is batch-shaped (it embeds a *list* of texts and returns a list of
    vectors, one per input) because at ingest we embed many chunks at once. Here we have
    exactly one text — the query — so we pass a one-element list and take `[0]`.
    (Passing a batch of one, rather than a scalar API, also dodges the v2 gotcha where a
    bare multi-part input collapses into a single fused vector; see the embed module.)

Owns its own session (the orchestrate.py contract):
    `search_semantic` takes a `session` because it's a pure query and shouldn't care
    where the connection comes from. `retrieve` is the orchestrator, so — exactly like
    the ingest-side `orchestrate` — it opens the session itself via `SessionLocal`. A
    request handler (Q10 `/ask`) or a CLI (`explain.py`) just calls `retrieve(query)`;
    connection lifecycle is not their problem.

Timings (the eval-substrate rule):
    `RetrievalResult.timings_ms` records the wall-clock ms of each stage. Retrieval is
    the part of RAG hardest to reason about blind, so it must never run silently — the
    timings feed logs, the `explain-retrieval` skill, and later the eval harness. Same
    discipline, same `perf_counter`/`_ms` helper as `IngestResult`.

Module-level seams (SessionLocal, embed_texts, as_retrieval_query, search_semantic):
    Imported here as module globals rather than reached through their packages at each
    call site, so a test can monkeypatch *this module's* copy — point `SessionLocal` at
    a throwaway test container, swap `embed_texts` for a fake that never calls Gemini.
    Costs nothing in production; makes the whole read path drivable offline. The same
    seam pattern `orchestrate.py` and `documents.py` use.
"""

from __future__ import annotations

import uuid
from time import perf_counter

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.embed import as_retrieval_query, embed_texts
from app.models import DEV_OWNER_ID
from app.retrieve.semantic import search_semantic
from app.retrieve.types import RetrievalResult


def _ms(since: float) -> float:
    """Milliseconds elapsed since a `perf_counter()` reading, rounded for logs."""
    return round((perf_counter() - since) * 1000, 1)


async def retrieve(
    query: str,
    *,
    k: int | None = None,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> RetrievalResult:
    """Retrieve the `k` chunks most relevant to `query`, end to end.

    Embeds `query` in the RETRIEVAL_QUERY role, runs semantic (vector) search over the
    owner's chunks, and returns a `RetrievalResult` (the query echoed, the ranked chunks
    with cosine-similarity scores, and per-stage timings). `k` defaults to
    `Settings.retrieval_k`; `owner_id` is the visibility-predicate seam forwarded to
    `search_semantic` (today the owner = me boundary; future RLS/role filtering).

    Naive-first: semantic only. Lexical + RRF fusion + rerank slot in behind this same
    call in Q5-Q7 without changing the signature or any caller.
    """
    settings = get_settings()
    k = settings.retrieval_k if k is None else k
    timings: dict[str, float] = {}
    started = perf_counter()

    # Query STRING -> vector, in the QUERY role. One text in, one vector out ([0]).
    t = perf_counter()
    query_embedding = (await embed_texts([as_retrieval_query(query)]))[0]
    timings["embed_ms"] = _ms(t)

    # Own the session (orchestrator contract); search_semantic is the pure query below.
    t = perf_counter()
    async with SessionLocal() as session:
        chunks = await search_semantic(session, query_embedding, k=k, owner_id=owner_id)
    timings["search_ms"] = _ms(t)

    timings["total_ms"] = _ms(started)
    return RetrievalResult(query=query, chunks=chunks, timings_ms=timings)
