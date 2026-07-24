# P4 — HyDE: embedding a fake answer instead of the question

_Phase 2.5, milestone P4. Adds a **query transform** at the very top of the read path: when
`hyde_enabled`, `retrieve()` doesn't embed the user's question — it first asks an LLM to
**hallucinate a short passage that would answer it**, then embeds *that* to drive the
semantic search. This note explains why that helps, then walks the design, and — the part
worth reading twice — where HyDE is fragile and how we blunt each failure._

---

## 1. The problem HyDE attacks: the query↔document asymmetry

Retrieval works by putting the query and the chunks in the **same vector space** and
finding the nearest chunks. But an embedding model encodes text by its *shape*, and a
**question** and its **answer** have different shapes:

```
question:  "Who founded Aurelia Robotics?"
answer:    "Aurelia Robotics was founded in 2011 by Marta Silveira, who remains its CEO."
```

Same topic, different surface form — so the question's vector can land a little *off* from
the answer's vector. We already fight this once with **asymmetric roles**: at ingest chunks
are embedded in the `RETRIEVAL_DOCUMENT` role, at query time the question is embedded in
`RETRIEVAL_QUERY` (see `retrieve.py`'s docstring). That helps, but it doesn't erase the
gap. Our Q7 baseline shows the residue precisely: **hit@1 0.896 but hit@3 0.979** — for 8
of 10 near-misses the *right* chunk was sitting at **rank 2**. The information was
retrieved; it just wasn't on top.

**HyDE's idea:** stop embedding the question. Instead, generate a *hypothetical answer* and
embed **that**. A fake answer is answer-*shaped*, so it lands in the same neighborhood as
the real answer chunks — closing the gap from the other side.

> The hypothetical does **not** need to be factually correct. It routinely won't be — the
> model is guessing about *your* private documents. HyDE only needs it to be
> answer-shaped and topically near the truth. Correctness is enforced later, by grounded
> generation (which only ever sees real retrieved chunks and cites them).

## 2. What we built (the design in one screen)

| Decision | Choice | Why (short) |
|---|---|---|
| Transform lives where? | top of `retrieve()`, gated `hyde_enabled` (default OFF) | one seam; OFF = byte-identical to Q7 |
| Embed the hypothetical in which role? | **DOCUMENT** (`as_retrieval_document`) | so it lands in *passage* space — the whole point |
| Which arm gets it? | **semantic only**; lexical keeps the **raw query** | a synthetic passage dilutes BM25 keyword match |
| How many hypotheses (N)? | **N = 1**, temperature 0 | deterministic + cheap on quota |
| On generation failure? | **fail-open** to raw-query embed | HyDE can help, never break, retrieval |
| Which model? | dedicated `hyde_model` | a throwaway passage can use a fast/cheap model |

The gated branch in `retrieve.py`:

```python
if settings.hyde_enabled:
    try:
        hypothetical = await generate_hypothetical(query)          # 1 LLM call, N=1
        query_embedding = (await embed_texts(
            [as_retrieval_document(hypothetical)]))[0]              # DOCUMENT role
    except Exception:
        query_embedding = await embed_query(query)                 # FAIL-OPEN
    timings["hyde_ms"] = _ms(t)
else:
    query_embedding = await embed_query(query)                     # plain Q7 path
```

`generate_hypothetical` (in `app/retrieve/hyde.py`) is a thin call on the **same** shared
google-genai client the embedder and generator use — no new dependency, plain text out (no
JSON schema; we only want a passage to embed).

**Why DOCUMENT role, concretely.** `gemini-embedding-2` encodes the same text differently
per role. Our chunks live in DOCUMENT space. Embedding the hypothetical as a QUERY would
just make it "a longer question" and forfeit the point; as a DOCUMENT it sits *among* the
chunks. This is the single most important line of P4.

**Why the lexical arm keeps the raw query.** The hybrid's two arms are complementary: the
semantic arm is now aimed by an answer-shaped vector, while the lexical/BM25 arm still
matches the user's *literal terms*. That real-terms anchor is not just neutral — it's a
safety rail (see §3).

## 3. The fragilities of HyDE — and how we blunt each

This is the honest part. HyDE trades a known weakness (asymmetry) for a set of new ones. It
is *not* free, and on the wrong corpus it can make retrieval **worse**. The failure modes:

1. **Hallucination steering.** The LLM writes a confident but *wrong* hypothetical and
   drags the search toward the wrong region of the space. This is worst exactly where a
   private RAG lives: **out-of-domain / proprietary / entity-specific** questions where the
   model has no real knowledge and simply fabricates ("Aurelia's Q3 revenue was $4.2M" —
   invented). A fabricated *number* or *name* can pull the vector toward chunks about the
   wrong entity.
2. **False-premise questions.** Ask "Why did Aurelia shut down its Berlin office?" when it
   never had one, and the model will fluently elaborate the false premise — retrieving
   plausible-looking-but-irrelevant chunks that seem to confirm it.
3. **Verbosity dilution.** A long, multi-topic hypothetical averages into one *blurred*
   vector. Instead of sharpening the query's focus, a rambling passage smears it across
   several topics and can retrieve *less* precisely than the bare question.
4. **Added latency, cost, and a new failure surface** on *every* read, before search even
   starts — an LLM round-trip in the hot path that can time out or error.
5. **Non-determinism.** At temperature > 0 the hypothetical (and therefore the retrieved
   set) changes run to run — unrepeatable retrieval, which also poisons eval comparability.

The mitigations, and which ones this build actually applies:

| Mitigation | What it does | This build |
|---|---|---|
| **Keep HyDE as one arm of a hybrid** | the lexical/BM25 arm anchors on the *real* query tokens, so a hallucinated passage can't fully hijack the result | ✅ **applied** — lexical arm keeps the raw query |
| **Fail-open** | any generation error → embed the raw query; core path never breaks | ✅ applied |
| **Temperature 0, N=1** | one deterministic hypothesis → repeatable retrieval + eval | ✅ applied |
| **Cap passage length** (`hyde_max_output_tokens=256`) | short, focused passage → fights verbosity dilution | ✅ applied |
| **Eval-gate per corpus** | HyDE's value is corpus-dependent; ship it only if it beats the baseline *here*, and keep it reversible via the gate | ✅ the P4 gate (see §4) |
| **Blend the hypothetical vector with the raw-query vector** (weighted average) | keeps the search tethered to the literal question, capping how far a bad hypothesis can pull it | 🔒 deferred — documented lever, keeps N=1 simple |
| **N>1 hypotheses + mean-pool** | averaging several samples cancels a single bad hypothesis's variance (the original paper's default) | 🔒 deferred — quota |
| **Query-type routing** | only run HyDE on the short/ambiguous queries it helps; skip keyword-rich ones | 🔒 deferred — future lever |

The through-line: **HyDE is a bet that a plausible fake answer is closer to the truth than
the raw question — and that bet is corpus-dependent.** The hybrid anchor, fail-open, and the
eval gate are what let us take the bet cheaply and back out of it for free.

## 4. How we decide if it stays — the eval gate

P4 is **not kept on faith.** The gate is the cheap retrieval harness (`eval.run`), run with
**rerank ON** (local cross-encoder, zero API cost) against the same golden set, HyDE OFF vs
ON:

```powershell
$env:RERANK_ENABLED="true"; $env:HYDE_ENABLED="false"   # baseline (or trust stored Q7 JSON)
python -m eval.run --golden eval/golden.jsonl --out eval/results/
$env:HYDE_ENABLED="true"                                # HyDE: ~48 gen + 48 embed
python -m eval.run --golden eval/golden.jsonl --out eval/results/
```

**Decision rule (ADR 0005):** HyDE becomes the default *only* if MRR and/or hit@1 improve
beyond noise vs **MRR 0.927 / hit@1 0.896**, with **no regression at hit@3 / hit@10**.
Otherwise it ships gated OFF — a studied, reversible lever — and the ADR records the
negative result.

**Outcome (ran 2026-07-24, rerank ON): no gain → HyDE ships gated OFF.** Baseline and
HyDE ON came back *identical to 16 decimals* — MRR **0.9267**, hit@1 **0.8958**, hit@3
**0.9375**, hit@5/@10 **1.000**. HyDE only reshuffled ranks **6–10**; because hit@5 = 1.0,
every gold chunk was already in the final top 5, and MRR/hit@1/3 depend only on the gold
chunk's rank, so nothing could move.

**The lesson — a reranker can mask a retrieval-stage lever.** HyDE and the cross-encoder
reranker attack the *same* failure: the asymmetry-induced rank-2 near-miss. The reranker
re-scores the whole candidate pool downstream and already promotes that chunk, so under
**rerank ON** it fully absorbs HyDE's effect — the extra per-query LLM call bought nothing
measurable. The experiment that would *isolate* HyDE's raw retrieval effect is the same A/B
with **rerank OFF** (deferred on quota). Takeaway: when two levers target the same defect,
stacking them is redundant — measure each **against the layer that actually decides the
final order**, not in isolation.

## 5. Interactions worth remembering

- **Cache (P3) × HyDE.** The semantic cache keys on the *raw-query* vector and passes it in
  via `query_embedding=`, which short-circuits HyDE. With both ON, HyDE wouldn't fire on a
  cache miss unless the two are reconciled. Both default OFF and the cache is OFF during
  eval, so this is a noted seam concern for the parked cache work, not a P4 problem.
- **Grounding still owns correctness.** Even a wildly wrong hypothetical only changes *which
  real chunks* we retrieve. `generate()` still sees only those real chunks, answers from
  them, cites them, and the P2 citation guard still checks the citations. HyDE is a
  *retrieval* lever; it cannot make the final answer cite something the model wasn't shown.
