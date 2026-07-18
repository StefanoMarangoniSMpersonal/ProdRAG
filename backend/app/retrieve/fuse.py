"""Q6 — fusion: merge several ranked id lists into one, by Reciprocal Rank Fusion.

Q2 (semantic) and Q5 (lexical) each rank the same chunks on *incomparable* scales:
semantic scores are cosine similarities (0-1), lexical scores are `ts_rank_cd` values
(unbounded, corpus-dependent). You cannot just add or average them — a lexical score of
0.05 and a cosine of 0.75 don't mean lexical is worse. Fusion must be **scale-free**,
and the way to be scale-free is to throw the scores away and keep only each list's *rank
order*.

**Reciprocal Rank Fusion (RRF)** does exactly that. Each id scores, per list it's in:

    contribution = 1 / (k_constant + rank)      # rank is 1-based: top result = rank 1

and its fused score is the SUM of those contributions across all lists. Order by fused
score, best-first. Two consequences fall out of the formula:

  - **Rank, not score, drives everything.** Being #1 in a list is worth 1/(k+1) whether
    that list is semantic or lexical, so the two combine fairly despite their scales.
  - **Agreement across lists wins.** An id both signals rank highly sums two large
    contributions and floats to the top — which is how lexical rescues a chunk semantic
    ranked #2 (the hit@1 gap Q6 exists to close): 1/(k+2) + 1/(k+1) beats a lone
    1/(k+1). And when one list is EMPTY (the 30/48 natural-language questions where
    lexical matches nothing), RRF reduces to the other list's order — so hybrid never
    underperforms semantic.

Why `k_constant = 60`: the value from the paper that introduced RRF (Cormack, Clarke &
Buettcher, 2009) and the de-facto default (Elasticsearch, etc.). It's a mild smoothing
prior: because rank sits *inside* the denominator, the gap between rank 1 and rank 2
(1/61 vs 1/62) is small, so no single list can dominate on the strength of one top hit —
consensus across lists matters more than any one list's #1. A larger k flattens rank
differences further; a smaller k sharpens them. Wired as
`Settings.retrieval_rrf_k_constant` so it's a config turn, not a code edit, to tune.

Rejected alternative — **score normalization** (min-max or z-score each list, then add):
it *looks* principled but silently assumes the two score distributions are comparable
after scaling, which they aren't (one outlier `ts_rank_cd` value skews the whole min-max
range). RRF sidesteps the question entirely by never touching scores.

Pure by design: no DB, no clock — ids in, a ranking out. That's why it's spec'd with
hand-computed numbers (`test_fuse.py`) and why `retrieve()` maps the returned ids
back to `Chunk`s itself. Returns `(chunk_id, fused_score)` pairs, best-first, ties by
`chunk_id` ascending so the order is fully reproducible.
"""

from __future__ import annotations


def reciprocal_rank_fusion(
    rankings: list[list[int]], *, k_constant: int = 60
) -> list[tuple[int, float]]:
    """Fuse `rankings` (ranked chunk-id lists) into one, best-first, with RRF scores.

    Each id at 1-based `rank` in a list contributes `1 / (k_constant + rank)`; an id's
    fused score is the sum of its contributions across all lists (so an id in several
    lists is counted once, with its scores added — cross-list agreement). Returns
    `(chunk_id, fused_score)` pairs sorted by score descending, ties broken by
    `chunk_id` ascending for a deterministic order. Empty (or all-empty) input -> `[]`.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k_constant + rank)

    # Sort best-first; on a score tie, lower chunk_id first (reproducible ordering).
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
