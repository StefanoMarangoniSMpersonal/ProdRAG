"""Spec: retrieve() drops chunks below the reranker-logit relevance floor.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the pre-Phase-3
relevance threshold. Kept OUT of the immutable `test_retrieve.py` / `test_retrieve_rerank.py`
on purpose: those pin the floor-OFF behavior (the floor defaults to None), so they stay
green untouched; this file pins the floor-ON path.

What's pinned here:
  1. When `rerank_score_floor` is set (and rerank is ON), retrieve() drops every chunk the
     cross-encoder scored BELOW the floor, keeping the survivors in reranked order.
  2. A floor above every score empties the result — that empty list is what lets
     generate() refuse ("nothing relevant"), the whole point of the threshold.
  3. The floor narrows only the RETURNED chunks; `candidate_chunk_ids` (the pre-rerank
     fused-pool snapshot the per-query log reports) is untouched.

The floor acts on the reranker logit, so these run with rerank ON and a fake reranker
that stamps a known score per chunk (so "below the floor" is exact and offline — no torch,
no Gemini). `embed_texts` is faked and embeddings sit on known axes so the pre-rerank
(semantic) pool order is deterministic. Real Postgres via the committing `session_factory`,
because retrieve() owns its sessions (the orchestrate.py pattern).
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


class _MixedScoreReranker:
    """Async fake `rerank`: stamps a per-content score, sorts desc, cuts to top_n.

    Unlike a single-sentinel fake, this assigns DIFFERENT scores per chunk (some below,
    some above the floor), so the test can watch retrieve() drop the sub-floor ones AFTER
    the reranker ran and ordered the pool.
    """

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    async def __call__(
        self, query: str, chunks: list[ScoredChunk], *, top_n: int
    ) -> list[ScoredChunk]:
        rescored = [
            ScoredChunk(chunk=sc.chunk, score=self._scores[sc.chunk.content])
            for sc in chunks
        ]
        rescored.sort(key=lambda sc: sc.score, reverse=True)
        return rescored[:top_n]


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as session:
        doc = Document(filename="thr.md", source_uri="file://thr-key/thr.md")
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
    async with factory() as session:
        session.add(
            Chunk(
                document_id=document_id,
                owner_id=DEV_OWNER_ID,
                ordinal=ordinal,
                content=content,
                embedding=embedding,
                char_count=len(content),
            )
        )
        await session.commit()


def _settings(*, floor: float | None) -> Settings:
    """Real Settings: rerank ON, a wide pool, and the floor under test."""
    return Settings(
        rerank_enabled=True, retrieval_candidate_k=10, rerank_score_floor=floor
    )


async def _seed_three(factory: async_sessionmaker[AsyncSession]) -> None:
    """c0/c1/c2, each further off the query axis -> semantic (fused) order c0,c1,c2.

    Contents share no lexeme with the query, so the lexical arm is empty and the fused
    pool equals the semantic order — a clean, known pool for the reranker to re-score."""
    doc_id = await _seed_document(factory)
    for i in range(3):
        await _seed_chunk(
            factory, doc_id, ordinal=i, content=f"c{i}", embedding=_axis_vec(1.0, float(i))
        )


async def test_floor_drops_subthreshold_chunks_after_rerank(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)

    # Reranker scores: c1 lands BELOW a 0.0 floor; c0/c2 above. Post-rerank order is
    # c0(8), c2(5), c1(-2); the floor then drops c1.
    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))
    monkeypatch.setattr(
        retrieve_mod, "rerank", _MixedScoreReranker({"c0": 8.0, "c1": -2.0, "c2": 5.0})
    )
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: _settings(floor=0.0))

    result = await retrieve("unmatched query", k=3)

    # Sub-floor c1 dropped; survivors kept in reranked order, carrying the reranker score.
    assert [sc.chunk.content for sc in result.chunks] == ["c0", "c2"]
    assert [sc.score for sc in result.chunks] == [8.0, 5.0]
    # The pre-rerank snapshot still holds the FULL fused pool — the floor narrowed only the
    # returned chunks, not what the log reports retrieval found.
    assert len(result.candidate_chunk_ids) == 3
    assert {sc.chunk.id for sc in result.chunks} <= set(result.candidate_chunk_ids)


async def test_floor_can_empty_the_result(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)

    # Every score is below a floor of 100.0 -> the reranker ran, but nothing survives.
    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))
    monkeypatch.setattr(
        retrieve_mod, "rerank", _MixedScoreReranker({"c0": 8.0, "c1": -2.0, "c2": 5.0})
    )
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: _settings(floor=100.0))

    result = await retrieve("unmatched query", k=3)

    # Empty result -> this is the signal generate() turns into an explicit refusal.
    assert result.chunks == []
    # But retrieval still HAPPENED and is auditable: the full pool is on the snapshot.
    assert len(result.candidate_chunk_ids) == 3
