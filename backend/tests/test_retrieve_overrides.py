"""Spec: retrieve() honours per-call overrides for rerank_enabled / rerank_score_floor.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the UI live-controls
feature. The chat UI flips the reranker and sets the floor PER MESSAGE, so those knobs
must be overridable at the `retrieve()` call, not only via the process-global Settings.
Kept OUT of the immutable `test_retrieve*.py` on purpose: those pin the no-override
behaviour (both kwargs default to "use settings"), so they stay green untouched; this file
pins the override path.

What's pinned here (each proves an override BEATS the settings value):
  1. `rerank_enabled=False` overrides `settings.rerank_enabled=True` → the cross-encoder
     never runs; the fused (RRF) pool is returned as-is.
  2. `rerank_enabled=True` overrides `settings.rerank_enabled=False` → the reranker runs
     even though settings had it off.
  3. `rerank_score_floor=<v>` overrides `settings.rerank_score_floor=None` → the floor
     drops sub-threshold chunks even though settings set no floor.
  4. An explicit `rerank_score_floor=None` override DISABLES the floor even when
     `settings.rerank_score_floor` sets one (the sentinel distinguishes "disable" from
     "not overridden").
  5. Omitting both kwargs falls back to the settings values (the sentinel default path).

Same offline harness as test_retrieve_threshold.py: real Postgres via the committing
`session_factory` (retrieve() owns its sessions), a fake `embed_texts` putting the pool on
a known axis, and a fake `rerank` that stamps a known score per chunk content — so "below
the floor" and "did the reranker run" are exact, with no torch and no Gemini.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.retrieve.retrieve as retrieve_mod
from app.config import Settings
from app.models import DEV_OWNER_ID, Chunk, Document
from app.retrieve.retrieve import retrieve
from app.retrieve.types import ScoredChunk

DIMS = 768


def _axis_vec(*components: float) -> list[float]:
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


class _RecordingReranker:
    """Async fake `rerank`: records whether it ran, then stamps a per-content score."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores
        self.called = False

    async def __call__(
        self, query: str, chunks: list[ScoredChunk], *, top_n: int
    ) -> list[ScoredChunk]:
        self.called = True
        rescored = [
            ScoredChunk(chunk=sc.chunk, score=self._scores[sc.chunk.content])
            for sc in chunks
        ]
        rescored.sort(key=lambda sc: sc.score, reverse=True)
        return rescored[:top_n]


async def _seed_three(factory: async_sessionmaker[AsyncSession]) -> None:
    """c0/c1/c2, each further off the query axis → semantic (fused) order c0,c1,c2.

    Contents share no lexeme with the query, so the lexical arm is empty and the fused
    pool equals the semantic order — a clean, known pool for the reranker to re-score.
    """
    async with factory() as session:
        doc = Document(filename="ovr.md", source_uri="file://ovr-key/ovr.md")
        session.add(doc)
        await session.flush()
        doc_id = doc.id
        await session.commit()
    for i in range(3):
        async with factory() as session:
            session.add(
                Chunk(
                    document_id=doc_id,
                    owner_id=DEV_OWNER_ID,
                    ordinal=i,
                    content=f"c{i}",
                    embedding=_axis_vec(1.0, float(i)),
                    char_count=2,
                )
            )
            await session.commit()


def _settings(*, rerank_enabled: bool, floor: float | None) -> Settings:
    return Settings(
        rerank_enabled=rerank_enabled,
        retrieval_candidate_k=10,
        rerank_score_floor=floor,
    )


# The reranker order: c0(8) > c2(5) > c1(-2). A 0.0 floor drops c1.
SCORES = {"c0": 8.0, "c1": -2.0, "c2": 5.0}


def _wire(monkeypatch, factory, settings) -> _RecordingReranker:
    reranker = _RecordingReranker(SCORES)
    monkeypatch.setattr(retrieve_mod, "SessionLocal", factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))
    monkeypatch.setattr(retrieve_mod, "rerank", reranker)
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: settings)
    return reranker


async def test_rerank_enabled_false_override_beats_settings_on(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)
    reranker = _wire(
        monkeypatch,
        session_factory,
        _settings(rerank_enabled=True, floor=None),
    )

    result = await retrieve("unmatched query", k=3, rerank_enabled=False)

    # The reranker never ran; the fused RRF pool is returned in semantic order, and the
    # scores are the RRF scores (NOT the reranker's 8/5/-2).
    assert reranker.called is False
    assert [sc.chunk.content for sc in result.chunks] == ["c0", "c1", "c2"]
    assert 8.0 not in {sc.score for sc in result.chunks}


async def test_rerank_enabled_true_override_beats_settings_off(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)
    reranker = _wire(
        monkeypatch,
        session_factory,
        _settings(rerank_enabled=False, floor=None),
    )

    result = await retrieve("unmatched query", k=3, rerank_enabled=True)

    # Reranker ran despite settings having it off: order + scores are the reranker's.
    assert reranker.called is True
    assert [sc.chunk.content for sc in result.chunks] == ["c0", "c2", "c1"]
    assert [sc.score for sc in result.chunks] == [8.0, 5.0, -2.0]


async def test_floor_override_beats_settings_none(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)
    _wire(monkeypatch, session_factory, _settings(rerank_enabled=True, floor=None))

    result = await retrieve("unmatched query", k=3, rerank_score_floor=0.0)

    # settings had NO floor, but the per-call floor drops sub-threshold c1.
    assert [sc.chunk.content for sc in result.chunks] == ["c0", "c2"]


async def test_explicit_none_override_disables_settings_floor(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)
    _wire(monkeypatch, session_factory, _settings(rerank_enabled=True, floor=0.0))

    # settings would drop c1 at floor 0.0, but an explicit None override disables it.
    result = await retrieve("unmatched query", k=3, rerank_score_floor=None)

    assert [sc.chunk.content for sc in result.chunks] == ["c0", "c2", "c1"]


async def test_omitting_overrides_falls_back_to_settings(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_three(session_factory)
    _wire(monkeypatch, session_factory, _settings(rerank_enabled=True, floor=0.0))

    # No override → the sentinel default → the settings floor (0.0) applies → c1 dropped.
    result = await retrieve("unmatched query", k=3)

    assert [sc.chunk.content for sc in result.chunks] == ["c0", "c2"]
