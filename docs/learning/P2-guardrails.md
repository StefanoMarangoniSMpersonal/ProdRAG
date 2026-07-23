# P2 — Guardrails: verifying the citations the model hands back

_Phase 2.5, milestone P2. Turns the citation list from something we trust into something we
check. `generate()` already tries three ways to keep the answer grounded; P2 adds the fourth
thing none of those can do — confirm, after the fact, that every cited chunk was one the model
was actually shown._

---

## 1. Implicit grounding vs an explicit guard

`generate.py` has three *implicit* grounding levers (ADR 0002), all applied **before or during**
the model call:

1. **System instruction** — "answer ONLY from the provided context; cite the chunk ids you used."
2. **Temperature 0** — deterministic decoding, reach for the best-supported continuation.
3. **Structured output** — a JSON schema so citations come back as typed `list[int]`, not prose.

Every one of these is a way of *asking nicely*. They shape what the model is likely to do; none
of them **guarantees** it. A model at temperature 0, told to cite only from context, handed a
strict schema, can still emit `citations: [13, 999]` when it was only ever shown chunks 13 and
11 — because 999 is a syntactically valid integer that satisfies the schema. The schema checks
the *shape* of the citation, not its *truth*.

P2 is the first **explicit** guard: it runs *after* the model has spoken and treats the citations
as claims to verify. This is the general shape of guardrails — not "make the model behave" but
"check the output and decide what to do when it didn't." The two are complementary, not
redundant: the implicit levers make violations rare; the explicit guard makes the rare ones
harmless.

## 2. What a "phantom citation" is, precisely

The check hinges on one fact about the pipeline: the model is shown **only the final reranked
chunks**. In `ask.py`:

```python
answer = await generate(query, result.chunks)   # result.chunks == the final, reranked set
final_ids = [sc.chunk.id for sc in result.chunks]
```

So the set of ids the model *could* legitimately cite is exactly `final_ids`. A **phantom** is
any citation outside it:

```python
set(answer.citations) ⊄ set(final_ids)   # a phantom is present
```

The subtle part is **which** id set to validate against. Retrieval is a funnel:
`candidate_chunk_ids` (the wide pre-rerank pool) → rerank → `final_ids` (the narrow shown set).
It is tempting to validate against the wider `candidate_chunk_ids` — but the model **never saw**
that pool. Validating against it would *bless* exactly the never-shown ids the guard exists to
catch. The target has to be `final_ids`: what was literally in the prompt.

Empty citations (the refusal path — "I don't know") pass trivially: there is nothing to
fabricate, so `has_violation` is `False`.

## 3. The guard is a pure, framework-free core

`app/guards.py` imports no FastAPI. It offers two pure pieces:

```python
def validate_query(raw, *, max_chars) -> str          # input: strip + non-blank + length cap
def check_citations(citations, shown_ids) -> CitationCheck   # output: partition into valid/phantom
```

`validate_query` raises plain `ValueError` subclasses (`EmptyQueryError`, `QueryTooLongError`);
`check_citations` returns a frozen `CitationCheck(valid_citations, phantom_citations,
has_violation)`. Neither knows what HTTP is. `ask.py` owns the translation — `ValueError →
HTTPException(400)`, and `phantom → WARNING + repaired response`.

Why the separation? The same reason `retrieve()` / `generate()` are framework-free: a pure core
is unit-testable without spinning up a web app, and reusable by a future non-HTTP caller (a batch
eval, a CLI) that has no request/response to map to. The web concern lives at the web boundary;
the logic lives underneath it. Both guards sit in **one** module because input-validation and
output-validation are the same idea from two sides — a boundary around the read path.

## 4. Repair-and-flag, not fail-closed

When a phantom shows up, there are two schools:

- **Fail-closed** (validate-and-fail): reject the request, or force a re-ask.
- **Repair-and-flag** (validate-and-repair): fix the output, return it, and record what happened.

P2 chose repair (ADR 0004). The deciding fact is **timing**: the citation check runs *after*
`generate()` — the answer is already produced and already billed. Fail-closed would throw that
away and turn *a good answer with one bad footnote* into no answer at all. A re-ask doubles
latency and cost with no guarantee the second attempt cites cleanly. So instead:

- drop the phantom from the **client-facing** `citations` (repair),
- keep the answer, return **HTTP 200** (never punish the user for a footnote),
- log an `ask.citation_violation` WARNING (flag),
- preserve the **raw** citations in both audit sinks (record the truth).

This is the exact stance the audit write already takes — "the audit matters; it does not matter
more than the product." Fail-closed is the right call only when a confident-but-wrong answer is
itself dangerous (medical, legal); a single-tenant learning RAG is not that.

**The audit contract is accumulated, never weakened.** The frozen per-query log keeps its RAW
`citations` key (what the model *did*) and *adds* `valid_citations` / `phantom_citations`
alongside; the `query_logs` row keeps raw citations (no migration). Only the response's
`citations` changes — the one client-visible effect. So the raw model behavior stays fully
reconstructable offline: the eval harness can count violation rates later without a schema change.

The `citation_guard_enabled` kill-switch (default on) gates the two *actions* (the repair and the
WARNING) but **not** the computation — the partition is always computed and logged, so even with
the guard off you can see phantoms in the logs while eval receives the model's untouched
citations to score raw grounding.

## 5. The input guard: fail fast, fail cheap

The output guard is the interesting half; the input guard is the disciplined half. `validate_query`
formalizes the blank-query check Q10 had inline and adds a **length cap** (`max_query_chars`,
4000, well under the embedder's ~8,192-token input limit). Both run at the very top of `ask()`,
**before** `retrieve()` embeds anything or `generate()` spends a billable call — a blank or a
20,000-character paste is a caller error we can know synchronously, so we reject it with a 400 and
spend nothing. Same principle as the empty-upload 400 in `documents.py`: validate what you can, as
early as you can.

Prompt-injection and PII screening are deliberately **not** here — those are a phase of their own,
and doing them badly is worse than not yet.

## 6. Why hand-rolled beat the library

`CLAUDE.md` names Guardrails-AI in the stack, but P2 hand-rolled the check (ADR 0004). The check
is ~5 lines; the library would drag a heavy pydantic/langchain-adjacent transitive tree into a
`requirements.txt` that already threads several **load-bearing** pins (`langchain-community<0.4`,
`pydantic-settings==2.14.2`, `ragas==0.4.3`, `redis<6.5`, `jsonref`) — a real collision risk for
no leverage. `guards.py` is the seam the library would slot into later *if* a richer need ever
earns the dependency and it proves import-clean. Naive-first, exactly as the RAG core is built.

## 7. What's verified

- Offline suite **+13** with `test_guardrails.py`, all red-first (the module didn't exist):
  - **Unit (9):** `check_citations` — all-valid, a phantom detected, empty passes, order +
    duplicates preserved; `validate_query` — strip/return, blank raises, whitespace raises,
    over-cap raises, boundary (`len == max`) passes.
  - **Endpoint (4, via `ASGITransport` + committing `session_factory`):** a phantom is repaired
    out of the response, a WARNING naming it is emitted, HTTP 200, and the `query_logs` row still
    carries the RAW citations; a fully-valid answer passes untouched (no WARNING); an over-length
    query 400s pre-spend (`retriever.calls == []`, no billable call, no audit row); a blank query
    400s via the formalized guard.
- **Immutable tests untouched:** `test_ask.py` cites `[13]` from a shown `{13, 11}` → repair is a
  no-op → every Q10 assertion (response citations, raw `QueryLog.citations`, the log line) stays
  green **unedited**. The lone client-visible change (repaired `citations`) is invisible where
  there's no phantom.
- **No eval re-run:** retrieval and answer *text* are unchanged, so hit@k / MRR and RAGAS
  faithfulness are unaffected. (The dev DB is **not** re-ingested — `golden.jsonl` is keyed to it.)
