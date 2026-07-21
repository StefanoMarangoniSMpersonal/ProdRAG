"""Q10 `/ask` endpoint tests: the HTTP entry point to the read path.

The *immutable spec* (CLAUDE.md "test-first & test-immutable"): written and watched fail
before `app/api/ask.py` existed. `/ask` is a thin, REQUEST-shaped shell over the
finished `retrieve()` + `generate()` stages — the deliberate contrast with job-shaped
ingestion (`POST /documents` = 202 + poll) — so these tests pin the *wiring* and the
*logging contract*, not retrieval or generation quality (those have their own specs and
their own eval harnesses):

    POST /ask -> retrieve -> generate -> 200 {answer, citations, both id lists}
                                      -> one structured log line + one query_logs row

Two seams keep it offline and fast (the M7/upload-test pattern): the module-level
`retrieve` and `generate` names are monkeypatched, so no Gemini call, no torch, no
embedding — while the endpoint's OWN logic (validation, response shape, the log record,
the audit row) runs for real. The request's DB session is overridden to the committing
`session_factory` (the log row must actually land), so Docker must be up — these ERROR,
never SKIP, if it isn't ("a SKIP is a false green").

The per-query log is the point of the milestone, not a side effect: CLAUDE.md requires
every RAG query to record the user message, the retrieved chunk ids, the reranked order,
the final context, the answer and token usage — it is the raw material every later
debugging session and eval starts from. So it is specified here, twice over: as a
structured stdout line AND as a durable `query_logs` row.
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import ask as ask_mod
from app.db import get_session
from app.generate.generate import GenerationResult, TokenUsage
from app.main import app
from app.models import Chunk, QueryLog
from app.retrieve.types import RetrievalResult, ScoredChunk

QUERY = "Who is the CEO of Aurelia Robotics?"
ANSWER = "The CEO is Marta Silveira."
# The funnel, made observable: retrieval found 4 candidates, rerank kept 2 (reordered).
CANDIDATE_IDS = [11, 12, 13, 14]
FINAL_IDS = [13, 11]
CITATIONS = [13]


def _scored(chunk_id: int) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(id=chunk_id, content=f"content {chunk_id}"), score=1.0
    )


class _RecordingRetrieve:
    """Async fake `retrieve`: records its calls, returns a fixed funnel."""

    def __init__(self, final_ids: list[int] | None = None) -> None:
        self.calls: list[str] = []
        self._final_ids = FINAL_IDS if final_ids is None else final_ids

    async def __call__(self, query: str, **kwargs) -> RetrievalResult:
        self.calls.append(query)
        return RetrievalResult(
            query=query,
            chunks=[_scored(i) for i in self._final_ids],
            timings_ms={"embed_ms": 1.0, "search_ms": 2.0, "total_ms": 3.0},
            candidate_chunk_ids=CANDIDATE_IDS,
        )


class _RecordingGenerate:
    """Async fake `generate`: records (query, chunk ids) and returns a canned answer."""

    def __init__(
        self, answer: str = ANSWER, citations: list[int] | None = None
    ) -> None:
        self.calls: list[tuple[str, list[int]]] = []
        self._answer = answer
        self._citations = CITATIONS if citations is None else citations

    async def __call__(self, query: str, chunks: list[ScoredChunk]) -> GenerationResult:
        self.calls.append((query, [sc.chunk.id for sc in chunks]))
        return GenerationResult(
            answer=self._answer,
            citations=list(self._citations),
            usage=TokenUsage(prompt_tokens=120, completion_tokens=18, total_tokens=138),
            timings_ms={"generate_ms": 4.0},
        )


@pytest.fixture
async def wired(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    """Swap the pipeline for recorders, point the request session at the container."""
    retriever = _RecordingRetrieve()
    generator = _RecordingGenerate()
    monkeypatch.setattr(ask_mod, "retrieve", retriever)
    monkeypatch.setattr(ask_mod, "generate", generator)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, retriever, generator, session_factory
    app.dependency_overrides.pop(get_session, None)


async def _log_rows(
    factory: async_sessionmaker[AsyncSession], query: str
) -> list[QueryLog]:
    async with factory() as session:
        rows = (
            await session.execute(select(QueryLog).where(QueryLog.query == query))
        ).scalars()
        return list(rows)


async def test_ask_answers_and_reports_the_whole_funnel(wired) -> None:
    client, retriever, generator, _ = wired

    resp = await client.post("/ask", json={"query": QUERY})

    # Request-shaped: the answer is IN the response, not behind a poll.
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["answer"] == ANSWER
    assert payload["citations"] == CITATIONS
    # Both rankings are reported, and they genuinely differ — the retrieve-wide ->
    # rerank-narrow funnel is visible to the caller, not collapsed into one list.
    assert payload["retrieved_chunk_ids"] == CANDIDATE_IDS
    assert payload["reranked_chunk_ids"] == FINAL_IDS
    assert uuid.UUID(payload["query_id"])  # a real correlation id
    assert "total_ms" in payload["timings_ms"]

    # The pipeline was really driven: the user's question reached retrieval, and the
    # FINAL (post-rerank) chunks — not the wide pool — were what generation saw.
    assert retriever.calls == [QUERY]
    assert generator.calls == [(QUERY, FINAL_IDS)]


async def test_ask_writes_the_query_log_row(wired) -> None:
    client, _, _, factory = wired

    resp = await client.post("/ask", json={"query": QUERY})
    query_id = resp.json()["query_id"]

    (row,) = await _log_rows(factory, QUERY)
    # The CLAUDE.md logging contract, durably: message, both rankings, answer, tokens.
    assert str(row.id) == query_id
    assert row.answer == ANSWER
    assert row.citations == CITATIONS
    assert row.retrieved_chunk_ids == CANDIDATE_IDS
    assert row.final_chunk_ids == FINAL_IDS
    assert row.prompt_tokens == 120
    assert row.completion_tokens == 18
    assert row.total_tokens == 138
    # The context is identified by chunk id (joinable back to `chunks`) plus its size,
    # so a later reader can reconstruct exactly what the model was shown.
    assert row.context_chars == sum(len(f"content {i}") for i in FINAL_IDS)
    assert "total_ms" in row.timings_ms


async def test_ask_emits_a_structured_log_line(
    wired, caplog: pytest.LogCaptureFixture
) -> None:
    client, _, _, _ = wired
    caplog.set_level(logging.INFO, logger="app.api.ask")

    await client.post("/ask", json={"query": QUERY})

    (record,) = [r for r in caplog.records if r.name == "app.api.ask"]
    line = record.getMessage()
    # One greppable line carrying the whole query story — the live tail that makes a
    # production "what did it retrieve?" answerable without a DB round trip.
    assert QUERY in line
    assert ANSWER in line
    assert "11" in line and "13" in line  # both rankings' ids
    assert "138" in line  # token usage


async def test_ask_rejects_a_blank_query_before_spending_anything(wired) -> None:
    client, retriever, generator, factory = wired

    resp = await client.post("/ask", json={"query": "   "})

    # A whitespace-only question is a caller error we can know synchronously — reject it
    # before retrieval embeds anything or generation spends a billable call.
    assert resp.status_code == 400
    assert retriever.calls == []
    assert generator.calls == []
    async with factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(QueryLog))
        ).scalar_one()
    assert count == 0


async def test_ask_logs_a_refusal_too(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing retrieved -> generation refuses locally. That is exactly the query you
    # most want on record (raw material for the refusal/faithfulness evals), so the
    # row must still be written and the refusal returned as a normal 200 — an honest
    # "I don't know" is a successful answer, not an error.
    retriever = _RecordingRetrieve(final_ids=[])
    generator = _RecordingGenerate(answer="I don't know.", citations=[])
    monkeypatch.setattr(ask_mod, "retrieve", retriever)
    monkeypatch.setattr(ask_mod, "generate", generator)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/ask", json={"query": QUERY})
    app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    assert resp.json()["citations"] == []
    assert resp.json()["reranked_chunk_ids"] == []
    (row,) = await _log_rows(session_factory, QUERY)
    assert row.answer == "I don't know."


async def test_ask_still_answers_when_the_log_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The audit row is best-effort: the answer is the product. A failing log write must
    # never turn a good, already-paid-for answer into a 500 for the user.
    monkeypatch.setattr(ask_mod, "retrieve", _RecordingRetrieve())
    monkeypatch.setattr(ask_mod, "generate", _RecordingGenerate())

    class _BrokenSession:
        def add(self, obj):  # the endpoint's first touch of the session
            raise RuntimeError("database is on fire")

        async def commit(self):
            raise RuntimeError("database is on fire")

    async def _override_get_session():
        yield _BrokenSession()

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/ask", json={"query": QUERY})
    app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    assert resp.json()["answer"] == ANSWER
