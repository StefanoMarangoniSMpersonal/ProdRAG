"""Pure retrieval metrics: hit@k and the pieces of MRR.

These are the most tightly specified thing in Q4, and deliberately the simplest: no DB,
no network, no clock — just a ranked list of chunk ids and the set of truly-relevant ids
in, a number out. That purity is *why* they can be trusted, and why their test uses
hand-built rankings rather than the real pipeline.

Vocabulary (defined once, used throughout the eval):

  - **relevant set** — for one question, the chunk ids that actually contain its answer
    (the "gold" / ground truth, built by `build_golden.py`). Relevance is a property of a
    chunk *with respect to a query*, so it's a set passed alongside the ranking, never a
    flag on the chunk.
  - **hit@k** — did at least one relevant chunk land in the top `k` retrieved? A yes/no
    per query; averaged over all queries by `hit_rate_at_k` it becomes a fraction.
  - **reciprocal rank (RR)** — 1 / (rank of the *first* relevant chunk), or 0 if none was
    retrieved. Rank is 1-based: first result is rank 1. Rewards putting a relevant chunk
    HIGH, not merely somewhere.
  - **MRR (Mean Reciprocal Rank)** — the mean of RR across all queries.

Score direction is irrelevant here: metrics take the ids already *ranked* best-first (as
`retrieve()` returns them), so they never see or compare scores.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def hit_at_k(ranked_ids: Sequence[int], relevant_ids: set[int], k: int) -> bool:
    """True iff any relevant chunk id appears in the first `k` of `ranked_ids`.

    Empty `relevant_ids` (no gold — e.g. a trap question) or empty `ranked_ids` (nothing
    retrieved) can never hit, so both return False. `ranked_ids[:k]` is safe when the list
    is shorter than `k` — Python slicing just stops at the end.
    """
    return any(cid in relevant_ids for cid in ranked_ids[:k])


def reciprocal_rank(ranked_ids: Sequence[int], relevant_ids: set[int]) -> float:
    """1 / rank of the FIRST relevant chunk in `ranked_ids` (1-based), else 0.0.

    Only the first match matters — MRR asks "how high was the best answer", so a second
    relevant chunk further down changes nothing.
    """
    for rank, cid in enumerate(ranked_ids, start=1):
        if cid in relevant_ids:
            return 1.0 / rank
    return 0.0


def mrr(reciprocal_ranks: Iterable[float]) -> float:
    """Mean of per-query reciprocal ranks. 0.0 on an empty set (never divide by zero)."""
    rrs = list(reciprocal_ranks)
    return sum(rrs) / len(rrs) if rrs else 0.0


def hit_rate_at_k(per_query_hits: Iterable[bool]) -> float:
    """Fraction of queries that hit. 0.0 on an empty set (never divide by zero)."""
    hits = list(per_query_hits)
    return sum(1 for h in hits if h) / len(hits) if hits else 0.0
