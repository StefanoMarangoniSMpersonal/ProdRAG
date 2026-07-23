# ADR 0004 — Guardrails: hand-rolled validation, and repair-vs-fail on a phantom citation

- **Status:** Accepted (2026-07-23)
- **Milestone:** Phase 2.5, P2 (guardrails I/O around the read path)
- **Deciders:** the architect (human); implemented by Claude Code

## Context

Phase 2 closed with a working `POST /ask` read path (retrieve → generate → grounded, cited
answer + a frozen per-query audit log). `generate()` returns `citations: list[int]` — the chunk
ids the answer claims to have used — but **nothing checks them.** The model is shown *only* the
final reranked chunks (`generate(query, result.chunks)`), so a citation that isn't one of those
ids is a fabrication: a phantom the model invented, or an id it lifted from the wider candidate
pool it never actually saw. P2 adds the explicit validation layer that catches this, plus
formalizes the input guard Q10 had inline.

`CLAUDE.md` names **Guardrails AI** in the stack and fixes the *intent* ("instruct Gemini to
answer only from provided context … return citations to source spans") but leaves two
implementation choices open, which this ADR closes. Each is a design decision (the architect's
call), not a coding detail:

1. **How the guard is built** — the Guardrails-AI library vs a hand-rolled validator.
2. **What happens on a violation** — fail the request vs repair the response and flag it.

## Decision

1. **Hand-rolled validator, in a framework-free `app/guards.py`.** The output guard is a set
   membership partition (`set(citations) ⊆ set(final_ids)`); the input guard is a strip + length
   check. Both are a handful of pure lines that raise plain `ValueError` subclasses / return a
   dataclass — no FastAPI import, unit-testable without the web stack. The HTTP mapping lives in
   `ask.py`.
2. **Phantom-citation policy = repair & flag.** When the model cites an id it wasn't shown: drop
   the phantom from the **client-facing** citation list, still return the answer as **HTTP 200**,
   and log an `ask.citation_violation` WARNING. The **raw** model citations are preserved in both
   audit sinks (the stdout `ask.query` line and the `query_logs` row), so nothing the model did
   is hidden and the eval harness can count violations later.
3. **Input guard = blank + length cap, pre-spend.** `validate_query` rejects a blank or
   over-`max_query_chars` question with a 400 *before* the embedder or any billable call runs.
4. **A kill-switch, `citation_guard_enabled` (default on).** Off, the repair and the WARNING are
   suppressed and the model's raw citations flow through — a way to measure raw grounding in
   eval. The partition is still computed and logged either way, so violations stay visible in the
   logs regardless of the switch.

## Rationale

- **Hand-rolled over the library.** `requirements.txt` already threads several *load-bearing*
  pins (`langchain-community<0.4` — ragas 0.4.3 imports a module 0.4.x deleted;
  `pydantic-settings==2.14.2`; `ragas==0.4.3`; `redis<6.5`; `jsonref`). `guardrails-ai` pulls a
  heavy, pydantic/langchain-adjacent transitive tree that is a live risk of colliding with one of
  those. For a five-line check, the library is all cost and no leverage. The seam (`guards.py`)
  is where a library would slot in later *if* a richer validation need (structured PII scrubbing,
  competing validators, streaming re-ask) ever earns the dependency — and only once it proves
  `import`-clean against the pins.
- **Repair over fail-closed.** The answer has already been generated and billed by the time we
  can check the citations. Failing the request (a 500, or a forced re-ask) would throw away work
  the user paid for and turn a *good answer with one bad footnote* into no answer at all — the
  same best-effort stance the audit write already takes ("the audit matters; it does not matter
  more than the product"). Repair keeps the answer, quietly corrects the client-visible evidence
  trail, and records the raw truth for offline analysis. Fail-closed is the right call only where
  a wrong-but-confident answer is itself dangerous (medical/legal), which single-tenant learning
  RAG is not.
- **Guards in one framework-free module.** Input and output validation are the same idea — a
  boundary around the read path — so they live together, and keeping the module FastAPI-free
  makes it a pure core a future non-HTTP caller (batch eval, CLI) can reuse. `ask.py` owns the
  `ValueError → HTTPException(400)` and `phantom → WARNING + repaired response` translation.

## Alternatives rejected

- **`guardrails-ai` library now.** The named-stack choice, but a real dependency-collision risk
  against the pinned tree for a check this small. Revisit only when a need earns it *and* it
  imports clean.
- **Fail-closed (reject / re-ask on a phantom).** Discards a paid-for answer over a footnote,
  and a re-ask doubles latency and cost with no guarantee the second answer cites cleanly.
  Justified only under a much higher cost-of-wrong than this system carries.
- **Validate against the wider candidate pool (`candidate_chunk_ids`).** Wrong target: the model
  never saw the pre-rerank pool, so allowing citations from it would bless exactly the
  never-shown ids the guard exists to catch. The check must be against `final_ids` — the chunks
  actually in the prompt.
- **A durable phantom-citation DB column now.** Deferred (possible migration `005`): the raw
  citations are already stored, so violations are *reconstructable* from `query_logs` +
  `final_chunk_ids` without a schema change. Add a dedicated column only if we want cheap SQL
  violation-rate stats.
- **Input guards beyond blank + length now** (prompt-injection detection, PII screening).
  Deferred to a **separate future guardrails phase**, not attempted in P2. The blank/length checks
  are cheap, exact, and self-evidently correct; injection and PII defense are the opposite —
  adversarial, probabilistic, and easy to build *badly*. A half-working injection filter is worse
  than a known gap: it grants false confidence, silently passes the payloads it misses, and
  false-positives away legitimate questions. It also wants its own threat model, eval set, and
  failure policy (block? sanitize? flag?) — a milestone's worth of work, not a rider on the
  citation guard. `app/guards.py` is already the module it would land in when its phase comes.

## Consequences

- **New module** `app/guards.py`: `validate_query(raw, *, max_chars) -> str` (raising
  `EmptyQueryError` / `QueryTooLongError`), and `check_citations(citations, shown_ids) ->
  CitationCheck` (a frozen dataclass with `valid_citations`, `phantom_citations`,
  `has_violation`). Framework-free.
- **New config:** `citation_guard_enabled` (`True`) and `max_query_chars` (`4000`, well under the
  ~8,192-token embed cap).
- **`ask.py` wiring:** input via `validate_query` (blank still 400s, now also over-length);
  output via `check_citations` → an `ask.citation_violation` WARNING on a phantom and a repaired
  `AskResponse.citations`. The **audit contract is preserved and accumulated, never weakened**:
  the stdout line keeps raw `citations` and *adds* `valid_citations` / `phantom_citations`; the
  `query_logs` row keeps raw `citations` (no migration). The one client-visible change is the
  response's `citations` (repaired).
- **Immutable tests untouched:** the existing `test_ask.py` case cites `[13]` from a shown
  `{13, 11}`, so the repair is a no-op there — every Q10 assertion stays green unedited. The
  phantom path is exercised only by the new `test_guardrails.py`.
- **No eval re-run:** P2 changes neither what is retrieved nor the answer *text*, only the
  client-facing citation list and logging — so hit@k / MRR and RAGAS faithfulness are unaffected.
- **Revisit if** a richer validation need earns the `guardrails-ai` dependency (and it imports
  clean against the pins), or we want SQL violation-rate stats (durable phantom column, migration
  `005`), or the cost-of-wrong rises enough to justify fail-closed / re-ask, or we open the
  **input-guard expansion phase** (prompt-injection / PII screening — its own threat model, eval,
  and failure policy, landing in the same `app/guards.py`).
