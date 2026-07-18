"""Q7 retrieve-integration spec: retrieve() widens the pool, reranks it, returns top-k.

The *immutable spec* for wiring rerank into `retrieve()` (CLAUDE.md "test-first &
test-immutable"). Kept OUT of the immutable `test_retrieve.py` on purpose: those 4 Q3
specs pin the rerank-OFF behavior (they never enable it), and rerank defaults off, so
they stay green untouched. This file pins the rerank-ON path.

What's pinned here (retrieve()'s ORCHESTRATION of rerank, with a fake reranker):
  1. When `rerank_enabled`, each arm fetches the WIDE pool (`retrieval_candidate_k`),
     not the final `k`. Reranking a pool no bigger than `k` could never reorder the
     top-k, so the pool must be widened first — the retrieve-wide -> rerank-narrow
     funnel.
  2. retrieve() hands the fused pool (query + candidate chunks) to `rerank`.
  3. retrieve() returns rerank's order, truncated to `k` (the final size the caller
     asked for — UNCHANGED by Q7).
  4. A `rerank_ms` timing is recorded (retrieval never runs silently — the
     eval-substrate rule).

Real Postgres (the committing `session_factory`) because retrieve() opens its own
sessions; `embed_texts` and `rerank` are monkeypatched to fakes so nothing hits Gemini
or torch, and `get_settings` is swapped to flip `rerank_enabled` on and shrink the pool
so the widen is observable. `_axis_vec` places embeddings on known axes so the
pre-rerank (semantic) order is exact (same helper/rationale as test_retrieve.py).
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
_RERANK_SENTINEL = (
    100.0  # the score the fake reranker stamps, so we can prove it replaced RRF
)


def _axis_vec(*components: float) -> list[float]:
    """A 768-dim vector: given leading components, rest zero (exact cosine order)."""
    v = [0.0] * DIMS
    for i, c in enumerate(components):
        v[i] = c
    return v


class _RecordingEmbedder:
    """Async fake `embed_texts`: returns a fixed vector per input, records the calls."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector for _ in texts]


class _RecordingReranker:
    """Async fake `rerank`: records the pool, returns it REVERSED, cut to top_n.

    Reversing is a deliberately different order from the incoming (semantic) ranking, so
    a test can tell rerank's output apart from the pre-rerank order. Stamps a sentinel
    score to prove retrieve() carries the reranker's score, not the RRF score.
    """

    def __init__(self) -> None:
        self.query: str | None = None
        self.contents: list[str] | None = None
        self.top_n: int | None = None

    async def __call__(
        self, query: str, chunks: list[ScoredChunk], *, top_n: int
    ) -> list[ScoredChunk]:
        self.query = query
        self.contents = [sc.chunk.content for sc in chunks]
        self.top_n = top_n
        reordered = list(reversed(chunks))[:top_n]
        return [ScoredChunk(chunk=sc.chunk, score=_RERANK_SENTINEL) for sc in reordered]


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as session:
        doc = Document(filename="q7.md", source_uri="file://q7-key/q7.md")
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


def _rerank_on_settings(*, candidate_k: int) -> Settings:
    """Real Settings with rerank ON and the pool shrunk so the widen is observable."""
    return Settings(rerank_enabled=True, retrieval_candidate_k=candidate_k)


async def test_retrieve_widens_pool_reranks_and_truncates(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = await _seed_document(session_factory)
    # 5 chunks, each further off the query axis than the last -> exact nearest-first
    # order c0..c4. Contents share no lexeme with the query, so the lexical arm is
    # empty and the fused pool == the semantic order.
    for i in range(5):
        await _seed_chunk(
            session_factory,
            doc_id,
            ordinal=i,
            content=f"c{i}",
            embedding=_axis_vec(1.0, float(i)),
        )

    reranker = _RecordingReranker()
    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))
    monkeypatch.setattr(retrieve_mod, "rerank", reranker)
    # Pool = 4 (< the 5 seeded), final k = 2 (< the pool). This makes all three sizes
    # distinct, so each claim below is unambiguous.
    monkeypatch.setattr(
        retrieve_mod, "get_settings", lambda: _rerank_on_settings(candidate_k=4)
    )

    result = await retrieve("unmatched query", k=2)

    # (1) The pool the reranker saw is retrieval_candidate_k (4) wide -- NOT the final
    # k (2), and NOT all 5. Proves retrieve fetched the wide pool before reranking.
    assert reranker.contents == ["c0", "c1", "c2", "c3"]
    # (2) retrieve handed the reranker the query and asked for top_n == the final k.
    assert reranker.query == "unmatched query"
    assert reranker.top_n == 2
    # (3) retrieve returns the reranker's order (reversed pool), cut to k=2, carrying
    # the reranker's score (not the RRF value).
    assert [sc.chunk.content for sc in result.chunks] == ["c3", "c2"]
    assert len(result.chunks) == 2
    assert all(sc.score == _RERANK_SENTINEL for sc in result.chunks)
    # (4) the rerank stage is timed.
    assert "rerank_ms" in result.timings_ms
