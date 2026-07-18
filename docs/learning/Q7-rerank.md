# Q7 — Cross-encoder rerank

_Phase 2, milestone Q7. Builds on Q6 (hybrid RRF fusion): `retrieve()` now, when enabled,
re-scores the fused candidate pool with a cross-encoder before returning the top-k._

## The problem Q7 targets

At Q6 the hybrid baseline was **MRR 0.895 · hit@1 0.812 · hit@10 1.000**. Read that last
number carefully: **recall is already maxed** — for every one of the 48 eval questions, the
right chunk is somewhere in the top 10. The whole remaining gap is **hit@1**: the right chunk
is *retrieved* but not *first*. In 8 of the 10 near-misses it sat at rank 2, edged out by an
adjacent sibling chunk that shares surface features (same section, similar wording).

Neither retrieval signal can fix that, because of *how* they score:

- **Semantic (bi-encoder).** A *bi-encoder* embeds the query and each chunk **separately**,
  into vectors, then compares the vectors (cosine). The chunk's vector was computed at ingest
  time, before your query existed — it's a fixed summary of the chunk, blind to what you asked.
- **Lexical (full-text).** Counts shared lexemes. Shallower still.
- **Fusion (RRF).** Only *reconciles* those two rankings; it never looks at the text again.

So to move the right chunk from rank 2 to rank 1 you need a model that actually **reads the
query and the candidate together**.

## Bi-encoder vs cross-encoder (the core idea)

A **cross-encoder** feeds the pair into one transformer as a single sequence —
`[CLS] query [SEP] passage [SEP]` — and lets every query token attend to every passage token
before emitting one relevance score. That cross-attention is exactly what a bi-encoder throws
away by encoding the two sides in isolation, and it's why a cross-encoder is markedly more
accurate at judging "does this passage answer this query".

| | Bi-encoder (retrieval) | Cross-encoder (rerank) |
|---|---|---|
| Sees query+passage together? | No | **Yes** |
| Cost | embed once at ingest, then a cheap vector compare | **one full forward pass per (query, chunk)** |
| Scales to the whole corpus? | Yes (millions of vectors) | No — O(n) per query |
| Job | **recall** — fetch the right candidates | **precision** — order the shortlist |

The cost line is the catch: a cross-encoder can't score a million chunks per query. So it never
touches the corpus — it only reorders a **shortlist**. That's the funnel:

> **retrieve wide** (hybrid fetches a ~50-candidate pool) **→ rerank narrow** (the
> cross-encoder reorders that pool, we keep the top-k).

This is why Q7 widened the per-arm fetch to `retrieval_candidate_k` (50) but kept `k` (the
final result size) unchanged: reranking a pool no bigger than `k` could never reorder the
top-k. You must over-fetch, *then* narrow.

## The model, and why it cost zero new dependencies

`cross-encoder/ms-marco-MiniLM-L-6-v2` — a ~22M-param MiniLM trained on MS MARCO to score
(query, passage) relevance. The decision to run it **locally** rather than call a managed
rerank API (Cohere/Voyage/Jina) is written up in `docs/adr/0001-reranker-provider.md`. The
short version: the entire `torch` + `transformers` stack is **already installed** — pulled in
by `unstructured[pdf]` for M2's hi_res layout/table models — so a *true* cross-encoder costs
**zero new pip dependencies**, only a one-time ~90 MB model download. It's a plain
`*ForSequenceClassification` checkpoint (a single relevance output), so we load it straight
through `transformers` (`AutoTokenizer` + `AutoModelForSequenceClassification`) — no
`sentence-transformers` wrapper needed.

## The 512-token window ↔ chunk-size coupling (a real constraint)

Unlike the embedding model (`gemini-embedding-2`, ~8,192-token input, huge headroom), the
cross-encoder's max sequence length is only **512 tokens** — and that budget is **shared** by
the pair (`[CLS] query [SEP] passage [SEP]`). We checked the real corpus: max chunk 1,358
chars (~340–450 WordPiece tokens), avg 602; plus a short query, a pair fits comfortably. We
still set `truncation="only_second", max_length=512` so the densest max-size passage is
trimmed (the passage, never the query) rather than erroring.

The lesson worth carrying: **the 1500-char chunk cap is what keeps chunks inside the reranker's
512-token window.** If you ever raised `ingest_chunk_max_characters` toward the embedder's much
larger limit, you'd start silently truncating passages here — chunk size and reranker choice
are coupled.

## Two implementation details that matter

- **Raw logits, no sigmoid.** The reranker outputs an unbounded relevance logit; we store it
  directly as `ScoredChunk.score` (replacing the RRF value). We deliberately *don't* squash it
  through a sigmoid: sigmoid is monotonic, so it wouldn't change the **order**, and ordering is
  the only thing rerank is for. (Higher = more relevant, the package-wide score direction.)
- **Off the event loop.** The forward pass is CPU-bound (~0.5–2 s for a 50-chunk pool). Because
  `retrieve()`/`rerank()` are `async` and will run inside the request loop at Q10 (`/ask`), the
  synchronous torch work is dispatched via `asyncio.to_thread` — it must not block the loop
  while it burns CPU.

## Gated, so the baseline is preserved

Rerank is behind `Settings.rerank_enabled`, default **False**. With it off, `retrieve()` fetches
only `k` per arm and returns the fused top-k — **byte-identical to Q6**, no torch imported, no
model download, the offline suite untouched (the 4 immutable Q3 specs and 7 fuse specs stayed
green with no edits). You turn it on with `RERANK_ENABLED=true` for live eval and prod. This is
the same "graceful, opt-in" discipline as the rest of the pipeline.

## Rejected alternatives

- **A managed rerank API (Cohere/Voyage/Jina).** A true cross-encoder behind an API, ~10-line
  integration — but a new SDK dependency, a new API key, network latency, and per-query cost,
  for *no* dependency saving here (torch is already present). See the ADR.
- **Gemini-as-reranker.** Prompt the LLM to score (query, chunk) relevance. Zero new dep
  (google-genai is installed), but it's an LLM relevance *judgement*, not a literal
  cross-encoder — it undercuts the milestone's whole point and carries LLM latency/cost.
- **`sentence-transformers` `CrossEncoder` wrapper.** Convenient, but it would pull
  `scikit-learn` as a genuinely-new dependency, and the raw `transformers` path is ~15 lines —
  so we skipped the wrapper.

## Eval delta (measured)

Live run, `RERANK_ENABLED=true`, 48-question golden set, candidate pool 50
(`eval/results/retrieval-20260718T192246_887014Z.json`), vs the Q6 hybrid baseline:

| metric | Q6 hybrid | Q7 rerank | Δ |
|---|---|---|---|
| MRR    | 0.895 | **0.927** | +0.032 |
| hit@1  | 0.812 | **0.896** | **+0.084** |
| hit@3  | 0.979 | 0.938 | **−0.041** |
| hit@5  | 0.979 | **1.000** | +0.021 |
| hit@10 | 1.000 | 1.000 | — |

**Reading — a clear win on the target metric, but NOT a Pareto improvement.** hit@1 jumped
8.4 points (5 questions promoted to rank 1: ids 5, 29, 33, 43, 44) and MRR rose 3.2 — exactly
the top-rank-precision lever Q7 exists for — and hit@5 reached a perfect 1.000. But the
cross-encoder also **demoted 2 questions out of the top-3** (hit@3 −0.041): id 22 ("NovaBridge
rate limit") fell from rank 1 to rank 4, and id 47 (a two-company revenue comparison) slipped
from the top-3 to rank 5.

The honest lesson: **a reranker is not magic.** It sharpens the very top on average (hit@1, MRR)
but adds variance in the near ranks, and a small MS-MARCO-tuned MiniLM, run over an *adversarial
off-domain* corpus (fictional companies with near-duplicate numeric facts), can misjudge exactly
the cases it's supposed to nail — precise-number and comparison questions where several sibling
chunks look almost identical. The net is positive and worth keeping (MRR and hit@1 both up, hit@5
perfected), but the id-22 rank-1→4 demotion is the kind of regression that would justify, later,
a stronger reranker (`bge-reranker-*`) or tuning `retrieval_candidate_k` — both are swaps behind
the `rerank` seam, not reshapes.

