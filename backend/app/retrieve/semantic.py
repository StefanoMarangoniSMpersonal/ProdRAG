"""Q2 — semantic retrieval: query vector -> the k nearest chunks, scored.

The first stage of the read side that actually queries the substrate Q1 switched
on (the HNSW index on `chunks.embedding`). Given a query *embedding* (Q3 adds the
query-string -> vector step; this stage takes the vector so it stays a pure DB
operation), it returns the k chunks whose stored vectors are nearest by **cosine**,
each wrapped in a `ScoredChunk`.

Why cosine: the embeddings are L2-normalized at ingest, so cosine similarity equals
inner product, and it's the metric the Q1 HNSW index was built for
(`vector_cosine_ops`). pgvector exposes cosine *distance* via the `<=>` operator,
surfaced in SQLAlchemy as `Chunk.embedding.cosine_distance(vec)` — 0 for identical
directions, up to 2 for opposite.

Distance -> score: callers want "how relevant", and the package convention is
higher = more relevant (so semantic/lexical/rerank scores all point the same way —
see `types.py`). So we store **similarity = 1 - distance**, not the raw distance.
We `SELECT` the distance expression alongside the row and reuse it for both the
ordering and the score, so they can never disagree.

Naive-first: this is a single `ORDER BY distance LIMIT k`. Rejected the exact
brute-force scan (compare the query against every row) — accurate but O(rows); the
HNSW *approximate* index exists precisely to avoid that, trading a little recall for
sublinear search.

ef_search recall knob: HNSW is approximate, and `hnsw.ef_search` sets how many graph
candidates the search keeps in flight — higher = better recall, slower. It's a
runtime setting scoped per transaction. We apply it via
`set_config('hnsw.ef_search', :v, true)` (the function form — plain `SET` doesn't
accept bound parameters; `is_local=true` = LOCAL, so it auto-reverts at transaction
end and never leaks to another query on the connection). Default is pgvector's 40
(`Settings.retrieval_hnsw_ef_search`), so wiring it changes nothing observable now;
it becomes a config turn, not a code edit, when eval asks for it.

Owner filter: every query is scoped by `owner_id` — the visibility-predicate seam.
Today it's the future RLS boundary (owner = me); it generalizes to role/clearance
filtering later by broadening this one predicate, no reshape of callers or vectors
(deferred).

Module-level seam: `get_settings` is imported as a module global so a test can
monkeypatch `semantic.get_settings` (the `orchestrate.py`/`documents.py` pattern).
The `session` is passed in — Q3's orchestrator owns `SessionLocal`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from app.config import get_settings
from app.models import DEV_OWNER_ID, Chunk
from app.retrieve.types import ScoredChunk

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def search_semantic(
    session: AsyncSession,
    query_embedding: list[float],
    *,
    k: int,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> list[ScoredChunk]:
    """Return the `k` chunks nearest `query_embedding` by cosine, owner-filtered.

    Results are ordered nearest-first; each `ScoredChunk.score` is cosine similarity
    (`1 - cosine_distance`, higher = more relevant). `owner_id` restricts the
    candidate set (the visibility seam). Runs one query under the HNSW `ef_search`
    recall knob from settings.
    """
    settings = get_settings()

    # Apply the recall knob for THIS transaction only. set_config(name, value,
    # is_local): value must be text, is_local=true scopes it to the transaction
    # (auto-reverts). Bound param keeps it injection-safe; plain `SET` can't take a
    # placeholder for the value.
    # We set ef.search, that is env variable, to :ef just for the particular
    # transaction (is_local=true). This enables us to dinamically try many :ef
    await session.execute(
        text("SELECT set_config('hnsw.ef_search', :ef, true)"),
        {"ef": str(settings.retrieval_hnsw_ef_search)},
    )
    # We save ef as local variable before making DB start searching

    # One distance expression, reused for ORDER BY and the score, so they never differ.
    # SQLAlchemy way of writing
    # distance = " embedding <=> '[your_query_vector]' "
    distance = Chunk.embedding.cosine_distance(query_embedding)
    rows = await session.execute(
        select(Chunk, distance.label("distance"))
        .where(Chunk.owner_id == owner_id)  # visibility seam / future RLS boundary
        .order_by(distance)  # ascending: nearest (smallest distance) first
        .limit(k)
    )

    return [ScoredChunk(chunk=chunk, score=1.0 - distance) for chunk, distance in rows]
