"""Q7 rerank spec: reorder a candidate shortlist with a cross-encoder, keep the top-n.

The *immutable spec* for Q7 (CLAUDE.md "test-first & test-immutable"): written and
watched fail (red, `ModuleNotFoundError`) before `app/retrieve/rerank.py` exists. The
code bends to these asserts.

What Q7's rerank is: retrieval (semantic + lexical + RRF, Q2-Q6) hands back a
*shortlist* of candidate chunks ranked by cheap signals. A **cross-encoder** then reads
the query and each candidate's TEXT *together* and scores relevance directly — more
accurate than the bi-encoder embeddings we retrieve with, but O(n), so it only runs over
the shortlist. `rerank` reorders the shortlist by that score and truncates to `top_n`
(the retrieve-wide -> rerank-narrow funnel).

The scorer is a SEAM: `rerank` gets its cross-encoder from the module-level
`_get_reranker`, so these tests swap in a fake scorer — no torch, no ~90 MB model
download, no CPU forward pass. What's pinned here is `rerank`'s *orchestration* (what it
feeds the scorer, how it uses the scores), not the model itself (exercised live). A fake
scorer takes `(query, passages)` and returns one float per passage, higher = more
relevant — the same direction the whole retrieve package uses (see `types.py`).
"""

from __future__ import annotations

import app.retrieve.rerank as rerank_mod
from app.models import Chunk
from app.retrieve.rerank import rerank
from app.retrieve.types import ScoredChunk


class _RecordingScorer:
    """A fake cross-encoder: returns a preset score per passage and records its inputs.

    `scores_by_text` maps a passage's text -> the relevance score to hand back, so a
    test can make any candidate 'win'. Records every `(query, passages)` call so a test
    can assert `rerank` fed it the query and each chunk's content, in shortlist order.
    """

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self._scores_by_text = scores_by_text
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        return [self._scores_by_text[p] for p in passages]


def _candidate(content: str, score: float) -> ScoredChunk:
    """A retrieval candidate: a Chunk with the given text, carrying a pre-rerank (RRF)
    score. The RRF score should be IGNORED by rerank — the cross-encoder re-scores from
    scratch — so these are set deliberately opposite to the rerank order below."""
    return ScoredChunk(chunk=Chunk(content=content), score=score)


# --- rerank feeds the scorer the query + every candidate's content, in order ---------


async def test_rerank_passes_query_and_all_contents_to_scorer(monkeypatch) -> None:
    scorer = _RecordingScorer({"A": 0.1, "B": 0.2, "C": 0.3})
    monkeypatch.setattr(rerank_mod, "_get_reranker", lambda: scorer)

    # Pre-rerank order A, B, C (descending RRF score) — the shortlist rerank receives.
    candidates = [_candidate("A", 0.9), _candidate("B", 0.8), _candidate("C", 0.7)]
    await rerank("my question", candidates, top_n=3)

    # Exactly one scoring call, given the query and the chunk CONTENTS in shortlist
    # order (the cross-encoder needs each candidate's raw text paired with the query).
    assert scorer.calls == [("my question", ["A", "B", "C"])]


# --- rerank reorders by the cross-encoder score, not the incoming RRF order -----------


async def test_rerank_reorders_by_score_descending(monkeypatch) -> None:
    # The scorer disagrees with retrieval: it ranks C highest, then A, then B. This is
    # the hit@1 promotion Q7 exists for — a chunk retrieval put at rank 3 (C) can be the
    # true best answer once query and passage are read together.
    scorer = _RecordingScorer({"A": 5.0, "B": 1.0, "C": 9.0})
    monkeypatch.setattr(rerank_mod, "_get_reranker", lambda: scorer)

    candidates = [_candidate("A", 0.9), _candidate("B", 0.8), _candidate("C", 0.7)]
    result = await rerank("q", candidates, top_n=3)

    assert [sc.chunk.content for sc in result] == ["C", "A", "B"]


# --- the returned score is the cross-encoder's score, not the old RRF score -----------


async def test_rerank_returns_cross_encoder_scores(monkeypatch) -> None:
    scorer = _RecordingScorer({"A": 5.0, "B": 1.0})
    monkeypatch.setattr(rerank_mod, "_get_reranker", lambda: scorer)

    candidates = [_candidate("A", 0.9), _candidate("B", 0.8)]
    result = await rerank("q", candidates, top_n=2)

    by_content = {sc.chunk.content: sc.score for sc in result}
    assert by_content == {"A": 5.0, "B": 1.0}


# --- rerank-narrow: truncate the reordered shortlist to top_n ------------------------


async def test_rerank_truncates_to_top_n(monkeypatch) -> None:
    scorer = _RecordingScorer({"A": 1.0, "B": 4.0, "C": 3.0, "D": 2.0})
    monkeypatch.setattr(rerank_mod, "_get_reranker", lambda: scorer)

    candidates = [
        _candidate("A", 0.9),
        _candidate("B", 0.8),
        _candidate("C", 0.7),
        _candidate("D", 0.6),
    ]
    result = await rerank("q", candidates, top_n=2)

    # A wide pool (4) reranked down to the 2 the cross-encoder scored top: B, then C.
    assert [sc.chunk.content for sc in result] == ["B", "C"]
    assert len(result) == 2


# --- empty shortlist: nothing to score, nothing to load ------------------------------


async def test_rerank_empty_shortlist_returns_empty(monkeypatch) -> None:
    # No candidates -> return [] WITHOUT touching the scorer (so the model is never
    # loaded for an empty pool). A scorer that explodes if called proves the guard.
    def _boom() -> None:
        raise AssertionError("scorer must not be built for an empty shortlist")

    monkeypatch.setattr(rerank_mod, "_get_reranker", _boom)

    assert await rerank("q", [], top_n=5) == []
