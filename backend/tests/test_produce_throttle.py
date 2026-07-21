"""Spec: phase A must be able to pace itself against the SAME quota as the judge.

The rate limiter was originally built for the judge, because that is where the call
volume is. But phase A is not free either: each question costs one generation call and
one query embedding, and a serial loop at ~2-3s per question issues ~20-30 generation
requests per minute — over the 15/min generation limit. Unthrottled, a 58-question
produce run can 429 partway through and leave a truncated answers file.

The limiter is OPTIONAL and defaults to off: the offline tests replace `retrieve`/
`generate` with fakes that make no network calls, and pacing those would only make the
suite slow for no benefit. Passing one in is how the real CLI run stays inside quota.

Phase A counts TWO requests per question (embed + generate) rather than one, because
both hit the API and both come out of a quota — even though they are metered against
different per-model pools. Counting the larger number is the safe direction to be wrong.
"""

from __future__ import annotations

import pytest
from eval import produce
from eval.run import GoldenItem
from eval.throttle import AsyncRateLimiter

from app.generate.generate import GeneratedAnswer
from app.models import Chunk
from app.retrieve.types import RetrievalResult, ScoredChunk


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_retrieve(query: str, **kwargs) -> RetrievalResult:
        return RetrievalResult(
            query=query,
            chunks=[ScoredChunk(chunk=Chunk(id=7, content="text"), score=1.0)],
        )

    async def fake_generate(query: str, chunks: list[ScoredChunk]) -> GeneratedAnswer:
        return GeneratedAnswer(answer="a", citations=[7])

    monkeypatch.setattr(produce, "retrieve", fake_retrieve)
    monkeypatch.setattr(produce, "generate", fake_generate)


async def test_a_limiter_paces_every_api_call_phase_a_makes(
    fake_pipeline: None,
) -> None:
    limiter = AsyncRateLimiter(0)  # unlimited: assert the COUNT, not the pacing
    items = [
        GoldenItem(id=i, question=f"q{i}", relevant_chunk_ids=set()) for i in range(3)
    ]

    await produce.produce_answers(items, limiter=limiter)

    # 3 questions x (1 query embedding + 1 generation).
    assert limiter.requests == 6


async def test_no_limiter_means_no_pacing(fake_pipeline: None) -> None:
    """The default must stay unthrottled so the offline suite doesn't sleep."""
    rows = await produce.produce_answers(
        [GoldenItem(id=1, question="q", relevant_chunk_ids=set())]
    )

    assert len(rows) == 1
