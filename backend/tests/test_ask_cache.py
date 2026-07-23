"""P3 — the semantic cache wired into POST /ask (endpoint integration spec).

Test-first, and deliberately SEPARATE from the immutable `test_ask.py`: that file pins
the cache-OFF contract (which P3 must leave byte-identical), so the cache-ON behavior is
specified here instead. What this pins:

  - HIT: the pipeline is skipped entirely — `retrieve` and `generate` are NEVER called —
    the stored answer is returned, and the frozen audit contract still fires with
    `cache_hit=true` and NULL token counts (a hit spent no generation).
  - MISS: the pipeline runs, the query is embedded exactly ONCE (the shared vector is
    handed to `retrieve` via `query_embedding=`, not re-embedded), and the post-guard
    answer is written back to the cache.
  - A below-threshold near-match reaches the endpoint as a `cache_get -> None`, i.e. the
    same miss path — exercised here through the REAL cache functions over a fake Redis,
    so the threshold decision is proven end to end (its arithmetic is unit-tested in
    `test_cache.py`).

Seams (the `test_ask.py` / `test_guardrails.py` pattern): the module-level `retrieve`,
`generate`, `embed_query` names and the `cache` module's `cache_get` / `cache_set` are
monkeypatched; the request session is overridden to the committing `session_factory`, so
the audit row really lands (Docker must be up — these ERROR, never SKIP). Cache-on is
forced by patching `ask.get_settings` to a copy with `cache_enabled=True`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import uuid

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cache as cache_mod
from app.api import ask as ask_mod
from app.config import get_settings
from app.db import get_session
from app.generate.generate import GenerationResult, TokenUsage
from app.main import app
from app.models import DEV_OWNER_ID, Chunk, QueryLog
from app.retrieve.types import RetrievalResult, ScoredChunk

QUERY = "Who is the CEO of Aurelia Robotics?"
ANSWER = "The CEO is Marta Silveira."
CACHED_ANSWER = "Marta Silveira leads Aurelia Robotics."
CANDIDATE_IDS = [11, 12, 13, 14]
FINAL_IDS = [13, 11]
QVEC = [0.6, 0.8, 0.0]  # the fixed vector our fake embed_query returns (unit length)


def _scored(chunk_id: int) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(id=chunk_id, content=f"content {chunk_id}"), score=1.0
    )


class _Retrieve:
    """Async fake `retrieve`: records the query AND the query_embedding handed to it."""

    def __init__(self, final_ids: list[int]) -> None:
        self.calls: list[str] = []
        self.embeddings: list[list[float] | None] = []
        self._final_ids = final_ids

    async def __call__(self, query: str, **kwargs: object) -> RetrievalResult:
        self.calls.append(query)
        self.embeddings.append(kwargs.get("query_embedding"))  # type: ignore[arg-type]
        return RetrievalResult(
            query=query,
            chunks=[_scored(i) for i in self._final_ids],
            timings_ms={"embed_ms": 0.0, "search_ms": 2.0, "total_ms": 3.0},
            candidate_chunk_ids=CANDIDATE_IDS,
        )


class _Generate:
    """Async fake `generate`: records calls, returns a canned cited answer."""

    def __init__(self, citations: list[int]) -> None:
        self.calls: list[tuple[str, list[int]]] = []
        self._citations = citations

    async def __call__(self, query: str, chunks: list[ScoredChunk]) -> GenerationResult:
        self.calls.append((query, [sc.chunk.id for sc in chunks]))
        return GenerationResult(
            answer=ANSWER,
            citations=list(self._citations),
            usage=TokenUsage(prompt_tokens=120, completion_tokens=18, total_tokens=138),
            timings_ms={"generate_ms": 4.0},
        )


class _CacheGet:
    """Async fake `cache_get`: records (vector, owner) and returns a fixed verdict."""

    def __init__(self, result: cache_mod.CachedAnswer | None) -> None:
        self.calls: list[tuple[list[float], uuid.UUID]] = []
        self._result = result

    async def __call__(
        self, query_embedding: list[float], owner_id: uuid.UUID
    ) -> cache_mod.CachedAnswer | None:
        self.calls.append((list(query_embedding), owner_id))
        return self._result


class _CacheSet:
    """Async fake `cache_set`: records everything written back to the cache."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[float], uuid.UUID, cache_mod.CachedAnswer]] = []

    async def __call__(
        self,
        query_embedding: list[float],
        owner_id: uuid.UUID,
        payload: cache_mod.CachedAnswer,
    ) -> None:
        self.calls.append((list(query_embedding), owner_id, payload))


async def _embed_query(query: str) -> list[float]:
    return list(QVEC)


class _FakeRedis:
    """Dict-backed async stand-in, for the real-cache below-threshold test."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


@contextlib.asynccontextmanager
async def _driver(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    retriever: _Retrieve,
    generator: _Generate,
    cache_get: object,
    cache_set: object,
):
    """Force cache-on, swap pipeline + cache for fakes, point the session at the DB."""
    patched = get_settings().model_copy(update={"cache_enabled": True})
    monkeypatch.setattr(ask_mod, "get_settings", lambda: patched)
    monkeypatch.setattr(ask_mod, "retrieve", retriever)
    monkeypatch.setattr(ask_mod, "generate", generator)
    monkeypatch.setattr(ask_mod, "embed_query", _embed_query)
    monkeypatch.setattr(cache_mod, "cache_get", cache_get)
    monkeypatch.setattr(cache_mod, "cache_set", cache_set)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _log_rows(
    factory: async_sessionmaker[AsyncSession], query: str
) -> list[QueryLog]:
    async with factory() as session:
        rows = (
            await session.execute(select(QueryLog).where(QueryLog.query == query))
        ).scalars()
        return list(rows)


def _ask_query_line(caplog: pytest.LogCaptureFixture) -> dict:
    (record,) = [
        r
        for r in caplog.records
        if r.name == "app.api.ask" and r.getMessage().startswith("ask.query ")
    ]
    return json.loads(record.getMessage().split("ask.query ", 1)[1])


async def test_cache_hit_returns_stored_answer_and_skips_the_pipeline(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.api.ask")
    cached = cache_mod.CachedAnswer(
        answer=CACHED_ANSWER,
        citations=[13],
        valid_citations=[13],
        phantom_citations=[],
        retrieved_chunk_ids=CANDIDATE_IDS,
        final_chunk_ids=FINAL_IDS,
        context_chars=20,
        generation_model="gemini-3.1-flash-lite",
    )
    retriever = _Retrieve(FINAL_IDS)
    generator = _Generate([13])
    cache_get = _CacheGet(cached)
    cache_set = _CacheSet()
    async with _driver(
        session_factory,
        monkeypatch,
        retriever=retriever,
        generator=generator,
        cache_get=cache_get,
        cache_set=cache_set,
    ) as client:
        resp = await client.post("/ask", json={"query": QUERY})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == CACHED_ANSWER
    assert body["citations"] == [13]  # the stored post-guard (repaired) list
    assert body["retrieved_chunk_ids"] == CANDIDATE_IDS
    assert body["reranked_chunk_ids"] == FINAL_IDS

    # The whole point of a hit: retrieval and generation never ran, and nothing was
    # written back (it's already cached).
    assert retriever.calls == []
    assert generator.calls == []
    assert cache_set.calls == []
    # The lookup used the embedded query vector.
    assert cache_get.calls and cache_get.calls[0][0] == QVEC

    # Audit contract intact on a hit: cache_hit true, tokens NULL (no generation spent).
    line = _ask_query_line(caplog)
    assert line["cache_hit"] is True
    assert line["answer"] == CACHED_ANSWER
    assert line["prompt_tokens"] is None
    assert line["total_tokens"] is None

    (row,) = await _log_rows(session_factory, QUERY)
    assert row.answer == CACHED_ANSWER
    assert row.total_tokens is None
    assert row.final_chunk_ids == FINAL_IDS
    assert row.timings_ms.get("cache_hit") is True


async def test_cache_miss_runs_pipeline_once_and_populates_the_cache(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.api.ask")
    retriever = _Retrieve(FINAL_IDS)
    generator = _Generate([13])
    cache_get = _CacheGet(None)  # a miss
    cache_set = _CacheSet()
    async with _driver(
        session_factory,
        monkeypatch,
        retriever=retriever,
        generator=generator,
        cache_get=cache_get,
        cache_set=cache_set,
    ) as client:
        resp = await client.post("/ask", json={"query": QUERY})

    assert resp.status_code == 200
    assert resp.json()["answer"] == ANSWER

    # The pipeline ran, and retrieval was handed the SHARED vector (embedded once), not
    # left to embed a second time.
    assert retriever.calls == [QUERY]
    assert retriever.embeddings == [QVEC]
    assert generator.calls == [(QUERY, FINAL_IDS)]

    # The post-guard answer was written back for next time.
    assert len(cache_set.calls) == 1
    qvec, owner_id, payload = cache_set.calls[0]
    assert qvec == QVEC
    assert owner_id == DEV_OWNER_ID
    assert isinstance(payload, cache_mod.CachedAnswer)
    assert payload.answer == ANSWER
    assert payload.final_chunk_ids == FINAL_IDS
    assert payload.retrieved_chunk_ids == CANDIDATE_IDS
    assert payload.valid_citations == [13]

    # The audit line marks it a miss so hit-rate is computable from the log.
    assert _ask_query_line(caplog)["cache_hit"] is False


async def test_below_threshold_near_match_misses_through_the_endpoint(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real cache functions over a fake Redis: seed a cached answer under a vector 0.9
    # cosine from the incoming query (QVEC). 0.9 < 0.95, so the endpoint must MISS and
    # run the pipeline rather than serve the near neighbor.
    fake = _FakeRedis()
    monkeypatch.setattr(cache_mod, "_get_redis", lambda: fake)
    patched = get_settings().model_copy(update={"cache_enabled": True})
    monkeypatch.setattr(ask_mod, "get_settings", lambda: patched)

    # A unit vector at cosine 0.9 from QVEC=[0.6,0.8,0]: rotate within the span.
    sin = math.sqrt(1.0 - 0.9 * 0.9)
    near = [0.9 * 0.6 - sin * 0.8, 0.9 * 0.8 + sin * 0.6, 0.0]
    seeded = cache_mod.CachedAnswer(
        answer="stale near answer",
        citations=[],
        valid_citations=[],
        phantom_citations=[],
        retrieved_chunk_ids=[],
        final_chunk_ids=[],
        context_chars=0,
        generation_model="m",
    )
    await cache_mod.cache_set(near, DEV_OWNER_ID, seeded)

    retriever = _Retrieve(FINAL_IDS)
    generator = _Generate([13])
    monkeypatch.setattr(ask_mod, "retrieve", retriever)
    monkeypatch.setattr(ask_mod, "generate", generator)
    monkeypatch.setattr(ask_mod, "embed_query", _embed_query)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post("/ask", json={"query": QUERY})
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    assert resp.json()["answer"] == ANSWER  # fresh pipeline answer, NOT the stale one
    assert retriever.calls == [QUERY]
