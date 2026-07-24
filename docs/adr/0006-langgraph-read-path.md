# ADR 0006 — LangGraph orchestration of the read path

- **Status:** Accepted (2026-07-24). Behaviour-preserving refactor; output byte-identical to pre-P5.
- **Milestone:** Phase 2.5, P5 (LangGraph orchestration of the read path)
- **Deciders:** the architect (human); implemented by Claude Code

## Context

`retrieve()` (`backend/app/retrieve/retrieve.py`) was a clean linear async function: embed the query
(with an optional HyDE detour) → run semantic + lexical search concurrently via `asyncio.gather` →
fuse by RRF → optionally rerank. It works and is well-tested. P5's goal is **not** better retrieval
numbers — it is the "why a graph?" teaching milestone Phase 2 deliberately deferred: re-express *that
same flow* as an explicit **LangGraph** graph so the payoff is understanding what graph structure
buys (named nodes, conditional edges as first-class structure, a shared typed state, and reducers for
parallel writes), against a real pipeline rather than a toy.

Because the output must not move, the only genuine decisions are about *shape*, and each is the
architect's call:

1. **What linear span becomes the graph** — the whole `/ask` request (`ask.py`), or just the
   `retrieve()` internals.
2. **How the two concurrent searches are drawn** — as one node that internally does `asyncio.gather`
   (keep the concurrency hidden), or as true parallel graph branches (fan-out → fan-in) that make the
   concurrency structural.

`CLAUDE.md` says "keep LangChain thin; let **LangGraph** own control flow" but doesn't say where the
graph boundary sits — hence this ADR.

## Decision

1. **Scope = `retrieve()` internals only.** The public `retrieve()` / `embed_query()` signatures are
   unchanged and **all of `ask.py` is untouched**. The graph replaces the *body* of `retrieve()`.
2. **The two searches = true parallel graph branches.** `embed` fans out to a `semantic` node and a
   `lexical` node (two edges from one node = one LangGraph *superstep* = concurrent execution,
   exactly what `asyncio.gather` did), which fan back in to a `fuse` node. Each search node opens its
   **own** `SessionLocal()` — per-arm session ownership is preserved (one async connection can't run
   two queries at once).
3. **A reducer on the shared `timings` key.** Both branches write a partial `timings` dict in the
   same superstep; `timings` is typed `Annotated[dict[str, float], _merge_timings]` where
   `_merge_timings(old, new) = {**old, **new}`. This is the deliberate lesson: without it LangGraph
   rejects two concurrent writes to one channel; with it, `semantic_ms` and `lexical_ms` coexist.
4. **The rerank gate is a conditional edge.** `_route_after_fuse` routes `fuse → rerank` node when
   `rerank_enabled`, else `fuse → END` (the plain `chunks = fused_pool[:k]` set by `fuse` stands).
5. **The graph is compiled once and memoized** (`@lru_cache _graph()`), with langgraph imported
   **lazily inside the builder** so `import app.retrieve.retrieve` / `import app.main` stay
   dependency-light and never pull langgraph (or torch) at app-import time.
6. **Nodes read the seams as module globals at call time** — never captured into a closure or default
   arg — so every existing `monkeypatch.setattr(retrieve_mod, ...)` in the immutable Q3/P4/cache
   suites still lands after the refactor.

## Rationale

- **retrieve-level over ask-level scope.** Scoping to `retrieve()` keeps the **frozen per-query audit
  contract** (`ask.query` stdout line + `query_logs` row) preserved *by construction* — `ask.py`
  isn't touched, so `test_ask*.py` stays green trivially and no audit re-verification is needed. It
  also isolates the new dependency to one module. Graphing the whole `/ask` (validate → cache →
  retrieve → generate → guard → log) is a larger, later move; the read path is the natural first
  graph and the one whose parallelism actually motivates a graph.
- **True parallel branches over a hidden-gather node.** The whole teaching point of P5 is that
  structure that was implicit (`asyncio.gather`, an `if rerank_enabled`) becomes *visible* as edges
  and nodes — and the fan-out/fan-in is precisely what forces the reducer lesson. A single node that
  hides the gather would compile and run identically but teach nothing; it would be a graph in name
  only.
- **Reducer over disjoint keys for `timings`.** The arms *could* have written disjoint `sem`/`lex`
  keys and had `fuse` assemble timings — but then the parallel-write conflict (and its resolution)
  never appears, which is the single most important LangGraph concept this milestone exists to show.
- **Lazy, memoized compile.** Mirrors the P0 warm-up / `_get_reranker` "build the heavy thing once"
  pattern and keeps the import graph clean (the reranker's cross-encoder stays the only torch user,
  loaded on demand).

## Alternatives rejected

- **Graph the whole `/ask` request now.** Bigger blast radius, touches the frozen audit contract, and
  the linear parts (validate/log) don't benefit from graph structure. Deferred — the read path is the
  right first graph.
- **One node that internally runs `asyncio.gather`.** Byte-identical behaviour, zero teaching value
  (concurrency and the reducer both stay hidden). Rejected — defeats P5's purpose.
- **Model the embed router (supplied-vector / HyDE / plain) as three conditional-edge branches.**
  Considered, but the three cases are a two-line if-ladder with a fail-open `try`; splitting them into
  nodes adds edges without clarifying anything. Kept as one `embed` node containing the verbatim
  if/else; the *illustrative* conditional edge is the rerank gate.
- **A LangChain LCEL chain (`Runnable` / `|`) instead of LangGraph.** LCEL expresses linear/parallel
  composition but not conditional routing or shared mutable state as cleanly, and `CLAUDE.md` names
  LangGraph as the control-flow owner (LangChain stays thin). Rejected.
- **Add checkpointing/persistence (`MemorySaver`) while we're here.** A single-shot read needs no
  resumable state or human-in-the-loop pause. Noted in the teaching doc as what a graph *could* buy;
  not built (naive-first).

## Consequences

- **`retrieve.py` internals rewritten**, public surface unchanged: a `GraphState` TypedDict (every key
  a channel; `timings` carries the reducer), module-level `_embed_node` / `_semantic_node` /
  `_lexical_node` / `_fuse_node` / `_rerank_node` coroutines (verbatim bodies of the old
  closures/blocks), `_route_after_fuse`, a memoized `_graph()` builder, and a thin `retrieve()`
  wrapper that seeds the state, `ainvoke`s the graph, and assembles the `RetrievalResult`.
- **New dependency pinned:** `langgraph==1.2.9` in `backend/requirements.txt`. Was already resolved
  transitively; proven to coexist with the load-bearing pins (`langchain-community<0.4`,
  `langchain-core 1.5.x`, `redis<6.5`, `ragas 0.4.3`) and imported lazily so it never loads at app
  import time and does **not** pull torch into the app path.
- **No new `Settings` knobs** (pure refactor). No schema/migration change. `ask.py` and the audit
  contract untouched.
- **Immutable tests untouched and green:** `test_retrieve.py` (Q3), `test_retrieve_hyde.py` (P4), and
  the whole `test_ask*.py` / `test_cache.py` family pass unedited — that *is* the byte-identical
  proof. New coverage: `test_read_graph.py` (9) — 2 red-first structural tests (`_graph()` memoized +
  the expected node set) and 7 characterisation/safety-net tests (the byte-identical end-to-end
  result, the rerank + HyDE conditional edges, the supplied-embedding short-circuit, and the
  reducer merging both arms' timings). Suite **182+5 → 191+5**, 0-skip.
- **No eval re-run.** Retrieval output is byte-identical and nothing that changes *what gets embedded
  or searched* moved, so hit@k/MRR cannot change; the dev DB / `golden.jsonl` are not touched.
- **Revisit if** we later graph the full `/ask` request (would fold retrieve in as a subgraph), or add
  checkpointing/human-in-the-loop, or want per-node tracing (LangSmith) hung off the graph.
