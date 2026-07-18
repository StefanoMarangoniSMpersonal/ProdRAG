"""Live tracer for the retrieval pipeline — the code `/explain-retrieval` drives.

Run one query through the REAL retrieve path and print each stage's output, so retrieval
behaviour can be judged by eye. Retrieval is the part of RAG hardest to reason about
blind; this is the window into it. Read-only: it queries, it never writes.

Usage:
    python -m app.retrieve.explain "<query>" [--k N] [--preview N]

It shows each signal separately AND the final output of the project's own `retrieve()`
(it does NOT reimplement retrieval): the semantic and lexical arms are called directly,
so you see what each contributes, then `retrieve()` is driven to show what it actually
returns — the RRF-fused ranking (Q6), or, when `rerank_enabled`, that pool after the
cross-encoder reranks it (Q7). The grounded generation stage (Q8) is still listed as
"not yet wired" so the output shape stays stable as the pipeline grows.

Note on the embedding line: `retrieve()` embeds the query internally (and reports it as
`embed_ms`). To *display* the query vector's shape and confirm it's unit-length, this
tracer embeds the wrapped query once more itself — a deliberate, clearly-labelled extra
call in this dev-only tool, so the "what got embedded" step is visible rather than
hidden inside `retrieve`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.embed import as_retrieval_query, embed_texts
from app.retrieve.lexical import search_lexical
from app.retrieve.retrieve import retrieve
from app.retrieve.semantic import search_semantic

_PREVIEW_DEFAULT = 160


def _preview(text: str, limit: int) -> str:
    """One-line, whitespace-collapsed, truncated preview with a visible boundary.

    ASCII-only on purpose (same as ingest/inspect.py): the Windows console defaults to
    cp1252 and would crash on fancy glyphs with a UnicodeEncodeError. Plain quotes make
    chunk boundaries clear everywhere.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return f'"{collapsed}"'
    return f'"{collapsed[:limit]}..."  (+{len(collapsed) - limit} chars)'


async def _run(query: str, k: int, preview: int) -> int:
    settings = get_settings()

    # --- Stage 1: query processing ------------------------------------------------
    # The role-wrapped string is what actually gets embedded (RETRIEVAL_QUERY role) --
    # the asymmetry with the document role is what makes query<->passage similarity
    # meaningful. Show it, then embed it once to confirm the vector's shape/norm.
    wrapped = as_retrieval_query(query)
    print(f'Query: "{query}"')
    print(f"  role-wrapped : {wrapped!r}")

    vec = (await embed_texts([wrapped]))[0]
    norm = math.sqrt(sum(x * x for x in vec))
    head = ", ".join(f"{x:+.4f}" for x in vec[:4])
    print(
        f"  embedding    : model={settings.embedding_model} dims={len(vec)} "
        f"|v|={norm:.4f}  head=[{head}, ...]"
    )

    # --- Stage 2: semantic arm ----------------------------------------------------
    # Called directly (not via retrieve()) so we see the PURE semantic ranking -- what
    # this signal contributes on its own, before fusion reorders it. Reuses the vector
    # embedded above. Semantic always returns up to k rows (nearest, relevant or not).
    async with SessionLocal() as session:
        semantic = await search_semantic(session, vec, k=k)
    print(f"\nSemantic arm (k={k}, ef_search={settings.retrieval_hnsw_ef_search}):")
    if not semantic:
        print("  (no chunks -- is anything ingested for this owner?)")
    for rank, sc in enumerate(semantic):
        ch = sc.chunk
        page = f"p{ch.page_number}" if ch.page_number is not None else "p?"
        etype = ch.element_type or "-"
        print(
            f"[{rank:>3}] score={sc.score:+.4f} doc={str(ch.document_id)[:8]} "
            f"ord={ch.ordinal:<3} {page:>4} {etype:<14} {_preview(ch.content, preview)}"
        )

    # --- Stage 3: lexical arm -----------------------------------------------------
    # Full-text search over the same query STRING (not the vector), called directly to
    # show the pure lexical ranking. Unlike semantic (always up to k rows), lexical
    # returns ONLY chunks whose text matches the tsquery, so it may show fewer than k,
    # or none. Contrast the two: proper nouns / exact tokens that vectors rank poorly
    # surface here, often above where semantic put them -- that's what fusion exploits.
    async with SessionLocal() as session:
        lexical = await search_lexical(session, query, k=k)
    print(f"\nLexical arm (k={k}, websearch_to_tsquery + ts_rank_cd):")
    if not lexical:
        print("  (no text matches -- no chunk shares a lexeme with the query)")
    for rank, sc in enumerate(lexical):
        ch = sc.chunk
        page = f"p{ch.page_number}" if ch.page_number is not None else "p?"
        etype = ch.element_type or "-"
        print(
            f"[{rank:>3}] rank={sc.score:.6f} doc={str(ch.document_id)[:8]} "
            f"ord={ch.ordinal:<3} {page:>4} {etype:<14} {_preview(ch.content, preview)}"
        )

    # --- Stage 4: final retrieve() output (RRF, then cross-encoder rerank if on) ---
    # Drive the REAL retrieve() -- this is the pipeline, not a re-implementation. With
    # rerank OFF, its chunks are the two arms above fused by RRF and `score` is the RRF
    # value (rank-based, NOT cosine/ts_rank_cd). With rerank ON, retrieve() fetches a
    # wider pool, fuses it, then a cross-encoder re-scores query+passage jointly and
    # returns the top-k -- so `score` is the reranker's logit and the order can differ
    # from the fused arms (that reordering is the point of Q7). Compare with the arms.
    result = await retrieve(query, k=k)
    if settings.rerank_enabled:
        print(
            f"\nFinal result (RRF pool={settings.retrieval_candidate_k} -> rerank "
            f"[{settings.rerank_model}], top {k}):"
        )
        score_label = "score"
    else:
        print(f"\nFused result (RRF, k_constant={settings.retrieval_rrf_k_constant}):")
        score_label = "rrf"
    if not result.chunks:
        print("  (no chunks -- is anything ingested for this owner?)")
    for rank, sc in enumerate(result.chunks):
        ch = sc.chunk
        page = f"p{ch.page_number}" if ch.page_number is not None else "p?"
        etype = ch.element_type or "-"
        print(
            f"[{rank:>3}] {score_label}={sc.score:.6f} doc={str(ch.document_id)[:8]} "
            f"ord={ch.ordinal:<3} {page:>4} {etype:<14} {_preview(ch.content, preview)}"
        )

    # --- timings + not-yet-wired stages -------------------------------------------
    print("\n" + "-" * 60)
    print(f"timings_ms : {result.timings_ms}")
    if not settings.rerank_enabled:
        print(
            "note: rerank is OFF (set RERANK_ENABLED=true to see Q7 reorder the pool)"
        )
    print("later stages: generate (Q8) -- not yet wired")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m app.retrieve.explain",
        description="Trace one query through the retrieval pipeline (read-only).",
    )
    parser.add_argument("query", help="the query string to retrieve for")
    parser.add_argument(
        "--k",
        type=int,
        default=settings.retrieval_k,
        help=f"number of chunks to retrieve (default: {settings.retrieval_k})",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=_PREVIEW_DEFAULT,
        help=f"max preview chars per chunk (default: {_PREVIEW_DEFAULT})",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.query, args.k, args.preview))


if __name__ == "__main__":
    sys.exit(main())
