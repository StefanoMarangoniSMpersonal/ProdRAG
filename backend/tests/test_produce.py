"""Spec for Q9 phase A: run the real pipeline once and CACHE the answers to disk.

WHY the eval is split into two phases at all. Judging with RAGAS costs ~240 Gemini calls
per full run against a 500/day quota, so the expensive part must never be re-paid for a
reason unrelated to it. Phase A (here) runs `retrieve()` + `generate()` and writes an
answers file; phase B (`eval/ragas_eval.py`) reads that file and scores it. Re-scoring
with different metrics, or after a crash in the judge, then costs zero generation calls
— and a bug in the judge can't burn the day's budget on regenerating answers.

The answers file is also the honest audit trail: it records the exact contexts the model
saw, so a faithfulness score can always be traced back to the passages it was computed
against, months later, without re-running anything.

Offline by construction: `retrieve` and `generate` are module globals in `eval.produce`,
so these tests monkeypatch *that module's* copies with fakes — the same seam pattern
`eval/run.py`, `app/retrieve/retrieve.py` and `app/ingest/orchestrate.py` use. No
Postgres, no Gemini, no key.
"""

from __future__ import annotations

import json

import pytest
from eval import produce
from eval.refusal import Trap
from eval.run import GoldenItem

from app.generate.generate import GeneratedAnswer
from app.models import Chunk
from app.retrieve.types import RetrievalResult, ScoredChunk


def _scored(chunk_id: int, content: str, score: float = 1.0) -> ScoredChunk:
    return ScoredChunk(chunk=Chunk(id=chunk_id, content=content), score=score)


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace retrieve/generate with fakes; record what they were called with."""
    calls: dict = {"retrieve": [], "generate": []}

    async def fake_retrieve(query: str, **kwargs) -> RetrievalResult:
        calls["retrieve"].append((query, kwargs))
        return RetrievalResult(
            query=query,
            chunks=[_scored(7, "seven text", 0.9), _scored(3, "three text", 0.4)],
        )

    async def fake_generate(query: str, chunks: list[ScoredChunk]) -> GeneratedAnswer:
        calls["generate"].append((query, [sc.chunk.id for sc in chunks]))
        return GeneratedAnswer(answer=f"answer to {query}", citations=[7])

    monkeypatch.setattr(produce, "retrieve", fake_retrieve)
    monkeypatch.setattr(produce, "generate", fake_generate)
    return calls


async def test_produces_one_row_per_item_with_the_judgeable_fields(
    fake_pipeline: dict,
) -> None:
    """A row carries all phase B needs — and nothing it would have to re-fetch."""
    items = [
        GoldenItem(id=1, question="who?", relevant_chunk_ids={7}, expected_answer="Ada")
    ]

    rows = await produce.produce_answers(items)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == 1
    assert row["question"] == "who?"
    # The reference answer is what LLM context recall is scored against; it must survive
    # the trip from golden.jsonl to the answers file untouched.
    assert row["reference"] == "Ada"
    assert row["answer"] == "answer to who?"
    assert row["citations"] == [7]
    # Contexts are the chunk TEXTS the model actually saw, in retrieval order — the
    # ids alone would be useless to a judge that has to read the passages.
    assert row["contexts"] == ["seven text", "three text"]
    assert row["retrieved_chunk_ids"] == [7, 3]


async def test_generate_is_fed_exactly_what_retrieve_returned(
    fake_pipeline: dict,
) -> None:
    """Phase A must run the REAL pipeline wiring, not a reconstruction of it."""
    await produce.produce_answers(
        [GoldenItem(id=1, question="q1", relevant_chunk_ids=set())]
    )

    assert fake_pipeline["generate"] == [("q1", [7, 3])]


async def test_traps_are_marked_and_carry_no_reference(fake_pipeline: dict) -> None:
    """Traps get a different judgment in phase B, so the row must say which kind it is.

    A trap has no correct answer in the corpus, so it has no reference to recall and
    (crucially) a correct refusal would score 0 on answer relevancy. Phase B needs to
    route on this, so phase A records it rather than making B guess from a blank field.
    """
    rows = await produce.produce_answers(
        [Trap(id="T1", question="who is the CTO?")], kind="trap"
    )

    assert rows[0]["kind"] == "trap"
    assert rows[0]["id"] == "T1"
    assert rows[0]["reference"] == ""


async def test_golden_rows_are_marked_golden(fake_pipeline: dict) -> None:
    rows = await produce.produce_answers(
        [GoldenItem(id=1, question="q", relevant_chunk_ids=set())]
    )
    assert rows[0]["kind"] == "golden"


async def test_limit_truncates_the_run(fake_pipeline: dict) -> None:
    """`--limit` is the cheap smoke path: validate wiring on 3 questions, not 48."""
    items = [
        GoldenItem(id=i, question=f"q{i}", relevant_chunk_ids=set()) for i in range(5)
    ]

    rows = await produce.produce_answers(items, limit=2)

    assert [r["id"] for r in rows] == [0, 1]
    assert len(fake_pipeline["retrieve"]) == 2


async def test_owner_id_is_forwarded_to_retrieve(fake_pipeline: dict) -> None:
    """The visibility seam must reach the query, or the eval reads the wrong corpus."""
    import uuid

    owner = uuid.uuid4()
    await produce.produce_answers(
        [GoldenItem(id=1, question="q", relevant_chunk_ids=set())], owner_id=owner
    )

    assert fake_pipeline["retrieve"][0][1]["owner_id"] == owner


def test_write_and_load_answers_roundtrip(tmp_path) -> None:
    """The answers file is JSONL, and reading it back yields the same rows.

    JSONL (one JSON object per line) so a long run can be appended to incrementally and
    a partial file after a crash is still parseable — the same reason golden.jsonl is.
    """
    rows = [
        {"id": 1, "question": "q", "reference": "r", "contexts": ["c"], "answer": "a"},
        {"id": 2, "question": "q2", "reference": "", "contexts": [], "answer": "a2"},
    ]

    path = produce.write_answers(rows, tmp_path)

    assert path.parent == tmp_path
    assert path.name.startswith("answers-")
    assert path.suffix == ".jsonl"
    # One line per row, so the file streams and survives truncation.
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert produce.load_answers(path) == rows


def test_load_answers_skips_blank_lines(tmp_path) -> None:
    path = tmp_path / "answers-x.jsonl"
    path.write_text(
        json.dumps({"id": 1}) + "\n\n" + json.dumps({"id": 2}) + "\n",
        encoding="utf-8",
    )

    assert produce.load_answers(path) == [{"id": 1}, {"id": 2}]
