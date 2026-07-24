"""P6 (backend rider) — `POST /ask` returns the source passages, not just chunk ids.

Test-first, and deliberately SEPARATE from the immutable `test_ask.py` / `test_ask_cache.py`
(those pin the frozen id-only contract, which this must leave intact). What this pins is
the ADDITIVE `sources` field the chat UI needs: bare `citations: [13, 11]` badges are
useless with no passage behind them, and the shown chunks (id + content) are already in
memory in `ask.py` at response time — returning them is free.

The contract:
  - `sources` is one entry per SHOWN chunk (the post-rerank `result.chunks` the model
    actually saw), each `{id, content, score}`, in shown order — a superset of `citations`
    so the UI can render every passage and highlight the cited ones.
  - The refusal path (nothing retrieved) returns `sources: []`, mirroring the empty
    `reranked_chunk_ids` — an honest "I don't know" cites nothing and shows nothing.
  - On a CACHE HIT (P3, cache-on) the stored sources come back too, so a hit reproduces
    the same response a fresh miss would have — the cache is replay, not a downgrade.

`sources` is RESPONSE-ONLY: the frozen per-query audit contract (`ask.query` line +
`query_logs` row) is untouched, so there is no migration and the audit tests stay green.

Seams mirror `test_ask.py` / `test_ask_cache.py`: module-level `retrieve` / `generate`
(and, for the hit test, `embed_query` + `cache_get` / `cache_set`) are monkeypatched, and
the request session is overridden to the committing `session_factory` (Docker must be up —
these ERROR, never SKIP).
"""

from __future__ import annotations

import contextlib
import uuid

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cache as cache_mod
from app.api import ask as ask_mod
from app.config import get_settings
from app.db import get_session
from app.generate.generate import GenerationResult, TokenUsage
from app.main import app
from app.models import Chunk
from app.retrieve.types import RetrievalResult, ScoredChunk

QUERY = "Who is the CEO of Aurelia Robotics?"
ANSWER = "The CEO is Marta Silveira."
CANDIDATE_IDS = [11, 12, 13, 14]
FINAL_IDS = [13, 11]
CITATIONS = [13]
QVEC = [0.6, 0.8, 0.0]  # unit-length fake query vector for the cache-on test


def _scored(chunk_id: int) -> ScoredChunk:
    # score varies with id so the test proves the field is per-chunk, not a constant.
    return ScoredChunk(
        chunk=Chunk(id=chunk_id, content=f"content {chunk_id}"),
        score=0.5 + chunk_id / 100,
    )


class _Retrieve:
    """Async fake `retrieve`: returns a fixed post-rerank funnel."""

    def __init__(self, final_ids: list[int]) -> None:
        self._final_ids = final_ids

    async def __call__(self, query: str, **kwargs: object) -> RetrievalResult:
        return RetrievalResult(
            query=query,
            chunks=[_scored(i) for i in self._final_ids],
            timings_ms={"embed_ms": 1.0, "search_ms": 2.0, "total_ms": 3.0},
            candidate_chunk_ids=CANDIDATE_IDS,
        )


class _Generate:
    """Async fake `generate`: returns a canned cited answer."""

    def __init__(self, answer: str, citations: list[int]) -> None:
        self._answer = answer
        self._citations = citations

    async def __call__(self, query: str, chunks: list[ScoredChunk]) -> GenerationResult:
        return GenerationResult(
            answer=self._answer,
            citations=list(self._citations),
            usage=TokenUsage(prompt_tokens=120, completion_tokens=18, total_tokens=138),
            timings_ms={"generate_ms": 4.0},
        )


@contextlib.asynccontextmanager
async def _client(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    retriever: _Retrieve,
    generator: _Generate,
):
    monkeypatch.setattr(ask_mod, "retrieve", retriever)
    monkeypatch.setattr(ask_mod, "generate", generator)

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


async def test_ask_returns_a_source_per_shown_chunk(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(
        session_factory,
        monkeypatch,
        retriever=_Retrieve(FINAL_IDS),
        generator=_Generate(ANSWER, CITATIONS),
    ) as client:
        resp = await client.post("/ask", json={"query": QUERY})

    assert resp.status_code == 200
    payload = resp.json()

    # One source per SHOWN (post-rerank) chunk, in shown order — the same funnel the
    # model saw, so `citations` (a subset) can be highlighted against the full set.
    sources = payload["sources"]
    assert [s["id"] for s in sources] == FINAL_IDS
    assert [s["content"] for s in sources] == [f"content {i}" for i in FINAL_IDS]
    # The score rides along and is genuinely per-chunk (not a constant).
    assert [s["score"] for s in sources] == [0.5 + i / 100 for i in FINAL_IDS]

    # Additive only: the id-only contract is untouched.
    assert payload["citations"] == CITATIONS
    assert payload["reranked_chunk_ids"] == FINAL_IDS


async def test_ask_refusal_returns_no_sources(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing retrieved -> nothing shown -> the response cites and shows nothing.
    async with _client(
        session_factory,
        monkeypatch,
        retriever=_Retrieve([]),
        generator=_Generate("I don't know.", []),
    ) as client:
        resp = await client.post("/ask", json={"query": QUERY})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["sources"] == []
    assert payload["reranked_chunk_ids"] == []


async def test_cache_hit_replays_the_stored_sources(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hit must reproduce the same response a fresh miss would — sources included. The
    # stored sources are plain dicts (JSON-safe through asdict); the endpoint rebuilds
    # them into the response `sources`.
    stored_sources = [
        {"id": i, "content": f"content {i}", "score": 0.5 + i / 100} for i in FINAL_IDS
    ]
    cached = cache_mod.CachedAnswer(
        answer="Marta Silveira leads Aurelia Robotics.",
        citations=[13],
        valid_citations=[13],
        phantom_citations=[],
        retrieved_chunk_ids=CANDIDATE_IDS,
        final_chunk_ids=FINAL_IDS,
        context_chars=20,
        generation_model="gemini-3.1-flash-lite",
        sources=stored_sources,
    )

    async def _embed_query(query: str) -> list[float]:
        return list(QVEC)

    async def _cache_get(vec: list[float], owner: uuid.UUID) -> cache_mod.CachedAnswer:
        return cached

    patched = get_settings().model_copy(update={"cache_enabled": True})
    monkeypatch.setattr(ask_mod, "get_settings", lambda: patched)
    monkeypatch.setattr(ask_mod, "embed_query", _embed_query)
    monkeypatch.setattr(cache_mod, "cache_get", _cache_get)
    monkeypatch.setattr(ask_mod, "retrieve", _Retrieve(FINAL_IDS))
    monkeypatch.setattr(ask_mod, "generate", _Generate(ANSWER, CITATIONS))

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
    sources = resp.json()["sources"]
    assert [s["id"] for s in sources] == FINAL_IDS
    assert [s["content"] for s in sources] == [f"content {i}" for i in FINAL_IDS]
