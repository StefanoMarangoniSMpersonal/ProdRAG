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
    queries concurrently, so the arms each get their own; the two search NODES run in
    the same graph superstep below, overlapping them exactly as `asyncio.gather` did.
    A request handler (Q10 `/ask`) or a CLI (`explain.py`) just calls `retrieve(query)`;
    connection lifecycle is not their problem.

Timings (the eval-substrate rule):
    `RetrievalResult.timings_ms` records the wall-clock ms of each stage. Retrieval is
    the part of RAG hardest to reason about blind, so it must never run silently — the
    timings feed logs, the `explain-retrieval` skill, and later the eval harness. Same
    discipline, same `perf_counter`/`_ms` helper as `IngestResult`.

P5 — the read path IS a LangGraph graph (why the internals look the way they do):
    The linear embed -> (semantic ‖ lexical) -> fuse -> [rerank] flow is expressed as
    an explicit graph: module-level `_*_node` coroutines are the steps, a shared
    `GraphState` dict is threaded through, `embed` fans out to the two search nodes
    (one superstep = concurrent), and they fan back in to `fuse`. The two branches each
    write their own timing into the shared `timings` key in the SAME superstep, so
    `timings` carries a **reducer** (`_merge_timings`) that merges both writes instead
    of one clobbering the other. A **conditional edge** off `fuse` routes to the
    `rerank` node or straight to END. `retrieve()` is now a thin wrapper: seed the
    state, `ainvoke` the graph (compiled ONCE and memoized by `_graph()`), assemble the
    `RetrievalResult`. Output is byte-identical to the pre-P5 linear version — a
    refactor for structure/teaching value, not a behaviour change. See
    `docs/learning/P5-langgraph.md`.

Module-level seams (SessionLocal, embed_texts, as_retrieval_query,
as_retrieval_document, generate_hypothetical, search_semantic, search_lexical,
reciprocal_rank_fusion, rerank, get_settings):
    Imported here as module globals rather than reached through their packages at each
    call site, so a test can monkeypatch *this module's* copy — point `SessionLocal` at
    a throwaway test container, swap `embed_texts` for a fake that never calls Gemini.
    Costs nothing in production; makes the whole read path drivable offline. The same
    seam pattern `orchestrate.py` and `documents.py` use. The graph nodes below resolve
    these names as **module globals at call time** (never captured into a closure or a
    default arg), so every existing `monkeypatch.setattr(retrieve_mod, ...)` still lands
    even though the graph holds references to the node functions, not the seams.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from time import perf_counter
from typing import Annotated, Any, TypedDict

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.embed import as_retrieval_document, as_retrieval_query, embed_texts
from app.models import DEV_OWNER_ID
from app.retrieve.fuse import reciprocal_rank_fusion
from app.retrieve.hyde import generate_hypothetical
from app.retrieve.lexical import search_lexical
from app.retrieve.rerank import rerank
from app.retrieve.semantic import search_semantic
from app.retrieve.types import RetrievalResult, ScoredChunk


def _ms(since: float) -> float:
    """Milliseconds elapsed since a `perf_counter()` reading, rounded for logs."""
    return round((perf_counter() - since) * 1000, 1)


async def embed_query(query: str) -> list[float]:
    """Embed a query STRING into its vector, in the RETRIEVAL_QUERY role.

    The single home for the query-side embed: wrap the string with `as_retrieval_query`
    (the query/document asymmetry — see the module docstring), embed the one-element
    batch, take `[0]`. Factored out because more than one caller now needs the query
    vector *before* retrieval runs — the P3 semantic cache embeds here to look up a
    cached answer, and P4/HyDE will embed here too — and paying Gemini twice for one
    query (once in the cache, once inside `retrieve`) would be wasteful. `retrieve`
    calls this when no vector is supplied; a caller that already embedded passes the
    vector down via `query_embedding=`.
    """
    return (await embed_texts([as_retrieval_query(query)]))[0]


# --- graph state + reducer ------------------------------------------------------------


def _merge_timings(old: dict[str, float], new: dict[str, float]) -> dict[str, float]:
    """Reducer for the shared `timings` state key: merge two partial timing dicts.

    The two search branches write `semantic_ms` / `lexical_ms` into `timings` in the
    SAME graph superstep. Without a reducer LangGraph would reject two concurrent writes
    to one key ("can receive only one value per step"); with it, both survive. Disjoint
    keys from sequential nodes (embed/fuse/rerank) merge harmlessly too. `old or {}`
    tolerates the channel's first write, whatever empty value it starts from.
    """
    return {**(old or {}), **new}


class GraphState(TypedDict, total=False):
    """The object threaded through the read-path graph — inputs, scratch, and outputs.

    Every key is a LangGraph channel; `timings` carries a reducer (`_merge_timings`);
    the rest are last-value channels (one writer each, so a plain overwrite is correct —
    `chunks` is written by `fuse` and then overwritten by `rerank` in a LATER superstep,
    which is a sequential overwrite, not a concurrent one).
    """

    # inputs (seeded by the retrieve() wrapper)
    query: str
    k: int
    owner_id: uuid.UUID
    pool_k: int
    settings: Any
    query_embedding: list[float] | None
    # scratch (produced by nodes, consumed downstream)
    search_t0: float
    sem: list[ScoredChunk]
    lex: list[ScoredChunk]
    fused_pool: list[ScoredChunk]
    # outputs (read back by the wrapper)
    chunks: list[ScoredChunk]
    candidate_chunk_ids: list[int]
    timings: Annotated[dict[str, float], _merge_timings]


# --- nodes (each references the module-global seams at call time) ---------------------


async def _embed_node(state: GraphState) -> dict:
    """Query STRING -> vector, in the QUERY role. One text in, one vector out.

    Only the semantic arm needs the vector; the lexical arm searches the raw query
    string. When the caller already embedded (P3 cache shares ONE embed with lookup),
    it hands the vector in via `query_embedding=` and we skip the call, recording
    embed_ms = 0.0 so the timings key set the immutable tests pin stays present.

    HyDE (P4, gated): instead of embedding the *question*, ask the LLM for a
    hypothetical answer PASSAGE and embed THAT in the DOCUMENT role — so the query
    vector lands in the same space as the real chunks (see app/retrieve/hyde.py). Only
    the semantic arm is affected; the lexical arm still searches the raw `query`.
    FAIL-OPEN: any generation error degrades to embedding the raw query, so a flaky
    HyDE call can never break retrieval. Only fires on the no-vector path — a supplied
    `query_embedding` short-circuits everything. Stashes `search_t0` for `fuse` to
    measure the fetch+fuse span.
    """
    settings = state["settings"]
    query = state["query"]
    query_embedding = state["query_embedding"]
    timings: dict[str, float] = {}

    t = perf_counter()
    if query_embedding is None:
        if settings.hyde_enabled:
            try:
                hypothetical = await generate_hypothetical(query)
                query_embedding = (
                    await embed_texts([as_retrieval_document(hypothetical)])
                )[0]
            except Exception:
                query_embedding = await embed_query(query)
            timings["hyde_ms"] = _ms(t)
        else:
            query_embedding = await embed_query(query)
        timings["embed_ms"] = _ms(t)
    else:
        timings["embed_ms"] = 0.0

    return {
        "query_embedding": query_embedding,
        "search_t0": perf_counter(),
        "timings": timings,
    }


async def _semantic_node(state: GraphState) -> dict:
    """Semantic (vector) arm — opens its OWN session (one connection, one query)."""
    s = perf_counter()
    async with SessionLocal() as session:
        hits = await search_semantic(
            session,
            state["query_embedding"],
            k=state["pool_k"],
            owner_id=state["owner_id"],
        )
    return {"sem": hits, "timings": {"semantic_ms": _ms(s)}}


async def _lexical_node(state: GraphState) -> dict:
    """Lexical (full-text) arm — opens its OWN session, searches the RAW query."""
    s = perf_counter()
    async with SessionLocal() as session:
        hits = await search_lexical(
            session, state["query"], k=state["pool_k"], owner_id=state["owner_id"]
        )
    return {"lex": hits, "timings": {"lexical_ms": _ms(s)}}


async def _fuse_node(state: GraphState) -> dict:
    """Fuse the two arms by RRF, map ids back to chunks, carry the fused score.

    `by_id` dedups a chunk both arms returned to one row. This is the fused candidate
    POOL (up to pool_k); rerank (or the plain truncation via `chunks = fused_pool[:k]`
    here) narrows it to k. `search_ms` = the whole fetch+fuse span, measured from the
    `search_t0` the embed node stashed. `chunks` is set to the plain top-k here so the
    rerank-OFF path is complete straight to END; the rerank node overwrites it when ON.
    """
    settings = state["settings"]
    sem, lex = state["sem"], state["lex"]
    pool_k, k = state["pool_k"], state["k"]

    f = perf_counter()
    by_id = {sc.chunk.id: sc.chunk for sc in (*sem, *lex)}
    fused = reciprocal_rank_fusion(
        [[sc.chunk.id for sc in sem], [sc.chunk.id for sc in lex]],
        k_constant=settings.retrieval_rrf_k_constant,
    )
    fused_pool = [
        ScoredChunk(chunk=by_id[cid], score=score) for cid, score in fused[:pool_k]
    ]
    timings = {"fuse_ms": _ms(f), "search_ms": _ms(state["search_t0"])}
    return {
        "fused_pool": fused_pool,
        # The fused pool's order, captured BEFORE rerank could overwrite it: the
        # per-query log (Q10) records both "what retrieval found" and "what the reranker
        # promoted", and only this snapshot preserves the former.
        "candidate_chunk_ids": [sc.chunk.id for sc in fused_pool],
        "chunks": fused_pool[:k],
        "timings": timings,
    }


async def _rerank_node(state: GraphState) -> dict:
    """Cross-encoder rerank (Q7): re-score the fused pool, return the top-k.

    The cross-encoder reads query+passage together and returns the top-k, its score
    replacing the RRF score. Reached only via the conditional edge off `fuse` when
    `rerank_enabled` is set; otherwise `fuse`'s plain `chunks = fused_pool[:k]` stands.
    """
    r = perf_counter()
    chunks = await rerank(state["query"], state["fused_pool"], top_n=state["k"])
    return {"chunks": chunks, "timings": {"rerank_ms": _ms(r)}}


def _route_after_fuse(state: GraphState) -> str:
    """Conditional edge: fuse -> rerank node when enabled, else straight to END.

    Returns a routing KEY (not `END` itself) so this module-level function needs no
    langgraph import — the path map inside `_graph()` translates "end" to `END`.
    """
    return "rerank" if state["settings"].rerank_enabled else "end"


@lru_cache(maxsize=1)
def _graph():
    """Build and compile the read-path graph ONCE (memoized), reused per query.

    langgraph is imported lazily HERE (not at module top) so importing this module
    stays dependency-light and the graph compiles a single time on first use — the same
    "build the heavy thing once" pattern as the P0 reranker warm-up / `_get_reranker`.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(GraphState)
    builder.add_node("embed", _embed_node)
    builder.add_node("semantic", _semantic_node)
    builder.add_node("lexical", _lexical_node)
    builder.add_node("fuse", _fuse_node)
    builder.add_node("rerank", _rerank_node)

    builder.add_edge(START, "embed")
    # Fan-out: embed -> both arms. Two edges from one node = one superstep = the two
    # arms run concurrently (what asyncio.gather did before P5).
    builder.add_edge("embed", "semantic")
    builder.add_edge("embed", "lexical")
    # Fan-in: fuse waits for BOTH arms (both incoming edges) before it runs.
    builder.add_edge("semantic", "fuse")
    builder.add_edge("lexical", "fuse")
    # Conditional edge: the rerank gate, expressed as graph structure.
    builder.add_conditional_edges(
        "fuse", _route_after_fuse, {"rerank": "rerank", "end": END}
    )
    builder.add_edge("rerank", END)

    return builder.compile()


async def retrieve(
    query: str,
    *,
    k: int | None = None,
    owner_id: uuid.UUID = DEV_OWNER_ID,
    query_embedding: list[float] | None = None,
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

    Internally (P5) this is a compiled LangGraph graph; the wrapper here only seeds the
    state, invokes the graph, and assembles the result. Output is byte-identical to the
    pre-P5 linear implementation.
    """
    settings = get_settings()
    k = settings.retrieval_k if k is None else k

    # Pool width the arms fetch. With rerank OFF, this is just k -> the whole path is
    # byte-identical to Q6 (fetch k, fuse, take k). With rerank ON, the arms fetch the
    # WIDE candidate pool: reranking a pool no bigger than k could never reorder the
    # top-k, so the pool must be widened BEFORE the cross-encoder narrows it
    # (retrieve-wide -> rerank-narrow). k stays the FINAL size either way.
    pool_k = settings.retrieval_candidate_k if settings.rerank_enabled else k

    started = perf_counter()
    initial: GraphState = {
        "query": query,
        "k": k,
        "owner_id": owner_id,
        "pool_k": pool_k,
        "settings": settings,
        "query_embedding": query_embedding,
        "timings": {},
    }
    final = await _graph().ainvoke(initial)

    timings = dict(final["timings"])
    timings["total_ms"] = _ms(started)
    return RetrievalResult(
        query=query,
        chunks=final["chunks"],
        timings_ms=timings,
        candidate_chunk_ids=final["candidate_chunk_ids"],
    )
