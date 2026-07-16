"""Q4 retrieval-eval spec: hit@k / MRR metrics + the harness that produces them.

The *immutable spec* for Q4 (CLAUDE.md "test-first & test-immutable"): written and
watched fail (red, `ModuleNotFoundError` — the `eval` package does not exist yet) before
`eval/metrics.py` and `eval/run.py` are written. The code bends to these asserts.

Two layers are pinned here:

  1. The metric functions in `eval/metrics.py` are PURE (no DB, no network, no clock):
     given a ranked list of chunk ids and the set of truly-relevant ids, they return a
     number. That purity is why they can be nailed down with hand-built rankings — the
     whole point of Q4 is a metric you can *trust*, so the metric itself is the most
     tightly specified thing in the milestone.

     - hit@k  = "was at least one relevant chunk in the top k" (a yes/no per query).
     - reciprocal rank = 1 / (rank of the FIRST relevant chunk), 0 if none — this is the
       per-query term that MRR (Mean Reciprocal Rank) averages.

  2. The harness (`eval/run.py`) drives real `retrieve()` per question and aggregates
     those metrics. Orchestration is tested OFFLINE: `retrieve` is monkeypatched to a
     fake returning canned rankings (no Postgres, no Gemini), so the harness's wiring —
     retrieve -> ranked ids -> metrics -> summary -> results file — is exercised without
     the world. The real end-to-end run against the dev DB is a separate `live` test.
"""

from __future__ import annotations

import json
import os

import pytest
from eval import run as run_mod
from eval.metrics import hit_at_k, hit_rate_at_k, mrr, reciprocal_rank
from eval.run import GoldenItem, evaluate_golden, load_golden, write_report

from app.models import DEV_OWNER_ID
from app.retrieve.types import RetrievalResult, ScoredChunk

# --- pure metrics: hit@k -------------------------------------------------------------


def test_hit_at_k_true_when_relevant_within_top_k() -> None:
    # Relevant chunk (7) sits at rank 3 (index 2). It's inside the top-3 and top-5...
    ranked = [1, 2, 7, 4, 5]
    relevant = {7}
    assert hit_at_k(ranked, relevant, k=3) is True
    assert hit_at_k(ranked, relevant, k=5) is True


def test_hit_at_k_false_when_relevant_below_k() -> None:
    # ...but NOT inside the top-1 or top-2 (rank 3 is below those cutoffs).
    ranked = [1, 2, 7, 4, 5]
    relevant = {7}
    assert hit_at_k(ranked, relevant, k=1) is False
    assert hit_at_k(ranked, relevant, k=2) is False


def test_hit_at_k_false_on_empty_relevant_or_ranked() -> None:
    # No relevant chunk exists -> can never hit (the trap-question shape; excluded from
    # the Q4 golden set for exactly this reason).
    assert hit_at_k([1, 2, 3], set(), k=3) is False
    # Retrieved nothing -> can never hit.
    assert hit_at_k([], {1}, k=3) is False


# --- pure metrics: reciprocal rank ---------------------------------------------------


def test_reciprocal_rank_is_inverse_of_first_relevant_rank() -> None:
    # First relevant at rank 1 -> 1.0; rank 2 -> 0.5; rank 4 -> 0.25.
    assert reciprocal_rank([9], {9}) == pytest.approx(1.0)
    assert reciprocal_rank([1, 9], {9}) == pytest.approx(0.5)
    assert reciprocal_rank([1, 2, 3, 9], {9}) == pytest.approx(0.25)


def test_reciprocal_rank_uses_the_first_relevant_when_several_match() -> None:
    # Two relevant chunks (at rank 2 and rank 4) -> only the FIRST (rank 2) counts.
    assert reciprocal_rank([1, 8, 3, 9], {8, 9}) == pytest.approx(0.5)


def test_reciprocal_rank_zero_when_no_relevant_retrieved() -> None:
    assert reciprocal_rank([1, 2, 3], {9}) == pytest.approx(0.0)
    assert reciprocal_rank([], {9}) == pytest.approx(0.0)


# --- pure metrics: aggregates --------------------------------------------------------


def test_mrr_averages_reciprocal_ranks() -> None:
    # (1.0 + 0.5 + 0.0) / 3
    assert mrr([1.0, 0.5, 0.0]) == pytest.approx(0.5)


def test_hit_rate_at_k_is_fraction_of_queries_that_hit() -> None:
    assert hit_rate_at_k([True, False, True, False]) == pytest.approx(0.5)


def test_aggregates_are_zero_not_error_on_empty() -> None:
    # A no-questions run must not divide by zero.
    assert mrr([]) == pytest.approx(0.0)
    assert hit_rate_at_k([]) == pytest.approx(0.0)


# --- golden set loading --------------------------------------------------------------


def test_load_golden_parses_jsonl(tmp_path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(
        '{"id": 1, "question": "Q", "relevant_chunk_ids": [10, 11], '
        '"expected_answer": "A"}\n',
        encoding="utf-8",
    )
    items = load_golden(path)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, GoldenItem)
    assert item.id == 1
    assert item.question == "Q"
    # Relevance is a SET of chunk ids (membership test, order-free).
    assert item.relevant_chunk_ids == {10, 11}
    assert item.expected_answer == "A"


# --- harness orchestration (offline: fake retrieve) ----------------------------------


class _StubChunk:
    """Minimal stand-in for a Chunk ORM row — the harness only reads `.id`."""

    def __init__(self, chunk_id: int) -> None:
        self.id = chunk_id


def _fake_retrieve_factory(rankings: dict[str, list[int]]):
    """Build a fake `retrieve` returning canned ranked chunk ids per query string."""

    async def fake_retrieve(query: str, *, k=None, owner_id=DEV_OWNER_ID):
        ranked = rankings[query]
        chunks = [
            ScoredChunk(chunk=_StubChunk(cid), score=1.0 / (i + 1))
            for i, cid in enumerate(ranked)
        ]
        return RetrievalResult(query=query, chunks=chunks)

    return fake_retrieve


async def test_evaluate_golden_scores_against_fake_retrieve(monkeypatch) -> None:
    golden = [
        GoldenItem(id=1, question="q1", relevant_chunk_ids={10}, expected_answer="a"),
        GoldenItem(id=2, question="q2", relevant_chunk_ids={99}, expected_answer="b"),
    ]
    # q1 retrieves 10 at rank 1 (hit, rr=1.0); q2 never retrieves 99 (miss, rr=0).
    monkeypatch.setattr(
        run_mod,
        "retrieve",
        _fake_retrieve_factory({"q1": [10, 20, 30], "q2": [1, 2, 3]}),
    )

    report = await evaluate_golden(golden, ks=[1, 3, 5, 10])

    assert report.summary["n_questions"] == 2
    # One of two queries hit at every cutoff.
    assert report.summary["hit_rate@1"] == pytest.approx(0.5)
    assert report.summary["hit_rate@3"] == pytest.approx(0.5)
    # MRR = (1.0 + 0.0) / 2.
    assert report.summary["mrr"] == pytest.approx(0.5)


async def test_write_report_persists_json(tmp_path, monkeypatch) -> None:
    golden = [
        GoldenItem(id=1, question="q1", relevant_chunk_ids={10}, expected_answer="a"),
    ]
    monkeypatch.setattr(run_mod, "retrieve", _fake_retrieve_factory({"q1": [10, 20]}))

    report = await evaluate_golden(golden, ks=[1, 3])
    out_path = write_report(report, tmp_path)

    assert out_path.exists()
    written = json.loads(out_path.read_text(encoding="utf-8"))
    # The results file carries the summary (the baseline future runs diff against).
    assert written["summary"]["hit_rate@1"] == pytest.approx(1.0)
    assert written["summary"]["mrr"] == pytest.approx(1.0)


# --- live end-to-end (real dev DB + real Gemini embed; opt-in, deselected) -----------


@pytest.mark.live
async def test_harness_runs_live_end_to_end() -> None:
    """The harness wires to the LIVE pipeline: real embed -> real pgvector -> metrics.

    Asserts plumbing, not quality — `relevant_chunk_ids` is left empty (so hit/MRR are 0
    and don't depend on churny DB ids); what's proven is that the real `retrieve()` runs
    through the harness and returns a non-empty ranking. Needs the dev Postgres up and
    GEMINI_API_KEY set; ERRORS loudly (never skips) when selected with `-m live`.
    """
    assert os.environ.get(
        "GEMINI_API_KEY"
    ), "GEMINI_API_KEY must be set for the live eval run"

    golden = [
        GoldenItem(
            id=1,
            question="Who is the CEO of Aurelia Robotics?",
            relevant_chunk_ids=set(),
            expected_answer="Marta Silveira",
        )
    ]
    report = await evaluate_golden(golden, ks=[1, 3, 5, 10])

    assert report.summary["n_questions"] == 1
    assert {"mrr", "hit_rate@1", "hit_rate@10"} <= set(report.summary)
    # Real retrieval returned a ranking (embed -> pgvector is live-wired through here).
    assert report.per_query[0]["retrieved_chunk_ids"]
