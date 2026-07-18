"""Q6 fusion spec: `reciprocal_rank_fusion` merges ranked id lists into one ranking.

The *immutable spec* for Q6 (CLAUDE.md "test-first & test-immutable"): written and
watched fail (red, `ModuleNotFoundError`) before `app/retrieve/fuse.py` exists. The code
bends to these asserts.

What Q6's fusion is: given several ranked lists of chunk ids (semantic, lexical),
produce ONE ranking. **Reciprocal Rank Fusion (RRF)** scores each id by summing `1/(k +
rank)` over every list it appears in (rank is 1-based: the top result is rank 1), then
orders by that summed score, best-first. It uses only each id's *rank position*, never
its raw score — scale-freeness is the point (cosine similarity and `ts_rank_cd` live
on incomparable scales, so you cannot just add them).

Why this is the cleanest unit in the phase: it is PURE — no DB, no network, no clock,
just lists in and a ranking out — so every number below is hand-computed and exact. With
`k_constant = 60` (the default): rank 1 -> 1/61 ≈ 0.016393, rank 2 -> 1/62 ≈ 0.016129,
rank 3 -> 1/63 ≈ 0.015873.

The return is `list[tuple[int, float]]` — (chunk_id, fused_score) pairs, best-first — so
the caller (`retrieve`) can put the RRF score on each `ScoredChunk`. Ties in score break
deterministically by chunk_id ascending, so the ordering is fully reproducible.
"""

from __future__ import annotations

import pytest

from app.retrieve.fuse import reciprocal_rank_fusion

# --- known fused order + scores, two overlapping lists -------------------------------


def test_rrf_fuses_two_lists_with_hand_computed_scores() -> None:
    # A ranks [10, 20, 30]; B ranks [20, 40]. Only 20 appears in both.
    #   10: 1/61                       = 0.016393
    #   20: 1/62 (A r2) + 1/61 (B r1)  = 0.032522   <- agreement across lists wins
    #   30: 1/63                       = 0.015873
    #   40: 1/62 (B r2)                = 0.016129
    fused = reciprocal_rank_fusion([[10, 20, 30], [20, 40]])

    assert [cid for cid, _ in fused] == [20, 10, 40, 30]
    by_id = dict(fused)
    assert by_id[20] == pytest.approx(1 / 62 + 1 / 61)
    assert by_id[10] == pytest.approx(1 / 61)
    assert by_id[40] == pytest.approx(1 / 62)
    assert by_id[30] == pytest.approx(1 / 63)


# --- the hit@1 promotion: cross-list agreement beats a lone #1 -----------------------


def test_rrf_promotes_chunk_ranked_high_in_both_lists() -> None:
    # Semantic ranks chunk 1 first, chunk 2 second. Lexical ranks chunk 2 first. RRF
    # must PROMOTE chunk 2 above chunk 1 -- this is exactly how lexical rescues
    # semantic's near-misses (the hit@1 gap Q6 exists to close).
    #   1: 1/61                       = 0.016393
    #   2: 1/62 (sem r2) + 1/61 (lex r1) = 0.032522
    fused = reciprocal_rank_fusion([[1, 2], [2]])

    assert [cid for cid, _ in fused] == [2, 1]


# --- dedup: an id in both lists accumulates both contributions -----------------------


def test_rrf_dedups_and_sums() -> None:
    # Chunk 5 is rank 1 in both lists -> one entry, score 1/61 + 1/61.
    fused = reciprocal_rank_fusion([[5], [5]])

    assert len(fused) == 1
    cid, score = fused[0]
    assert cid == 5
    assert score == pytest.approx(2 / 61)


# --- k_constant is applied as 1/(k_constant + rank) ---------------------------------


def test_rrf_k_constant_is_used_in_denominator() -> None:
    # A single rank-1 id scores exactly 1/(k_constant + 1).
    assert reciprocal_rank_fusion([[100]], k_constant=1)[0][1] == pytest.approx(0.5)
    assert reciprocal_rank_fusion([[100]], k_constant=60)[0][1] == pytest.approx(1 / 61)

    # A larger k_constant flattens the gap between adjacent ranks (why 60 is a mild
    # prior, not a hard cutoff): rank1 - rank2 shrinks as k_constant grows.
    small = reciprocal_rank_fusion([[1, 2]], k_constant=1)
    large = reciprocal_rank_fusion([[1, 2]], k_constant=1000)
    gap_small = dict(small)[1] - dict(small)[2]
    gap_large = dict(large)[1] - dict(large)[2]
    assert gap_small > gap_large


# --- graceful degradation: an empty list contributes nothing ------------------------


def test_rrf_empty_list_degrades_to_the_other() -> None:
    # The 30/48 NL-question case: lexical returns []. Fusion must equal pure semantic
    # order (so hybrid never underperforms semantic).
    fused = reciprocal_rank_fusion([[1, 2, 3], []])

    assert [cid for cid, _ in fused] == [1, 2, 3]
    by_id = dict(fused)
    assert by_id[1] == pytest.approx(1 / 61)
    assert by_id[2] == pytest.approx(1 / 62)
    assert by_id[3] == pytest.approx(1 / 63)


def test_rrf_all_empty_returns_empty() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


# --- deterministic tie-break: equal scores order by chunk_id ascending ---------------


def test_rrf_ties_break_by_chunk_id() -> None:
    # 7 (rank 1 in A) and 3 (rank 1 in B) tie at 1/61. The result must be reproducible:
    # equal score -> lower id first.
    fused = reciprocal_rank_fusion([[7], [3]])

    assert [cid for cid, _ in fused] == [3, 7]
    assert dict(fused)[3] == pytest.approx(dict(fused)[7])
