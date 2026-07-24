"""Spec: /ask records `lexical_matched` in both audit sinks (row + log line).

New spec beside the immutable `test_ask.py` (which stays untouched — the flag is an
additive field). Pins that the boolean retrieve() computed reaches BOTH halves of the
per-query audit: the durable `query_logs` row and the greppable stdout line. Same offline
seams as `test_ask.py`: module-level `retrieve`/`generate` are faked, and the request
session is the committing `session_factory` (the row must actually land), so Docker must
be up — these ERROR, never SKIP.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import ask as ask_mod
from app.db import get_session
from app.generate.generate import GenerationResult, TokenUsage
from app.main import app
from app.models import Chunk, QueryLog
from app.retrieve.types import RetrievalResult, ScoredChunk

QUERY = "Who chairs the audit committee?"


class _RetrieveWithLexicalFlag:
    """Async fake `retrieve` that returns a fixed funnel with a chosen lexical flag."""

    def __init__(self, *, lexical_matched: bool) -> None:
        self._lexical_matched = lexical_matched

    async def __call__(self, query: str, **kwargs) -> RetrievalResult:
        return RetrievalResult(
            query=query,
            chunks=[ScoredChunk(chunk=Chunk(id=7, content="ctx"), score=1.0)],
            timings_ms={"embed_ms": 1.0, "search_ms": 2.0, "total_ms": 3.0},
            candidate_chunk_ids=[7],
            lexical_matched=self._lexical_matched,
        )


class _RecordingGenerate:
    async def __call__(self, query: str, chunks: list[ScoredChunk]) -> GenerationResult:
        return GenerationResult(
            answer="Some answer.",
            citations=[7],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            timings_ms={"generate_ms": 4.0},
        )


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    lexical_matched: bool,
) -> None:
    monkeypatch.setattr(
        ask_mod, "retrieve", _RetrieveWithLexicalFlag(lexical_matched=lexical_matched)
    )
    monkeypatch.setattr(ask_mod, "generate", _RecordingGenerate())

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/ask", json={"query": QUERY})
    app.dependency_overrides.pop(get_session, None)


async def _row(factory: async_sessionmaker[AsyncSession]) -> QueryLog:
    async with factory() as session:
        rows = (
            await session.execute(select(QueryLog).where(QueryLog.query == QUERY))
        ).scalars()
        (row,) = list(rows)
        return row


async def test_ask_records_lexical_matched_true(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.api.ask")
    await _run(session_factory, monkeypatch, lexical_matched=True)

    # Durable row carries the flag...
    assert (await _row(session_factory)).lexical_matched is True
    # ...and so does the live line.
    (record,) = [r for r in caplog.records if r.name == "app.api.ask"]
    assert '"lexical_matched": true' in record.getMessage()


async def test_ask_records_lexical_matched_false(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.api.ask")
    await _run(session_factory, monkeypatch, lexical_matched=False)

    assert (await _row(session_factory)).lexical_matched is False
    (record,) = [r for r in caplog.records if r.name == "app.api.ask"]
    assert '"lexical_matched": false' in record.getMessage()
