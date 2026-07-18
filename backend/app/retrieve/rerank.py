"""Q7 — rerank: reorder a retrieved shortlist with a cross-encoder, keep the top-n.

The last retrieval stage before generation. Semantic (Q2) + lexical (Q5) + RRF (Q6)
produce a *shortlist* fast, using signals that never read the query and a chunk
together: the bi-encoder embeds query and chunk SEPARATELY and compares vectors;
lexical counts shared lexemes. Both are cheap and shallow. A **cross-encoder** reads
`query` and one candidate's text *jointly* through a transformer and emits a single
relevance score — more accurate, because it can weigh how the query's terms actually
relate to the passage, but O(n): one forward pass per candidate. So it runs only over
the shortlist, never the corpus. That's the funnel this milestone completes: **retrieve
wide (a ~50-candidate pool) -> rerank narrow (the top few)**.

Why this closes the hit@1 gap: at Q6 recall was already maxed (the right chunk sits in
the top 10 for every eval question) but hit@1 was 0.812 — the right chunk was often at
rank 2, edged out by an adjacent sibling that shares surface features. Fusion can't fix
that; it only reconciles two shallow rankings. A cross-encoder can, because it judges
each candidate on the actual query-passage interaction, not a proxy.

The model — `cross-encoder/ms-marco-MiniLM-L-6-v2` (config `rerank_model`):
    A ~22M-param MiniLM trained on MS MARCO to score (query, passage) relevance. Chosen
    local (not a managed rerank API) because the whole torch/transformers stack is
    ALREADY installed — `unstructured[pdf]` pulls it in for M2's hi_res layout/table
    models — so a true cross-encoder costs ZERO new dependencies, just a one-time ~90 MB
    model download. It's a plain `*ForSequenceClassification` checkpoint (num_labels=1),
    so we load it straight through `transformers`, no `sentence-transformers` wrapper.
    See `docs/adr/0001-reranker-provider.md`.

The 512-token window (a real constraint, unlike the 8k-token embedder):
    The cross-encoder's max sequence length is 512 tokens, SHARED by the pair
    (`[CLS] query [SEP] passage [SEP]`). Our chunks cap at 1500 chars (~<=450 tokens)
    and a query is short, so a pair fits — but we set
    `truncation="only_second", max_length=512` so the densest max-size passage is
    trimmed (never the query) rather than erroring. This couples chunk size to the
    reranker: raising the 1500-char cap toward the embedder's much larger limit would
    start truncating passages here.

Off the event loop:
    The forward pass is CPU-bound and takes ~0.5-2 s for a ~50-chunk pool. `rerank` is
    async and may run inside the request-handling loop (Q10's `/ask`), so the sync
    torch/tokenizer work is dispatched via `asyncio.to_thread` — it must not block the
    loop while it burns CPU.

Score direction & raw logits:
    The returned `ScoredChunk.score` is the cross-encoder's raw logit — unbounded, NOT
    the 0-1 cosine or the RRF value it replaces. We keep it raw (no sigmoid): sigmoid is
    monotonic, so it wouldn't change the ORDER, and ordering is all rerank is for.
    Higher = more relevant, the package-wide convention (`types.py`).

Seams (`_get_reranker`, module-level):
    The cross-encoder is fetched through `_get_reranker`, a module global, so a test
    monkeypatches `rerank._get_reranker` with a fake scorer — the whole reorder path is
    drivable with no torch, no download, no CPU. Same seam discipline as
    `embed._get_client`. The heavy imports live INSIDE the loader, so importing this
    module (and running the offline suite) never pulls torch unless rerank runs live.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings
from app.retrieve.types import ScoredChunk

if TYPE_CHECKING:
    # Only for type hints; the real object is built lazily inside _get_reranker so torch
    # is never imported at module import time.
    Scorer = Callable[[str, list[str]], list[float]]


@lru_cache(maxsize=1)
def _get_reranker() -> Scorer:
    """Build (once per process) the cross-encoder scorer: (query, passages) -> [score].

    Lazily imports torch/transformers and loads `Settings.rerank_model` — a
    `*ForSequenceClassification` cross-encoder with a single relevance output. Cached
    like `embed._get_client`, so the ~90 MB model loads once and is reused. Returns a
    plain sync callable; `rerank` runs it off the event loop via `asyncio.to_thread`.
    """
    # torch is heavy; import only when rerank runs (never in the offline suite).
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    model_name = get_settings().rerank_model
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(model_name)
    model.eval()  # inference only — disable dropout etc.

    def score(query: str, passages: list[str]) -> list[float]:
        # Encode each candidate as a SENTENCE PAIR with the query:
        # [CLS] query [SEP] passage [SEP]. truncation="only_second" trims the passage
        # (never the query) to fit 512; padding batches ragged passages into one tensor.
        features = tokenizer(
            [query] * len(passages),
            passages,
            padding=True,
            truncation="only_second",
            max_length=512,
            return_tensors="pt",
        )
        with torch.no_grad():  # no gradients at inference — less memory, faster
            logits = model(**features).logits  # [n, 1] for a 1-label cross-encoder
        # squeeze the label dim -> one raw relevance logit per candidate.
        return logits.squeeze(-1).tolist()

    return score


async def rerank(
    query: str, chunks: list[ScoredChunk], *, top_n: int
) -> list[ScoredChunk]:
    """Reorder `chunks` by cross-encoder relevance to `query`, return the top `top_n`.

    Scores every candidate's `content` jointly with `query`, sorts best-first, truncates
    to `top_n`. Each returned `ScoredChunk` carries the cross-encoder's raw logit as its
    `score` (replacing the incoming RRF score; higher = more relevant). An empty
    shortlist returns `[]` without building the model.
    """
    if not chunks:
        return []

    scorer = _get_reranker()
    passages = [sc.chunk.content for sc in chunks]
    # CPU-bound torch work off the event loop so it never blocks a request handler.
    scores = await asyncio.to_thread(scorer, query, passages)

    # Stable sort by score descending: on a tie, candidates keep their incoming (fused)
    # order, so the result is deterministic without needing chunk ids (which may be
    # unset for freshly-built ScoredChunks in tests). strict=True: one score per chunk.
    ranked = sorted(
        zip(chunks, scores, strict=True), key=lambda cs: cs[1], reverse=True
    )
    return [ScoredChunk(chunk=sc.chunk, score=float(s)) for sc, s in ranked[:top_n]]
