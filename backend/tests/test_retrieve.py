"""Q3 retrieve-orchestrator spec: query STRING -> embed -> semantic search -> result.

The *immutable spec* for Q3 (CLAUDE.md "test-first & test-immutable"): written and
watched fail (red, `ModuleNotFoundError`) before `app/retrieve/retrieve.py` and
`RetrievalResult` exist. The code bends to it, never the reverse.

What Q3 is: the first end-to-end retrieve. `search_semantic` (Q2) takes a query
*vector*; `retrieve(query: str, ...)` is the caller above it that turns the query STRING
into that vector — via the existing embed seam, in the RETRIEVAL_QUERY role — owns its
own session, runs the search, and returns a `RetrievalResult` (query, chunks, timings).

Two things are pinned here that a silent regression could break:
  1. The query is embedded in the QUERY role (`as_retrieval_query`), NOT the document
     role — the asymmetry is exactly what makes query<->document similarity meaningful.
     We assert on the *actual string handed to embed_texts*, so forgetting the wrapper
     (or using the document wrapper) goes red.
  2. The vector flows through to a REAL pgvector search and comes back correctly ordered
     and scored — i.e. `retrieve` really wires embed -> search, it doesn't just embed.

Real Postgres (the `pgvector/pgvector:pg16` testcontainer) via the committing
`session_factory` fixture, because `retrieve` opens its OWN session (it owns
`SessionLocal`, the orchestrate.py pattern) and commits are what a separate reader sees.
The Gemini API is NOT called: `embed_texts` is monkeypatched to a fake that records its
input and returns a known axis vector, so ordering stays exact and offline.

Vectors are placed on known axes via `_axis_vec` (same helper as test_semantic.py; do
NOT reuse `_vec(seed)` from test_write.py — its cyclic values have no cosine order).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.retrieve.retrieve as retrieve_mod
from app.ingest.embed import as_retrieval_query
from app.models import DEV_OWNER_ID, Chunk, Document
from app.retrieve.retrieve import retrieve

DIMS = 768


def _axis_vec(*components: float) -> list[float]:
    """A 768-dim vector with the given leading components and the rest zero-filled.

    On known axes, cosine ordering is exact: same-axis vectors have distance 0
    (similarity 1.0); orthogonal vectors have distance 1 (similarity 0.0), regardless of
    magnitude (cosine is scale-invariant)."""
    v = [0.0] * DIMS
    for i, c in enumerate(components):
        v[i] = c
    return v


class _RecordingEmbedder:
    """A fake `embed_texts` that records every input list and returns a fixed vector.

    Async (retrieve awaits it), returns one vector per input text (the `embed_texts`
    contract), and stashes the inputs so a test can assert the query was wrapped in the
    RETRIEVAL_QUERY role before embedding."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector for _ in texts]


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Insert a parent `documents` row (chunks.document_id is a NOT NULL FK); commit."""
    async with factory() as session:
        doc = Document(filename="q3.md", source_uri="file://q3-key/q3.md")
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


# --- the query is embedded in the QUERY role ----------------------------------------


async def test_retrieve_wraps_query_in_query_role(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = _RecordingEmbedder(_axis_vec(1.0))
    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", embedder)

    await retrieve("how do quokkas nap?", k=5)

    # Exactly one embed call, given exactly the query wrapped in the RETRIEVAL_QUERY
    # role. Skipping the wrapper, or using as_retrieval_document, makes this go red.
    assert embedder.calls == [[as_retrieval_query("how do quokkas nap?")]]


# --- real pgvector order, end to end (embed -> search) ------------------------------


async def test_retrieve_orders_by_similarity_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = await _seed_document(session_factory)
    # A on query axis (nearest); B leans 45deg (middle); C orthogonal (farthest).
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="A", embedding=_axis_vec(1.0)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=1, content="B", embedding=_axis_vec(1.0, 1.0)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=2, content="C", embedding=_axis_vec(0.0, 1.0)
    )

    # The fake embeds the query to axis 0, so the DB search should order A, B, C.
    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))

    result = await retrieve("q", k=3)

    assert [sc.chunk.content for sc in result.chunks] == ["A", "B", "C"]


# --- k truncates ---------------------------------------------------------------------


async def test_retrieve_k_truncates(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))

    result = await retrieve("q", k=2)

    assert len(result.chunks) == 2
    assert [sc.chunk.content for sc in result.chunks] == ["chunk-0", "chunk-1"]


# --- result shape: query echoed, timings recorded, scores are RRF-fused (Q6) --------


async def test_retrieve_result_shape_and_timings(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = await _seed_document(session_factory)
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

    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))

    result = await retrieve("my question", k=2)

    # The query string is echoed back on the result (identifies what was retrieved).
    assert result.query == "my question"
    # Every stage is timed (the eval-substrate rule: retrieval never runs silently).
    assert set(result.timings_ms) >= {"embed_ms", "search_ms", "total_ms"}
    # Score is the RRF FUSED score (Q6), not cosine: the lexical arm matches nothing
    # here (content "near"/"orthogonal" share no lexeme with "my question"), so fusion
    # reduces to the semantic order -> "near" is rank 1, above "orthogonal", both small
    # positive RRF values (~1/61), never the cosine 1.0/0.0 of the pre-fusion Q3 spec.
    by_content = {sc.chunk.content: sc.score for sc in result.chunks}
    assert result.chunks[0].chunk.content == "near"
    assert by_content["near"] > by_content["orthogonal"] > 0.0
    assert by_content["near"] < 1.0
