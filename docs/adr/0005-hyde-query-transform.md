# ADR 0005 — HyDE query transform: whether to hallucinate a passage before we embed the query

- **Status:** Accepted — **ships gated OFF; eval showed no gain** (2026-07-24). See Consequences → eval gate for the numbers.
- **Milestone:** Phase 2.5, P4 (query rewrite / HyDE)
- **Deciders:** the architect (human); implemented by Claude Code

## Context

Phase 2 closed with a hybrid read path whose retrieval baseline (Q7, rerank ON) is **MRR 0.927 /
hit@1 0.896** over the 48-question golden set. The residual weakness is at **rank 1**: 8 of 10
near-misses had the correct chunk sitting at *rank 2*. The suspected cause is the query↔document
**asymmetry** — a question ("Who founded Aurelia Robotics?") and the passage that answers it
("Aurelia Robotics was founded in 2011 by…") are written differently, so the question's embedding
can land just far enough from the answer's to cost it the top slot.

**HyDE (Hypothetical Document Embeddings)** is a technique that attacks this directly: instead of
embedding the *question*, ask an LLM to write a short hypothetical passage that *would* answer it,
and embed *that*. A fake answer is answer-shaped, so its vector lands in passage space, near the
real answer chunks. Adopting it is a **RAG-core fork** — it puts an LLM generation call at the very
top of every read, adding latency, cost, and a new hallucination surface *on the retrieval side* —
so it needs an explicit decision, not a silent default.

`CLAUDE.md` names retrieval as hybrid + rerank but says nothing about query transforms, leaving the
choices below open. Each is the architect's call:

1. **Whether to transform the query at all** (accept the cost/risk for the rank-1 gain).
2. **Which role to embed the hypothetical in** — QUERY vs DOCUMENT.
3. **Which retrieval arm(s) the transform feeds** — both, or semantic only.
4. **How many hypotheses (N)** — one, or several pooled.

## Decision

1. **Adopt HyDE, behind a default-OFF gate (`hyde_enabled`), kept only if the eval proves it.**
   The stage is fully wired but ships disabled; it becomes the default only if a golden-set run
   beats the Q7 baseline beyond noise (see Consequences → eval gate). Otherwise it stays a studied,
   reversible lever, and this ADR records the negative result.
2. **Embed the hypothetical in the DOCUMENT role** (`as_retrieval_document`), the same role the
   real chunks were ingested in — so it lands in *passage* space. This is the whole premise of
   HyDE; embedding it in the QUERY role would treat it as merely a longer question and forfeit the
   point.
3. **Semantic arm only.** The lexical (full-text/BM25) arm keeps searching the **raw query
   string**. A synthetic multi-sentence passage would dilute keyword matching — the lexical arm's
   value is anchoring on the user's *real* terms, which is also the main thing keeping a
   hallucinated hypothetical from hijacking the result.
4. **N = 1** (a single greedy hypothesis at temperature 0). Deterministic and reproducible for
   eval, and cheap on the free-tier daily quota. Classic HyDE samples N>1 and mean-pools the
   vectors to cancel a bad hypothesis's variance; that is a documented future upgrade, not built
   here.
5. **Fail-open.** Any error from the generation call degrades to embedding the raw query (the exact
   non-HyDE path). A flaky HyDE call can never break retrieval — it can only fail to help.
6. **A dedicated `hyde_model`**, separate from `generation_model`, so the throwaway hypothetical
   can use a fast/cheap model without coupling to the answer model.

## Rationale

- **Transform at all.** The baseline's only real gap is rank-1 promotion, and HyDE targets exactly
  the asymmetry most likely to cause it. It is also gated and reversible, so the downside of trying
  it is bounded — if the eval says no, we flip the switch off and keep the teaching value.
- **DOCUMENT role over QUERY.** `gemini-embedding-2` encodes text differently by role; the real
  chunks live in DOCUMENT space. Putting the hypothetical in that same space is what makes "embed a
  fake answer" mean anything. (Chosen over reusing the existing QUERY-role `embed_query` seam,
  which would have been one line simpler but semantically wrong.)
- **Semantic-only over both-arms.** The handoff anticipated "the arms may take different texts."
  Feeding the passage to the lexical arm would blur its keyword signal; keeping the raw query there
  gives the hybrid a real-terms anchor, which is a *mitigation* for HyDE's hallucination risk, not
  just a neutral choice.
- **N=1 over N>1.** Quota discipline (48 generations per eval run vs 48×N) and determinism now;
  the multi-hypothesis variance reduction is a clean later add behind the same seam.
- **Fail-open over fail-closed.** Mirrors the P3 cache stance: an auxiliary enhancement must never
  be able to take down the core read path.

## Alternatives rejected

- **Do nothing (keep Q7 as the ceiling).** Leaves the rank-1 gap unexplored; HyDE is the
  cheapest-to-try lever aimed at it, and it is reversible.
- **Plain query rewrite** (LLM paraphrases/expands the *question*, still embedded in QUERY role)
  instead of HyDE. Simpler, but it stays in question-space — it doesn't cross the asymmetry gap the
  way embedding an answer-shaped passage does. Kept as a fallback if HyDE underperforms.
- **QUERY-role embedding of the hypothetical.** One line simpler, but fights HyDE's premise (an
  answer-shaped text encoded as a question). Rejected.
- **Feed the hypothetical to both arms.** Dilutes the lexical keyword match and removes the
  raw-terms anchor that limits hallucination damage. Rejected.
- **N>1 hypotheses + mean-pool** (the paper's default). Deferred purely on quota; the seam accepts
  it later without reshaping callers.
- **Blend the hypothetical vector with the raw-query vector** (weighted average, so retrieval never
  strays too far from the literal question). A promising robustness lever, deferred to keep N=1
  simple; documented in the teaching note.
- **Query-type routing** (run HyDE only on short/ambiguous queries). Deferred; needs its own
  heuristic and eval.

## Consequences

- **New module** `app/retrieve/hyde.py`: `generate_hypothetical(query) -> str`, reusing the shared
  google-genai client (`_get_client` seam) and GEMINI_API_KEY — **no new dependency**. Plain text
  out (no `response_schema`), unlike `generate.py`.
- **`retrieve.py`** gains a gated branch at the query→vector step: on `hyde_enabled`, generate →
  `embed_texts([as_retrieval_document(hypothetical)])[0]` for the semantic arm, fail-open to
  `embed_query(query)`; records a `hyde_ms` timing. `generate_hypothetical` and
  `as_retrieval_document` are added as monkeypatchable module seams. The lexical arm is untouched.
- **New config:** `hyde_enabled` (`False`), `hyde_model` (`gemini-3.1-flash-lite`),
  `hyde_temperature` (`0.0`), `hyde_max_output_tokens` (`256`).
- **Immutable tests untouched.** With `hyde_enabled` default-OFF, `retrieve()` is byte-identical to
  Q7, so `test_retrieve.py` / `test_ask.py` / `test_cache.py` pass unedited. The new branch is
  covered by `test_hyde.py` (4) + `test_retrieve_hyde.py` (5). Suite **173+5 → 182+5**, 0-skip.
  The `hyde_ms` key is safe against `test_retrieve.py:208` because that assertion is a **superset**
  check (`set(timings_ms) >= {...}`).
- **Cache × HyDE seam collision (noted, deferred).** The P3 cache keys on the raw-query vector
  (QUERY role) and passes it in via `query_embedding=`, which short-circuits HyDE. With the cache
  ON, HyDE would not fire on a cache miss unless the two are reconciled. Both default OFF and the
  cache is OFF during eval, so this is a documented concern for the parked cache work, not a P4
  blocker.
- **✅ EVAL GATE — RAN 2026-07-24 (architect), rerank ON. Result: no gain → HyDE stays gated OFF.**
  Baseline HyDE OFF (`eval/results/retrieval-20260724T094447_715100Z.json`) and HyDE ON
  (`eval/results/retrieval-20260724T094825_638419Z.json`) came back **identical to 16 decimals**:
  MRR **0.9267**, hit@1 **0.8958**, hit@3 **0.9375**, hit@5/@10 **1.000**. HyDE only reshuffled
  ranks **6–10** (irrelevant tail); since hit@5 = 1.0, every gold chunk was already in the final
  top 5, so no metric could move. **Why zero delta:** the cross-encoder reranker targets the *same*
  asymmetry-induced rank-2 near-miss HyDE targets, and already promotes it — so under rerank ON the
  reranker fully **masks** HyDE, and the per-query LLM call buys nothing. Decision rule not met (no
  improvement beyond noise) → **HyDE ships default-OFF**, a studied/reversible lever. Cost of the run:
  ~48 generation + ~48 embedding requests (rerank local, zero API).
- **Not-yet-run isolation experiment (optional):** the same A/B with **rerank OFF** would measure
  HyDE's raw *retrieval-layer* effect before the reranker erases it — the only way to see whether
  HyDE moves rank-1 at all here. Deferred on quota; would confirm/deny the "reranker masks HyDE"
  reading above.
- **Revisit if** the eval says HyDE doesn't beat the baseline (try plain rewrite, or N>1, or
  vector-blending), or if we later want query-type routing, or if the cache×HyDE reconciliation
  becomes worth building.
