# ADR 0001 — Reranker provider: a local cross-encoder via `transformers`

- **Status:** Accepted (2026-07-18)
- **Milestone:** Phase 2, Q7 (cross-encoder rerank)
- **Deciders:** the architect (human); implemented by Claude Code

## Context

Q7 adds the cross-encoder rerank stage: retrieve a wide candidate pool (hybrid semantic +
lexical + RRF), then re-score `(query, chunk)` pairs with a cross-encoder and keep the top-k.
`CLAUDE.md` fixes the *shape* ("…then **cross-encoder rerank** down to 5–10 chunks") but leaves
the **provider** open — this ADR closes it. The reranker must run on **CPU** (no GPU in this
project) and fit a single-tenant, human-paced learning build.

Three realistic options were on the table:

1. **Managed rerank API** — Cohere Rerank (`rerank-v3.5`), Voyage, or Jina. A purpose-built
   cross-encoder behind an HTTP call.
2. **Gemini-as-reranker** — prompt the already-wired Gemini LLM to score relevance.
3. **Local cross-encoder** — a HuggingFace `*ForSequenceClassification` model run on CPU.

## Decision

**Run a local cross-encoder, `cross-encoder/ms-marco-MiniLM-L-6-v2`, loaded through the
already-installed `transformers` library** (config `rerank_model`, gated by `rerank_enabled`).

The deciding fact came from checking the venv before assuming a dependency cost: the entire
PyTorch/HuggingFace stack (`torch` 2.12, `transformers` 5.13, `tokenizers`, `safetensors`,
`huggingface_hub`, `accelerate`) is **already installed** — `unstructured[pdf]` pulls it in for
M2's hi_res layout/table models (a locked decision in `CLAUDE.md`). So torch is **sunk cost**,
already load-bearing in the same venv the query path runs in.

Given that, a local cross-encoder is the cheapest *and* most faithful option:

- **Zero new pip dependencies.** The model is a plain `BertForSequenceClassification`
  checkpoint (num_labels=1), loadable via `AutoTokenizer` + `AutoModelForSequenceClassification`
  — no `sentence-transformers` wrapper (which would add `scikit-learn`). Only a one-time
  **~90 MB model download**, cached under `~/.cache/huggingface`.
- **A true cross-encoder**, so the milestone's bi-encoder-vs-cross-encoder lesson is the real
  thing, not an approximation.
- **Offline, keyless, testable** — the only option that needs no network and no API key, so CI
  can (in principle) run the real model; the unit tests fake the `_get_reranker` seam and pull
  no torch at all.
- **Window fits the corpus:** 512-token max (shared query+passage) vs our ≤1,358-char chunks
  (~≤450 tokens) + short query; `truncation="only_second", max_length=512` is the safety net.

## Alternatives rejected

- **Managed rerank API (Cohere/Voyage/Jina).** A genuine cross-encoder with the lowest latency
  (~100–400 ms network vs ~0.5–2 s local CPU) and no local compute. Rejected because it buys a
  **new SDK dependency + a new API key + network dependency + per-query cost** for *no*
  dependency saving here — torch is already installed. A managed API would make more sense at
  real scale / multi-tenant throughput, which is explicitly out of scope (`CLAUDE.md`).
- **Gemini-as-reranker.** Zero new dependency (google-genai is wired), and the client pattern
  would foreshadow Q8 generation. Rejected because it's an **LLM relevance judgement, not a
  literal cross-encoder** — it undercuts the milestone's teaching goal, and adds LLM latency and
  per-query token cost where a 22M-param model does the job locally for free.

## Consequences

- **Cost accepted:** ~0.5–2 s/query CPU to score a ~50-chunk pool, a ~1–3 s one-time model load
  per process, and a few-hundred-MB resident RAM footprint in whichever process runs the query
  path (the Q10 API server + the eval run — **not** the Celery ingest worker). Fine for
  single-tenant, interactive use.
- **New config:** `rerank_enabled` (default False — off preserves the exact Q6 baseline, no
  torch imported), `retrieval_candidate_k` (pool width, 50), `rerank_model`.
- **First live run downloads the model** (~90 MB) from HuggingFace Hub; needs network once.
- **Coupling recorded:** chunk size (`ingest_chunk_max_characters=1500`) must stay within the
  512-token window; raising it later would truncate passages at rerank time.
- **Revisit if** we ever go multi-tenant / high-QPS (a managed API's latency and no-local-compute
  become worth the key + cost), or if eval shows this model underperforming a stronger reranker
  (`bge-reranker`, etc.) — both are `rerank_model` / provider swaps behind the `rerank` seam,
  not a reshape of callers.
