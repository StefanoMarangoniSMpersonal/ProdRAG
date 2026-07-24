"""Spec: POST /ask honours per-request overrides for rerank / cache / floor.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the UI live-controls
feature at the HTTP boundary. The chat panel sends `rerank_enabled` / `cache_enabled` /
`rerank_score_floor` on each `/ask`; this pins that they take effect for THAT request:

  1. `cache_enabled=false` in the body skips the whole cache block even when the server
     env has the cache ON (no early embed, no cache lookup) — and the inverse,
     `cache_enabled=true`, turns it on even when the env has it OFF.
  2. `rerank_enabled` / `rerank_score_floor` reach `retrieve()` as the effective values
     (the request value when given, else the Settings default) — so the same question can
     be re-run with the reranker flipped or the floor moved.
  3. The response echoes an `applied` block (the effective settings + whether it was a
     cache hit) so the UI can show what produced each answer.

Kept OUT of the immutable `test_ask.py` (which posts `{query}` only and pins the
no-override path). Same offline seams as that file: `retrieve` / `generate` / `embed_query`
/ `cache.*` are monkeypatched, so no Gemini, no torch, no Redis; the request session is the
committing `session_factory` for the audit row (Docker must be up — ERROR, never SKIP).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import ask as ask_mod
from app.cache import CachedAnswer
from app.config import Settings
from app.db import get_session
from app.generate.generate import GenerationResult, TokenUsage
from app.main import app
from app.models import Chunk
from app.retrieve.types import RetrievalResult, ScoredChunk

QUERY = "Who is the CEO of Aurelia Robotics?"


def _scored(chunk_id: int) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(id=chunk_id, content=f"content {chunk_id}"), score=1.0
    )


class _KwargRecordingRetrieve:
    """Async fake `retrieve`: records (query, kwargs) so overrides are inspectable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, query: str, **kwargs) -> RetrievalResult:
        self.calls.append((query, kwargs))
        return RetrievalResult(
            query=query,
            chunks=[_scored(1)],
            timings_ms={"total_ms": 1.0},
            candidate_chunk_ids=[1],
            lexical_matched=True,
        )


class _CannedGenerate:
    async def __call__(self, query: str, chunks: list[ScoredChunk]) -> GenerationResult:
        return GenerationResult(
            answer="The CEO is Marta Silveira.",
            citations=[1],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            timings_ms={"generate_ms": 1.0},
        )


class _SpyCacheGet:
    """Records lookups; returns a configurable hit (or None for a miss)."""

    def __init__(self, hit: CachedAnswer | None = None) -> None:
        self.calls = 0
        self._hit = hit

    async def __call__(self, embedding, owner_id) -> CachedAnswer | None:
        self.calls += 1
        return self._hit


class _SpyEmbed:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, query: str) -> list[float]:
        self.calls += 1
        return [0.0] * 768


async def _noop_cache_set(embedding, owner_id, payload) -> None:
    return None


def _install(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    retrieve: _KwargRecordingRetrieve,
    cache_get: _SpyCacheGet,
    embed: _SpyEmbed,
) -> None:
    monkeypatch.setattr(ask_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(ask_mod, "retrieve", retrieve)
    monkeypatch.setattr(ask_mod, "generate", _CannedGenerate())
    monkeypatch.setattr(ask_mod, "embed_query", embed)
    monkeypatch.setattr(ask_mod.cache, "cache_get", cache_get)
    monkeypatch.setattr(ask_mod.cache, "cache_set", _noop_cache_set)

    async def _override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_cache_disabled_by_request_skips_the_cache_block(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve = _KwargRecordingRetrieve()
    cache_get = _SpyCacheGet()
    embed = _SpyEmbed()
    _install(
        monkeypatch,
        session_factory,
        settings=Settings(cache_enabled=True),  # env ON…
        retrieve=retrieve,
        cache_get=cache_get,
        embed=embed,
    )
    try:
        async with _client() as client:
            resp = await client.post(
                "/ask", json={"query": QUERY, "cache_enabled": False}  # …request OFF
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    # The cache block never ran: no early embed, no lookup — the request's OFF won.
    assert cache_get.calls == 0
    assert embed.calls == 0
    assert len(retrieve.calls) == 1
    assert resp.json()["applied"]["cache_enabled"] is False


async def test_cache_enabled_by_request_turns_it_on(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve = _KwargRecordingRetrieve()
    cache_get = _SpyCacheGet(hit=None)  # a miss, so retrieval still runs
    embed = _SpyEmbed()
    _install(
        monkeypatch,
        session_factory,
        settings=Settings(cache_enabled=False),  # env OFF…
        retrieve=retrieve,
        cache_get=cache_get,
        embed=embed,
    )
    try:
        async with _client() as client:
            resp = await client.post(
                "/ask", json={"query": QUERY, "cache_enabled": True}  # …request ON
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    # The cache block ran despite the env default: it embedded and looked up.
    assert cache_get.calls == 1
    assert embed.calls == 1
    assert resp.json()["applied"]["cache_enabled"] is True


async def test_rerank_and_floor_overrides_reach_retrieve(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve = _KwargRecordingRetrieve()
    _install(
        monkeypatch,
        session_factory,
        settings=Settings(rerank_enabled=False, rerank_score_floor=None),
        retrieve=retrieve,
        cache_get=_SpyCacheGet(),
        embed=_SpyEmbed(),
    )
    try:
        async with _client() as client:
            resp = await client.post(
                "/ask",
                json={
                    "query": QUERY,
                    "rerank_enabled": True,
                    "rerank_score_floor": -3.5,
                },
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    _, kwargs = retrieve.calls[0]
    # The request values reached retrieve as the effective overrides.
    assert kwargs["rerank_enabled"] is True
    assert kwargs["rerank_score_floor"] == -3.5
    # And the response echoes them back.
    applied = resp.json()["applied"]
    assert applied["rerank_enabled"] is True
    assert applied["rerank_score_floor"] == -3.5
    assert applied["cache_hit"] is False


async def test_omitted_knobs_fall_back_to_settings_at_the_endpoint(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve = _KwargRecordingRetrieve()
    _install(
        monkeypatch,
        session_factory,
        settings=Settings(rerank_enabled=True, rerank_score_floor=0.0),
        retrieve=retrieve,
        cache_get=_SpyCacheGet(),
        embed=_SpyEmbed(),
    )
    try:
        async with _client() as client:
            resp = await client.post("/ask", json={"query": QUERY})
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    _, kwargs = retrieve.calls[0]
    # No knobs in the body → the Settings defaults are resolved and passed through.
    assert kwargs["rerank_enabled"] is True
    assert kwargs["rerank_score_floor"] == 0.0
    applied = resp.json()["applied"]
    assert applied["rerank_enabled"] is True
    assert applied["rerank_score_floor"] == 0.0


async def test_applied_reports_cache_hit_on_a_hit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = CachedAnswer(
        answer="cached answer",
        citations=[1],
        valid_citations=[1],
        phantom_citations=[],
        retrieved_chunk_ids=[1],
        final_chunk_ids=[1],
        context_chars=9,
        generation_model="gemini-x",
        sources=[{"id": 1, "content": "content 1", "score": 1.0}],
    )
    retrieve = _KwargRecordingRetrieve()
    _install(
        monkeypatch,
        session_factory,
        settings=Settings(cache_enabled=True),
        retrieve=retrieve,
        cache_get=_SpyCacheGet(hit=hit),
        embed=_SpyEmbed(),
    )
    try:
        async with _client() as client:
            resp = await client.post("/ask", json={"query": QUERY})
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    payload = resp.json()
    # A hit short-circuits retrieval — but the echo still reports cache_hit=true.
    assert retrieve.calls == []
    assert payload["answer"] == "cached answer"
    assert payload["applied"]["cache_hit"] is True
    assert uuid.UUID(payload["query_id"])
