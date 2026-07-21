"""Q10 spec: `RetrievalResult` carries the PRE-rerank order, not just the top-k.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the one field Q10
adds to the retrieval result. Kept OUT of the immutable `test_retrieve.py` / the Q7
`test_retrieve_rerank.py` on purpose (the Q7 precedent): those files pin what they
pinned, and a new claim belongs in a new file.

WHY the field exists: `retrieve()` returns only the FINAL ranking, so once the
cross-encoder reorders the pool the earlier ranking is gone — and the per-query log Q10
writes is supposed to record "retrieved ids" AND "reranked order" (the CLAUDE.md logging
contract). Without this field the /ask endpoint could only ever report the same list
twice, hiding the retrieve-wide -> rerank-narrow funnel the log exists to expose.

What's pinned here:
  1. rerank ON  -> `candidate_chunk_ids` is the WIDE fused pool, in fused order, while
     `chunks` is the reranked top-k. The two must differ (order AND length), which is
     what proves the pre-rerank ranking really survived.
  2. rerank OFF -> the two agree exactly (pool_k == k), so the field is always safe to
     read and never lies about a funnel that didn't happen.

Same harness as `test_retrieve_rerank.py`: real Postgres via the committing
`session_factory` (retrieve owns its sessions), `embed_texts` + `rerank` faked so
nothing touches Gemini or torch, `_axis_vec` for an exact cosine order.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.retrieve.retrieve as retrieve_mod
from app.config import Settings
from app.models import DEV_OWNER_ID, Chunk, Document
from app.retrieve.retrieve import retrieve
from app.retrieve.types import ScoredChunk

DIMS = 768


def _axis_vec(*components: float) -> list[float]:
    """A 768-dim vector: given leading components, rest zero (exact cosine order)."""
    v = [0.0] * DIMS
    for i, c in enumerate(components):
        v[i] = c
    return v


class _RecordingEmbedder:
    """Async fake `embed_texts`: returns a fixed vector per input."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


async def _reversing_rerank(
    query: str, chunks: list[ScoredChunk], *, top_n: int
) -> list[ScoredChunk]:
    """Fake cross-encoder: reverses the pool and cuts to top_n.

    Reversing guarantees the final order differs from the fused order, so a test can
    tell the two rankings apart.
    """
    return list(reversed(chunks))[:top_n]


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as session:
        doc = Document(filename="q10.md", source_uri="file://q10-key/q10.md")
        session.add(doc)
        await session.flush()
        doc_id = doc.id
        await session.commit()
    return doc_id


async def _seed_chunks(
    factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID, count: int
) -> list[int]:
    """Seed `count` chunks on a known cosine axis; return their ids nearest-first."""
    ids: list[int] = []
    async with factory() as session:
        for i in range(count):
            content = f"c{i}"
            chunk = Chunk(
                document_id=document_id,
                owner_id=DEV_OWNER_ID,
                ordinal=i,
                content=content,
                embedding=_axis_vec(1.0, float(i)),
                char_count=len(content),
            )
            session.add(chunk)
            await session.flush()
            ids.append(chunk.id)
        await session.commit()
    return ids


async def test_candidate_ids_hold_the_pre_rerank_pool(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = await _seed_document(session_factory)
    ids = await _seed_chunks(session_factory, doc_id, 5)

    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))
    monkeypatch.setattr(retrieve_mod, "rerank", _reversing_rerank)
    # Pool 4 (< the 5 seeded), final k 2 (< the pool): all three sizes distinct.
    monkeypatch.setattr(
        retrieve_mod,
        "get_settings",
        lambda: Settings(rerank_enabled=True, retrieval_candidate_k=4),
    )

    result = await retrieve("unmatched query", k=2)

    # The wide fused pool, in fused (here: nearest-first) order — the ranking that would
    # otherwise be destroyed by the rerank below.
    assert result.candidate_chunk_ids == ids[:4]
    # The final ranking is the reranker's (reversed), cut to k -> genuinely different.
    assert [sc.chunk.id for sc in result.chunks] == [ids[3], ids[2]]
    assert result.candidate_chunk_ids != [sc.chunk.id for sc in result.chunks]


async def test_candidate_ids_equal_final_ids_when_rerank_off(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = await _seed_document(session_factory)
    ids = await _seed_chunks(session_factory, doc_id, 3)

    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: Settings())  # rerank OFF

    result = await retrieve("unmatched query", k=3)

    # No funnel happened, so the field reports the same ranking as `chunks` — it never
    # implies a rerank that didn't run.
    assert result.candidate_chunk_ids == ids
    assert result.candidate_chunk_ids == [sc.chunk.id for sc in result.chunks]
