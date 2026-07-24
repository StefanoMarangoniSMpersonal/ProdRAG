"""Spec: RetrievalResult.lexical_matched reports whether the lexical arm hit anything.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the per-query lexical
flag. `search_lexical` returns ONLY actual full-text matches (it can return fewer than k,
or zero), so `len(lex) > 0` is exactly "did the lexical arm contribute?" — the boolean the
`query_logs` row records so we can later ask, in SQL, how often lexical fires.

Fully offline: both retrieval arms and the query embed are faked, and `SessionLocal` is a
no-op async context manager, so retrieve()'s orchestration (embed -> arms -> fuse) runs for
real while nothing touches Postgres or Gemini. Rerank stays OFF (the default), so this
pins the flag independently of the threshold work.
"""

from __future__ import annotations

import pytest

import app.retrieve.retrieve as retrieve_mod
from app.models import Chunk
from app.retrieve.retrieve import retrieve
from app.retrieve.types import ScoredChunk


class _FakeSession:
    """A stand-in for an AsyncSession that the faked arms never actually use."""

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingEmbedder:
    async def __call__(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


async def _fake_semantic(session, query_embedding, *, k, owner_id):
    """Semantic always returns one hit, so a result exists regardless of lexical."""
    return [ScoredChunk(chunk=Chunk(id=1, content="semantic hit"), score=0.9)]


def _wire_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieve_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder())
    monkeypatch.setattr(retrieve_mod, "search_semantic", _fake_semantic)


async def test_lexical_matched_true_when_lexical_arm_returns_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_common(monkeypatch)

    async def _lexical_hit(session, query, *, k, owner_id):
        return [ScoredChunk(chunk=Chunk(id=2, content="lexical hit"), score=0.5)]

    monkeypatch.setattr(retrieve_mod, "search_lexical", _lexical_hit)

    result = await retrieve("some query")

    assert result.lexical_matched is True


async def test_lexical_matched_false_when_lexical_arm_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_common(monkeypatch)

    async def _lexical_empty(session, query, *, k, owner_id):
        return []

    monkeypatch.setattr(retrieve_mod, "search_lexical", _lexical_empty)

    result = await retrieve("some query")

    # Semantic still returned a hit, so the result is non-empty — but lexical contributed
    # nothing, and that distinction is exactly what the flag records.
    assert result.lexical_matched is False
    assert len(result.chunks) == 1
