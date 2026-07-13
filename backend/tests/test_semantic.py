"""Q2 semantic-search spec: `search_semantic` returns the k nearest chunks, scored.

This is the *immutable spec* for Q2 (CLAUDE.md "test-first & test-immutable"): written
and watched fail (red) before `app/retrieve/semantic.py`, `app/retrieve/types.py`, and
the `retrieval_hnsw_ef_search` setting existed. Once written it does not change to
accommodate the code — the code bends to it.

What Q2 is: naive semantic (vector) search over the HNSW index Q1 switched on. Given a
query *vector* (Q3 adds the query-string -> vector step), return the k chunks whose
stored embeddings are nearest by cosine, each wrapped in a `ScoredChunk` carrying a
relevance `score`. The score is a cosine **similarity** (`1 - cosine_distance`):
higher = more relevant, the direction lexical `ts_rank` (Q5) and rerank (Q7) will use.

These assertions run against a REAL Postgres (the throwaway `pgvector/pgvector:pg16`
testcontainer) because the `<=>` cosine operator is a pgvector feature SQLite can't
stand in for. They use the committing `session_factory` fixture (TRUNCATE teardown) so
seeded rows are visible the way an independent retrieval query would be. They ERROR
loudly (never SKIP) if Docker is down — the "a SKIP is a false green" stance.

Vectors here are placed on known axes via `_axis_vec` so cosine ordering is exact and
obvious (do NOT reuse `_vec(seed)` from test_write.py — its cyclic values have no cosine
monotonicity). The stored vectors need not be unit-length: cosine is scale-invariant, so
`_axis_vec(1.0)` and a longer vector on the same axis have distance 0.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DEV_OWNER_ID, Chunk, Document
from app.retrieve.semantic import search_semantic

DIMS = 768

# A second owner, distinct from DEV_OWNER_ID, for the visibility-filter test.
OTHER_OWNER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _axis_vec(*components: float) -> list[float]:
    """A 768-dim vector with the given leading components and the rest zero-filled.

    Placing vectors on known axes makes cosine ordering exact: two vectors on the same
    axis have cosine distance 0 (score 1.0); orthogonal vectors have distance 1 (score
    0.0), regardless of magnitude (cosine is scale-invariant)."""
    v = [0.0] * DIMS
    for i, c in enumerate(components):
        v[i] = c
    return v


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Insert a parent `documents` row (chunks.document_id is a NOT NULL FK); commit."""
    async with factory() as session:
        doc = Document(filename="q2.md", source_uri="file://q2-key/q2.md")
        session.add(doc)
        await session.flush()
        doc_id = doc.id
        await session.commit()
    return doc_id


async def _seed_chunk(
    factory: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
    *,
    ordinal: int,
    content: str,
    embedding: list[float],
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> None:
    """Insert one committed `chunks` row with a given embedding and owner."""
    async with factory() as session:
        session.add(
            Chunk(
                document_id=document_id,
                owner_id=owner_id,
                ordinal=ordinal,
                content=content,
                embedding=embedding,
                char_count=len(content),
            )
        )
        await session.commit()


# --- ordering: nearest by cosine comes first ----------------------------------------


async def test_search_semantic_orders_by_similarity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # Query on axis 0. A is on axis 0 (identical direction, nearest); B leans 45deg onto
    # axis 1 (middle); C is orthogonal on axis 1 (farthest).
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="A", embedding=_axis_vec(1.0)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=1, content="B", embedding=_axis_vec(1.0, 1.0)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=2, content="C", embedding=_axis_vec(0.0, 1.0)
    )

    async with session_factory() as session:
        results = await search_semantic(session, _axis_vec(1.0), k=3)

    assert [r.chunk.content for r in results] == ["A", "B", "C"]


# --- score is a cosine similarity (higher = nearer) ---------------------------------


async def test_search_semantic_score_is_similarity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # Identical-direction chunk -> similarity 1.0; orthogonal chunk -> similarity 0.0.
    # This pins the score DIRECTION: a regression to raw cosine distance (0.0 for the
    # near chunk, 1.0 for the far one) flips these and goes red.
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="near", embedding=_axis_vec(1.0)
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=1,
        content="orthogonal",
        embedding=_axis_vec(0.0, 1.0),
    )

    async with session_factory() as session:
        results = await search_semantic(session, _axis_vec(1.0), k=2)

    by_content = {r.chunk.content: r.score for r in results}
    assert by_content["near"] == pytest.approx(1.0)
    assert by_content["orthogonal"] == pytest.approx(0.0)


# --- k truncates ---------------------------------------------------------------------


async def test_search_semantic_k_truncates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    for i in range(3):
        await _seed_chunk(
            session_factory,
            doc_id,
            ordinal=i,
            content=f"chunk-{i}",
            # Each successively farther off axis 0 so ordering is deterministic.
            embedding=_axis_vec(1.0, float(i)),
        )

    async with session_factory() as session:
        results = await search_semantic(session, _axis_vec(1.0), k=2)

    assert len(results) == 2
    assert [r.chunk.content for r in results] == ["chunk-0", "chunk-1"]


# --- owner filter: another owner's chunk is invisible --------------------------------


async def test_search_semantic_filters_by_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # The other owner's chunk is the BEST vector match (identical to the query), so if
    # the owner filter were missing it would rank first. It must not appear at all.
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=0,
        content="other-owner",
        embedding=_axis_vec(1.0),
        owner_id=OTHER_OWNER_ID,
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=1,
        content="mine",
        embedding=_axis_vec(1.0, 0.5),
        owner_id=DEV_OWNER_ID,
    )

    async with session_factory() as session:
        results = await search_semantic(session, _axis_vec(1.0), k=10)

    assert [r.chunk.content for r in results] == ["mine"]
