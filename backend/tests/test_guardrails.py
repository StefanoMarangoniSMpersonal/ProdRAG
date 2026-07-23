"""P2 — guardrails: the validation layer around the read path (input + output).

The *immutable spec* (CLAUDE.md "test-first & test-immutable"): written and watched fail
before `app/guards.py` and the P2 wiring in `app/api/ask.py` existed. Two guards, one
module:

  - INPUT guard (`validate_query`): a blank or over-long question is a caller error
    we can know synchronously — reject it with 400 *before* retrieval embeds anything
    or generation spends a billable call. This formalizes (and extends, with a length
    cap) the inline blank-query check Q10 already had.
  - OUTPUT guard (`check_citations`): the model is shown ONLY the final reranked
    chunks, so a citation that isn't one of those ids is a phantom — fabricated, or
    pulled from the wider candidate pool the model never saw. The guard partitions
    citations into valid vs phantom so `ask.py` can repair-and-flag (drop the phantom
    from the client response, still return the answer, log the violation).

The pure functions are unit-tested with no DB. The endpoint tests reuse the
`test_ask.py` seams (monkeypatched module-level `retrieve`/`generate`, committing
`session_factory` override), so Docker must be up — they ERROR, never SKIP, if not.

The frozen audit contract is re-asserted, not weakened: the phantom case still writes a
`query_logs` row carrying the RAW model citations, so nothing the model did is hidden.
"""

from __future__ import annotations

import contextlib
import logging

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import ask as ask_mod
from app.config import get_settings
from app.db import get_session
from app.generate.generate import GenerationResult, TokenUsage
from app.guards import (
    CitationCheck,
    EmptyQueryError,
    QueryTooLongError,
    check_citations,
    validate_query,
)
from app.main import app
from app.models import Chunk, QueryLog
from app.retrieve.types import RetrievalResult, ScoredChunk

QUERY = "Who is the CEO of Aurelia Robotics?"
ANSWER = "The CEO is Marta Silveira."
CANDIDATE_IDS = [11, 12, 13, 14]
FINAL_IDS = [13, 11]


# --------------------------------------------------------------------------------------
# Unit: check_citations (pure, no DB) — the output guard's core partition.
# --------------------------------------------------------------------------------------
def test_check_citations_all_valid_has_no_phantom() -> None:
    check = check_citations([13, 11], [13, 11])
    assert isinstance(check, CitationCheck)
    assert check.valid_citations == [13, 11]
    assert check.phantom_citations == []
    assert check.has_violation is False


def test_check_citations_detects_a_phantom() -> None:
    # 999 was never among the chunks the model was shown -> phantom.
    check = check_citations([13, 999], [13, 11])
    assert check.valid_citations == [13]
    assert check.phantom_citations == [999]
    assert check.has_violation is True


def test_check_citations_empty_passes_trivially() -> None:
    # The refusal path: no citations to validate, so no violation.
    check = check_citations([], [13, 11])
    assert check.valid_citations == []
    assert check.phantom_citations == []
    assert check.has_violation is False


def test_check_citations_preserves_order_and_duplicates() -> None:
    # Order-preserving partition, duplicates kept as-is (the guard reports, not dedups).
    check = check_citations([11, 999, 13, 999, 11], [11, 13])
    assert check.valid_citations == [11, 13, 11]
    assert check.phantom_citations == [999, 999]


# --------------------------------------------------------------------------------------
# Unit: validate_query (pure, no DB) — the input guard.
# --------------------------------------------------------------------------------------
def test_validate_query_strips_and_returns() -> None:
    assert validate_query("  hello  ", max_chars=100) == "hello"


def test_validate_query_blank_raises() -> None:
    with pytest.raises(EmptyQueryError):
        validate_query("", max_chars=100)


def test_validate_query_whitespace_only_raises() -> None:
    with pytest.raises(EmptyQueryError):
        validate_query("   ", max_chars=100)


def test_validate_query_over_cap_raises() -> None:
    with pytest.raises(QueryTooLongError):
        validate_query("a" * 101, max_chars=100)


def test_validate_query_at_the_boundary_passes() -> None:
    # len == max_chars is allowed; only strictly-longer is rejected.
    assert validate_query("a" * 100, max_chars=100) == "a" * 100


# --------------------------------------------------------------------------------------
# Endpoint: the guards wired into POST /ask (mirrors test_ask.py's seams).
# --------------------------------------------------------------------------------------
def _scored(chunk_id: int) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(id=chunk_id, content=f"content {chunk_id}"), score=1.0
    )


class _Retrieve:
    """Async fake `retrieve`: records its calls, returns a fixed funnel."""

    def __init__(self, final_ids: list[int]) -> None:
        self.calls: list[str] = []
        self._final_ids = final_ids

    async def __call__(self, query: str, **kwargs) -> RetrievalResult:
        self.calls.append(query)
        return RetrievalResult(
            query=query,
            chunks=[_scored(i) for i in self._final_ids],
            timings_ms={"embed_ms": 1.0, "search_ms": 2.0, "total_ms": 3.0},
            candidate_chunk_ids=CANDIDATE_IDS,
        )


class _Generate:
    """Async fake `generate`: returns whatever citations the test asks for."""

    def __init__(self, citations: list[int], answer: str = ANSWER) -> None:
        self.calls: list[tuple[str, list[int]]] = []
        self._citations = citations
        self._answer = answer

    async def __call__(self, query: str, chunks: list[ScoredChunk]) -> GenerationResult:
        self.calls.append((query, [sc.chunk.id for sc in chunks]))
        return GenerationResult(
            answer=self._answer,
            citations=list(self._citations),
            usage=TokenUsage(prompt_tokens=120, completion_tokens=18, total_tokens=138),
            timings_ms={"generate_ms": 4.0},
        )


@contextlib.asynccontextmanager
async def _driver(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_ids: list[int],
    citations: list[int],
    answer: str = ANSWER,
):
    """Swap the pipeline for recorders, point the request session at the container."""
    retriever = _Retrieve(final_ids)
    generator = _Generate(citations, answer)
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
            yield client, retriever, generator
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


async def test_ask_repairs_and_flags_a_phantom_citation(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.api.ask")
    # The model cites 999, which is NOT among the chunks it was shown ([13, 11]).
    async with _driver(
        session_factory, monkeypatch, final_ids=[13, 11], citations=[13, 999]
    ) as (client, _, _):
        resp = await client.post("/ask", json={"query": QUERY})

    # The answer is still returned (200 — never cost the user an answer already billed),
    # but the phantom is dropped from the client-facing citations.
    assert resp.status_code == 200
    assert resp.json()["answer"] == ANSWER
    assert resp.json()["citations"] == [13]

    # The violation is flagged on a greppable WARNING line naming the phantom id.
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.api.ask" and r.levelno == logging.WARNING
    ]
    assert any("citation_violation" in r.getMessage() for r in warnings)
    assert any("999" in r.getMessage() for r in warnings)

    # Audit contract intact: the durable row still carries the RAW model citations,
    # so the phantom is on record (the eval harness can count violations later) —
    # repair is only client-facing.
    (row,) = await _log_rows(session_factory, QUERY)
    assert row.citations == [13, 999]


async def test_ask_passes_a_fully_valid_answer_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.api.ask")
    async with _driver(
        session_factory, monkeypatch, final_ids=[13, 11], citations=[13]
    ) as (client, _, _):
        resp = await client.post("/ask", json={"query": QUERY})

    assert resp.status_code == 200
    assert resp.json()["citations"] == [13]
    # No violation -> no WARNING, and the repair is a no-op.
    assert not [r for r in caplog.records if "citation_violation" in r.getMessage()]


async def test_ask_rejects_an_overlong_query_before_spending(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_query = "a" * (get_settings().max_query_chars + 1)
    async with _driver(
        session_factory, monkeypatch, final_ids=[13, 11], citations=[13]
    ) as (client, retriever, generator):
        resp = await client.post("/ask", json={"query": long_query})

    # Rejected pre-spend: no embed, no billable generation, no audit row.
    assert resp.status_code == 400
    assert retriever.calls == []
    assert generator.calls == []
    async with session_factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(QueryLog))
        ).scalar_one()
    assert count == 0


async def test_ask_rejects_a_blank_query_via_the_guard(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _driver(
        session_factory, monkeypatch, final_ids=[13, 11], citations=[13]
    ) as (client, retriever, generator):
        resp = await client.post("/ask", json={"query": "   "})

    assert resp.status_code == 400
    assert retriever.calls == []
    assert generator.calls == []
