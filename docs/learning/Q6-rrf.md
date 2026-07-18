# Q6 — Hybrid fusion: combining semantic + lexical with Reciprocal Rank Fusion

_Phase 2, milestone Q6. The milestone Q5 was building toward: `retrieve()` now runs both
signals and fuses them into one ranking._

Q6 is `app/retrieve/fuse.py::reciprocal_rank_fusion(rankings, *, k_constant=60)` plus the
rewiring of `retrieve()` to run semantic (Q2) and lexical (Q5) **concurrently** and fuse
their two rankings. The signature and every caller are unchanged — fusion slotted in behind
the Q3 seam, exactly as designed.

---

## 1. The problem: two rankings on incomparable scales

Semantic scores are cosine similarities (0–1). Lexical scores are `ts_rank_cd` values
(unbounded, corpus-dependent). You **cannot** just add or average them — a lexical 0.05 and
a cosine 0.75 don't mean "lexical is worse", they're different units. Any fusion that touches
the raw scores has to first make them comparable, and that's exactly the trap (§3).

## 2. Reciprocal Rank Fusion — throw the scores away, keep the ranks

RRF scores each chunk by its **rank position** in each list, never its raw score:

```
fused_score(chunk) = Σ_lists  1 / (k_constant + rank)      # rank 1-based: top = rank 1
```

Sum across the lists a chunk appears in, order by the sum, best-first. Two properties fall
straight out of the formula:

- **Rank, not score, drives it.** Being #1 is worth `1/(k+1)` whether the list is semantic
  or lexical — so the two signals combine *fairly* despite their scales. Scale-free by
  construction.
- **Cross-list agreement wins.** A chunk both signals rank highly sums two contributions and
  rises. This is the mechanism that closes the hit@1 gap: a chunk semantic ranks #2 but
  lexical ranks #1 scores `1/(k+2) + 1/(k+1)`, which **beats** a chunk that is only semantic's
  #1 (`1/(k+1)` alone). Dedup-by-summing *is* the feature.

**`k_constant = 60`** is the value from the paper that introduced RRF (Cormack, Clarke &
Buettcher, 2009) and the de-facto default. Because rank sits *inside* the denominator, the
gap between rank 1 and rank 2 (1/61 vs 1/62) is tiny — a mild prior that says "consensus
across lists matters more than any single list's #1". Larger k flattens rank differences,
smaller k sharpens them. Wired as `Settings.retrieval_rrf_k_constant` (env-overridable), so
tuning it is a config turn, not a code edit.

## 3. Why RRF over score-normalization (the rejected alternative)

The tempting alternative is to **normalize** each list's scores (min-max or z-score to 0–1)
and then add. It looks principled but silently assumes the two score *distributions* are
comparable after scaling — they aren't. One outlier `ts_rank_cd` value stretches the whole
min-max range and squashes every other lexical score toward 0; a semantic set clustered in
0.7–0.85 min-maxes into a totally different shape. RRF sidesteps the entire question by never
looking at a score — only the order. That's why it's the standard for combining heterogeneous
retrievers.

## 4. Concurrency and graceful degradation (the wiring)

`retrieve()` embeds the query once (only the semantic arm needs the vector; lexical searches
the raw string), then runs the two arms with **`asyncio.gather`**. Each arm opens its **own**
`SessionLocal` session — a single async DB connection can't service two queries at once, so
the concurrent arms can't share one. The live tracer confirms real overlap: on a sample query
`search_ms` ≈ `max(semantic_ms, lexical_ms)` (~125 ms), not their sum (~225 ms).

**Graceful degradation:** when the lexical arm matches nothing — the 30/48 natural-language
questions where `websearch_to_tsquery` ANDs terms into oblivion (Q5 §5) — RRF over one
non-empty list is just that list's order. So hybrid **can never underperform semantic**; the
worst case is "lexical adds nothing", never "lexical drags it down". This is why adding a weak
standalone signal (Q5's MRR 0.344) is safe.

## 5. Score semantics changed — and the immutable test that pinned it

The `score` on each returned `ScoredChunk` is now the **RRF fused score** (rank-based,
~1/61), **not** cosine similarity. That's the honest score for a fused result and keeps our
"score and order never disagree" discipline. It did conflict with the immutable Q3 test
(`test_retrieve_result_shape_and_timings`), which asserted `score ≈ 1.0` for an identical
vector — correct when `retrieve()` was semantic-only, now outdated. Per CLAUDE.md that's a
**spec revision** (architect-authorized), so those two asserts were revised to RRF-fused truth
(rank 1 = "near", `near.score > orthogonal.score > 0`, both `< 1.0`). The timings assertion
stayed green because `retrieve()` retains a `search_ms` key alongside the finer
`semantic_ms`/`lexical_ms`/`fuse_ms` breakdown. The other three Q3 tests passed unchanged
(their queries match nothing lexically → fusion = semantic order).

## 6. Eval: the hybrid-vs-semantic delta (the payoff)

Same golden set (48 questions, 9 docs / 97 chunks), now measuring the real hybrid `retrieve()`
(`eval/results/retrieval-20260718T140608_833817Z.json`):

| metric | semantic baseline | lexical alone | **hybrid (Q6)** |
|---|---|---|---|
| MRR | 0.881 | 0.344 | **0.895** |
| hit@1 | 0.792 | 0.333 | **0.812** |
| hit@3 | 0.979 | 0.354 | **0.979** |
| hit@10 | 1.000 | 0.354 | **1.000** |

**+0.020 hit@1, +0.014 MRR, zero regression.** hit@1 went from 38/48 to 39/48 — one
near-miss promoted to rank 1 by lexical agreement. Small, and worth understanding *why*
rather than wishing it bigger:

1. **30/48 questions get no lexical signal** (the AND-on-prose problem), so for the majority
   fusion == semantic — no room to move.
2. **Semantic was already strong** on a small, clean, single-domain-per-doc corpus:
   hit@3 = 0.979 and hit@10 = 1.000 leave almost no headroom above rank 1.
3. The gain that exists is exactly where theory predicts: **hit@1**, from an exact-token
   question where lexical outranked semantic.

The honest read: on *this* corpus RRF is a small, safe win. Its value grows with corpus size
and with more exact-token / proper-noun queries (where lexical is strongest), and the bigger
retrieval jump is expected from **rerank (Q7)**. Fusion's job was to combine the signals
correctly and without regression — done.

---

## Alternatives rejected
- **Score-normalization fusion** — assumes comparable distributions after scaling; one
  outlier skews it (§3). RRF is scale-free.
- **Weighted RRF (α·semantic + β·lexical)** — a tunable per-signal weight is a real option,
  but it's a knob to *learn*, and unweighted RRF is the standard baseline. Deferred until eval
  shows one signal should systematically count more.
- **Fusing inside SQL (one query with both rankings)** — possible with a CTE, but it welds the
  two signals into one statement and kills the clean pure-function seam + the concurrency.
  Kept them as two independent queries fused in Python.
- **Returning bare `list[ChunkId]` from `fuse()`** (the original plan signature) — then
  `retrieve()` would have to recompute scores. Returning `(id, score)` keeps the score with
  its rank, so the `ScoredChunk` carries the real RRF value.

## Gotcha hit during build
Running the two arms on one shared session raised no error in unit tests (they seed tiny data)
but is a latent "connection is busy" bug under real concurrency — the fix is a session per
arm. And `retrieve()` had to **retain the `search_ms` timing key** (now the fetch+fuse span)
so the immutable timings assertion stayed green while the finer per-arm timings were added.
