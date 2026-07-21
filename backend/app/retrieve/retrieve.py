"""Q3 — retrieve: a query STRING -> the ranked chunks that answer it.

The single *end-to-end* read. Q2's `search_semantic` / Q5's `search_lexical` are pure DB
operations over one signal each; a caller has a *question*, not a vector or a tsquery.
`retrieve` is the stage above them that closes the gap — it embeds the query string,
runs BOTH searches concurrently, fuses them with Reciprocal Rank Fusion (Q6), and
(when enabled) reranks the fused pool with a cross-encoder (Q7) before handing back a
`RetrievalResult`. It is the single entry point that answers "what does this query
retrieve?". Every stage landed behind this ONE signature without changing it or its
callers: semantic-only in Q3, +lexical/fusion in Q5-Q6, +rerank in Q7 — the generation
stage (Q8) will read this result, not reshape this call.

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

Owns its sessions (the orchestrate.py contract):
    `search_semantic`/`search_lexical` each take a `session` because they are pure
    queries and shouldn't care where the connection comes from. `retrieve` is the
    orchestrator, so — like the ingest-side `orchestrate` — it opens the sessions via
    `SessionLocal`. It opens TWO, one per arm: a single async connection can't run two
    queries concurrently, so the arms each get their own so `asyncio.gather` can overlap
    them. A request handler (Q10 `/ask`) or a CLI (`explain.py`) just calls
    `retrieve(query)`; connection lifecycle is not their problem.

Timings (the eval-substrate rule):
    `RetrievalResult.timings_ms` records the wall-clock ms of each stage. Retrieval is
    the part of RAG hardest to reason about blind, so it must never run silently — the
    timings feed logs, the `explain-retrieval` skill, and later the eval harness. Same
    discipline, same `perf_counter`/`_ms` helper as `IngestResult`.

Module-level seams (SessionLocal, embed_texts, as_retrieval_query, search_semantic,
search_lexical, reciprocal_rank_fusion, rerank):
    Imported here as module globals rather than reached through their packages at each
    call site, so a test can monkeypatch *this module's* copy — point `SessionLocal` at
    a throwaway test container, swap `embed_texts` for a fake that never calls Gemini.
    Costs nothing in production; makes the whole read path drivable offline. The same
    seam pattern `orchestrate.py` and `documents.py` use.
"""

from __future__ import annotations

import asyncio
import uuid
from time import perf_counter

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.embed import as_retrieval_query, embed_texts
from app.models import DEV_OWNER_ID
from app.retrieve.fuse import reciprocal_rank_fusion
from app.retrieve.lexical import search_lexical
from app.retrieve.rerank import rerank
from app.retrieve.semantic import search_semantic
from app.retrieve.types import RetrievalResult, ScoredChunk


def _ms(since: float) -> float:
    """Milliseconds elapsed since a `perf_counter()` reading, rounded for logs."""
    return round((perf_counter() - since) * 1000, 1)


async def retrieve(
    query: str,
    *,
    k: int | None = None,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> RetrievalResult:
    """Retrieve the `k` chunks most relevant to `query`, end to end (hybrid).

    Embeds `query` in the RETRIEVAL_QUERY role, runs semantic (vector) and lexical
    (full-text) search CONCURRENTLY over the owner's chunks, fuses the two rankings with
    Reciprocal Rank Fusion, and returns a `RetrievalResult` (the query echoed, the fused
    top-`k` chunks each carrying its RRF score, and per-stage timings). `k` defaults to
    `Settings.retrieval_k`; `owner_id` is the visibility-predicate seam forwarded to
    both arms (today the owner = me boundary; future RLS/role filtering).

    The `score` on each returned `ScoredChunk` is the RRF **fused** score (rank-based),
    NOT a cosine similarity or `ts_rank_cd` value — fusion discards the arms' scales.
    When the lexical arm matches nothing, RRF reduces to the semantic order, so hybrid
    never underperforms semantic. When `Settings.rerank_enabled` is set, the fused pool
    (`retrieval_candidate_k` wide) is re-scored by the cross-encoder and the `score`
    becomes the reranker's; otherwise this returns the fused top-k unchanged (Q6).
    """
    settings = get_settings()
    k = settings.retrieval_k if k is None else k
    timings: dict[str, float] = {}
    started = perf_counter()

    # Pool width the arms fetch. With rerank OFF, this is just k -> the whole path
    # below is byte-identical to Q6 (fetch k, fuse, take k). With rerank ON, the arms
    # fetch the WIDE candidate pool: reranking a pool no bigger than k could never
    # reorder the top-k, so the pool must be widened BEFORE the cross-encoder narrows it
    # (retrieve-wide -> rerank-narrow). k stays the FINAL size either way, so callers
    # never change.
    pool_k = settings.retrieval_candidate_k if settings.rerank_enabled else k

    # Query STRING -> vector, in the QUERY role. One text in, one vector out ([0]). Only
    # the semantic arm needs the vector; the lexical arm searches the raw query string.
    t = perf_counter()
    query_embedding = (await embed_texts([as_retrieval_query(query)]))[0]
    timings["embed_ms"] = _ms(t)

    # Each arm opens its OWN session: one async DB connection can't service two queries
    # at once, so the two concurrent arms can't share a session. Each search is a pure
    # query (it takes the session); retrieve owns the sessions (orchestrate contract).
    async def _semantic() -> tuple[list[ScoredChunk], float]:
        s = perf_counter()
        async with SessionLocal() as session:
            hits = await search_semantic(
                session, query_embedding, k=pool_k, owner_id=owner_id
            )
        return hits, _ms(s)

    async def _lexical() -> tuple[list[ScoredChunk], float]:
        s = perf_counter()
        async with SessionLocal() as session:
            hits = await search_lexical(session, query, k=pool_k, owner_id=owner_id)
        return hits, _ms(s)

    # Run both arms concurrently; record each arm's own wall time (they overlap).
    t = perf_counter()
    (sem, timings["semantic_ms"]), (lex, timings["lexical_ms"]) = await asyncio.gather(
        _semantic(), _lexical()
    )

    # Fuse by RRF (rank-based, scale-free), then map the fused ids back to their Chunk
    # objects (from either arm) and carry the RRF score on each ScoredChunk. `by_id`
    # dedups a chunk both arms returned to one row. This is the fused candidate POOL (up
    # to pool_k); rerank (or the plain truncation) narrows it to k below.
    f = perf_counter()
    by_id = {sc.chunk.id: sc.chunk for sc in (*sem, *lex)}
    fused = reciprocal_rank_fusion(
        [[sc.chunk.id for sc in sem], [sc.chunk.id for sc in lex]],
        k_constant=settings.retrieval_rrf_k_constant,
    )
    fused_pool = [
        ScoredChunk(chunk=by_id[cid], score=score) for cid, score in fused[:pool_k]
    ]
    timings["fuse_ms"] = _ms(f)
    # search_ms = the whole fetch+fuse span (both arms, overlapped, + fusion). Kept as a
    # stable key alongside the finer semantic_ms/lexical_ms/fuse_ms breakdown.
    timings["search_ms"] = _ms(t)

    # Cross-encoder rerank (Q7), gated. ON: the cross-encoder re-scores the fused pool
    # by reading query+passage together and returns the top-k (its score replaces the
    # RRF score). OFF: fused_pool is already length k (pool_k == k), so this is just a
    # slice, exactly the Q6 result. Either way `chunks` is the final top-k.
    if settings.rerank_enabled:
        r = perf_counter()
        chunks = await rerank(query, fused_pool, top_n=k)
        timings["rerank_ms"] = _ms(r)
    else:
        chunks = fused_pool[:k]

    timings["total_ms"] = _ms(started)
    return RetrievalResult(
        query=query,
        chunks=chunks,
        timings_ms=timings,
        # The fused pool's order, captured BEFORE rerank could overwrite it: the
        # per-query log (Q10) records both "what retrieval found" and "what the
        # reranker promoted", and only this snapshot preserves the former.
        candidate_chunk_ids=[sc.chunk.id for sc in fused_pool],
    )
