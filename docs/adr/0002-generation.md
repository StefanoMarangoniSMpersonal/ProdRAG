# ADR 0002 — Generation: model, citation granularity, and refusal-eval scope

- **Status:** Accepted (2026-07-20)
- **Milestone:** Phase 2, Q8 (grounded, cited answer)
- **Deciders:** the architect (human); implemented by Claude Code

## Context

Q8 adds the final RAG read stage: hand the retrieved chunks to a *generation* model and get an
answer that is grounded in that context, refuses when the context lacks the answer, and cites
the chunks it used. `CLAUDE.md` fixes the *shape* (answer only from context; say "I don't
know"; return citations) but leaves three implementation choices open, which this ADR closes.
Each is a design decision (the architect's call), not a coding detail:

1. **Which generation model** the client defaults to.
2. **Citation granularity** — chunk-level ids vs finer char-spans.
3. **How much of the refusal/answer eval** Q8 builds now vs defers to Q9.

## Decision

1. **Generation model: `gemini-3.1-flash-lite`** (config `generation_model`, env
   `GENERATION_MODEL`). A fast, low-cost Gemini model, reached through the **same
   `google-genai` SDK and `GEMINI_API_KEY` as embedding** — no new dependency, no second key.
   It is a config value behind the `generate()` seam, so swapping to a heavier model is a
   one-line change.
2. **Citation granularity: chunk-level ids.** `GeneratedAnswer.citations` is a `list[int]` of
   `chunk.id`s. Each chunk is labeled `[chunk {id}]` in the prompt so the model has a stable
   handle to cite.
3. **Refusal eval now, RAGAS later.** Q8 ships a lightweight, phrase-based **refusal eval**
   over the 10 trap questions (`eval/refusal.py`): retrieve → generate → assert the model
   declined. Full answer-quality metrics (faithfulness / answer relevance / context
   precision·recall) wait for **Q9 (RAGAS)**.

## Rationale

- **flash-lite over pro / 2.0-flash.** Grounded generation is closer to *extraction* than
  open-ended reasoning — the hard work (finding the right chunks) is already done by
  retrieval + rerank, so a small fast model is a good fit and keeps the eval loop (48+ golden
  questions, re-run on every prompt/pipeline change) cheap and quick. `gemini-2.5-pro`-class
  reasoning is overkill here and slower/costlier per call; it's better justified once Q9 is
  measuring answer quality and we can A/B a stronger model against the numbers. A previous-gen
  flash was rejected for weaker instruction-following on the exact "only from context / say I
  don't know" constraint the whole refusal path depends on.
- **Chunk-level over char-span citations.** `chunk.id` is already the citation target
  `retrieve()` hands us, so chunk-level attribution costs nothing to plumb and nothing
  downstream has to reshape. Char-spans (`{chunk_id, start, end}`) give prettier highlighting
  but ask the LLM to emit reliable offsets — error-prone — and need span-validation logic.
  They can be added later *behind the same `GeneratedAnswer` model* without touching callers,
  so there's no reason to pay for them before chunk-level grounding is even proven.
- **Refusal eval now.** The "I don't know" path is the headline anti-hallucination behavior of
  a RAG system, and the 10 trap questions were deliberately reserved (from Q4) to test it. A
  cheap phrase-based pass/refuse check answers "does the grounding instruction actually hold on
  absent-answer questions" without waiting on the heavier RAGAS wiring — and gives Q9 a
  baseline to build on.

## Grounding mechanisms (how the decision is enforced)

Three levers, cheapest-first, all in `app/generate/generate.py`:

1. **System instruction** — a standing rule to answer only from the provided context, decline
   when it's absent, and cite chunk ids. Highest-leverage, zero per-call cost, imperfect alone.
2. **Temperature 0** (`generation_temperature`) — deterministic decoding: reach for the
   best-supported continuation, not a creative one. Grounding wants faithfulness, not variety.
3. **Structured output** — `response_mime_type="application/json"` + `response_schema=
   GeneratedAnswer`, so the answer and its citations come back as typed data we parse into the
   Pydantic model, not prose we'd have to scrape.

## Alternatives rejected

- **`gemini-2.5-pro` (or similar reasoning-tier model) as default.** Higher answer quality but
  slower + costlier per call, for a task that is mostly context extraction. Revisit via
  `generation_model` if Q9 shows flash-lite leaving quality on the table.
- **`gemini-2.0-flash` / previous-gen flash.** Cheapest, but weaker at obeying the
  only-from-context / refuse-when-absent instruction — the one behavior we most need.
- **Char-span citations in v1.** More precise, but a bigger ask of the model and more
  validation code; deferred behind the same Pydantic model.
- **Defer all eval to Q9.** Would leave the anti-hallucination behavior unmeasured through the
  whole milestone; the trap questions exist precisely to avoid that.

## Consequences

- **New config:** `generation_model` (`gemini-3.1-flash-lite`), `generation_temperature`
  (`0.0`), `generation_max_output_tokens` (`1024`). `gemini_api_key` is shared with embedding.
- **New shared client module** `app/gemini_client.py` (`get_client()`): the google-genai
  client factory, promoted out of `ingest/embed.py` so embedding and generation share **one**
  process-cached client — required so the worker's one-loop httpx-keepalive discipline (see
  `app/worker.py`) still holds. `embed.py` keeps a `_get_client` alias to that function, so its
  immutable tests are untouched.
- **New eval artifacts:** `eval/build_traps.py` → `eval/traps.jsonl` (the 10 traps, {id,
  question}, no relevant chunk ids), and `eval/refusal.py` (a live eval; writes
  `refusal-<ts>.json` alongside the retrieval results). The pure `is_refusal` helper is
  unit-tested offline.
- **The trap metric is an explicit-refusal rate — a LOWER BOUND, not hallucination
  resistance.** `is_refusal` is a keyword check on the answer text (an explicit decline), not
  on empty citations (a confident but uncited answer is a grounding failure, not a refusal).
  The first live run (`gemini-3.1-flash-lite`, 2026-07-20) scored **8/10 explicit refusals**,
  but inspection showed **0 real hallucinations** — both "misses" were the metric's fault, not
  the model's: **T5** ("Thornfield's football coach?") correctly answered "does not have a
  football program" *citing the supporting chunk* (a grounded negation, which carries no
  refusal phrase), and **T8** genuinely refused but phrased it "does not state" (outside the
  keyword list). So the report counts `non_refusals` as a **review queue, never a
  hallucination count**, and labels `explicit_refusal_rate` a lower bound. The robust
  fabricated-vs-grounded judgment is deferred to **Q9's LLM-as-judge / RAGAS faithfulness** —
  string matching cannot separate a correct grounded negation from a fabrication. (Decision:
  relabel honestly now, judge properly in Q9; `is_refusal` logic left unchanged.)
- **Revisit if** Q9 shows the model under-answering or under-refusing (model swap via
  `generation_model`), or if the UI needs span-level highlights (add char-spans behind
  `GeneratedAnswer`).
