"""Q1 schema spec: the HNSW vector index + the full-text `tsv` column are switched on.

This is the *immutable spec* for Q1 (CLAUDE.md "test-first & test-immutable"): written
and watched fail (red) before `003_retrieval_indexes.sql`, the `Chunk.tsv` mapping, and
the conftest migration-glob existed. Once written it does not change to accommodate the
code — the code bends to it.

What Q1 turns on (both derive from data already stored, so no re-ingest):
  - an **HNSW** approximate-nearest-neighbour index on `chunks.embedding` (`idx_chunks_
    embedding_hnsw`, `vector_cosine_ops`) — the substrate Q2 semantic search runs on;
  - a generated **`tsv tsvector`** column + its **GIN** index (`idx_chunks_tsv`) — the
    preprocessed full-text form of `content` that Q5 lexical search matches against.

These assertions run against a REAL Postgres (the throwaway `pgvector/pgvector:pg16`
testcontainer), because HNSW, `tsvector`, and the `<=>` cosine operator are all
Postgres/pgvector features SQLite can't stand in for. They use the committing
`session_factory` fixture (TRUNCATE teardown) so seeded rows are visible the way an
independent retrieval query would. They ERROR loudly (never SKIP) if Docker is down.

Index *presence* is asserted via `pg_indexes`; index *usage* (that the planner actually
chooses the HNSW scan) is an EXPLAIN spot-check in the teaching note, not a brittle test
assertion — a tiny seeded table would let Postgres pick a sequential scan and make a
usage assertion flap.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DEV_OWNER_ID, Chunk, Document

DIMS = 768


def _axis_vec(*components: float) -> list[float]:
    """A 768-dim vector with the given leading components and the rest zero-filled.

    Lets a test place vectors on known axes so cosine ordering is exact and obvious,
    unlike the cyclic `_vec(seed)` in test_write.py (whose values are deliberately
    meaningless to the write stage and have no clean cosine monotonicity)."""
    v = [0.0] * DIMS
    for i, c in enumerate(components):
        v[i] = c
    return v


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Insert a parent `documents` row (chunks.document_id is a NOT NULL FK); commit."""
    async with factory() as session:
        doc = Document(filename="q1.md", source_uri="file://q1-key/q1.md")
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
) -> None:
    """Insert one committed `chunks` row. `tsv` is never supplied — it's a generated
    column the DB fills from `content`, which is exactly what the full-text test proves.
    """
    async with factory() as session:
        session.add(
            Chunk(
                document_id=document_id,
                ordinal=ordinal,
                content=content,
                embedding=embedding,
                char_count=len(content),
            )
        )
        await session.commit()


# --- index presence ------------------------------------------------------------------


async def test_hnsw_and_tsv_indexes_exist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'")
        )
        names = {r[0] for r in rows}

    assert "idx_chunks_embedding_hnsw" in names
    assert "idx_chunks_tsv" in names


# --- semantic: nearest-vector ordering by cosine distance ----------------------------


async def test_nearest_vector_search_orders_by_cosine_distance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # Query points along axis 0. A is nearly parallel to it (tiny cosine distance),
    # B leans onto axis 1 (moderate), C is orthogonal on axis 2 (max distance).
    query = _axis_vec(1.0)
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="A", embedding=_axis_vec(1.0, 0.1)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=1, content="B", embedding=_axis_vec(0.5, 1.0)
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=2,
        content="C",
        embedding=_axis_vec(0.0, 0.0, 1.0),
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Chunk)
                    .where(Chunk.owner_id == DEV_OWNER_ID)  # future RLS boundary
                    .where(Chunk.document_id == doc_id)
                    .order_by(Chunk.embedding.cosine_distance(query))  # pgvector `<=>`
                    .limit(2)  # k truncates: 3 seeded, 2 returned
                )
            )
            .scalars()
            .all()
        )

    assert [r.content for r in rows] == ["A", "B"]


# --- lexical: the generated tsv column matches a full-text query ---------------------


async def test_full_text_tsv_matches_websearch_tsquery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # A distinctive token in one chunk; a decoy chunk without it. `tsv` is populated by
    # the DB from `content` on insert — this both proves the column generates itself and
    # that a websearch tsquery hits only the matching row.
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=0,
        content="The quokka is a small marsupial native to Australia.",
        embedding=_axis_vec(1.0),
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=1,
        content="Unrelated content about distributed systems.",
        embedding=_axis_vec(0.0, 1.0),
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Chunk)
                    .where(Chunk.document_id == doc_id)
                    .where(
                        Chunk.tsv.op("@@")(
                            text("websearch_to_tsquery('english', 'quokka')")
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert "quokka" in rows[0].content
