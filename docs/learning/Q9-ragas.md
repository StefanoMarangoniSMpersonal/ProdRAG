# Q9 — RAGAS: judging the *answer* with an LLM

_Phase 2, milestone Q9. The first eval that measures the generation stage. Everything
before this scored retrieval._

Q9 is `eval/produce.py` (phase A: run the pipeline, cache the answers) plus
`eval/ragas_eval.py` (phase B: score those answers with an LLM judge), with
`eval/judge.py` and `eval/throttle.py` supporting them. No code under `app/` changed —
this is measurement, not pipeline.

---

## 1. The problem: we had no honest measure of the answer

By the end of Q8 the read path was complete, but look at what the numbers actually
covered:

| Milestone | What it measured | Against what |
|---|---|---|
| Q4–Q7 | retrieval: hit@k, MRR | hand-labeled `relevant_chunk_ids` |
| Q8 | `is_refusal` keyword match | a list of 20 phrases |

Retrieval was well measured. **Generation was measured by string matching**, and we
already knew that was inadequate — `eval/refusal.py`'s own docstring says so. The live Q8
run scored 8/10 "refusals" while the model actually hallucinated **0/10**. Both misses
were the metric's fault:

- T5 answered "Thornfield does not have a football program" and cited the chunk saying so
  — a *correct grounded negation*, containing no refusal phrase.
- T8 said "does not state the verdict"; the keyword list has "not stated".

No list of phrases can fix this, because the property we care about — *is this claim
supported by the retrieved text?* — is *semantic*. A string match cannot distinguish a
correct denial from a fabrication. That needs a reader.

## 2. The idea: claim decomposition + entailment

**RAGAS** (Retrieval Augmented Generation Assessment; Es et al., EACL 2024) is a library
of published LLM-as-judge metrics. "LLM-as-judge" means using a language model as the
*evaluator* rather than the thing evaluated.

The core move is **not** "ask the LLM to rate this answer 1–5". That's miscalibrated and
prompt-sensitive — models cluster on 4s, and the scale shifts if you reword the prompt.
Instead:

1. **Decompose** the text into atomic statements ("Aurelia was founded in 2011", "its CEO
   is Marta Silveira") — one verifiable claim each.
2. **Entail** each statement against the context: a binary "is this supported? yes/no".
   This is **NLI**, natural language inference — the classic task of deciding whether a
   premise entails a hypothesis.
3. **Score** = supported / total.

Two binary judgments per claim are far more reliable than one holistic 1–5 opinion, and
the score becomes interpretable: 0.75 means literally *three of four claims were
supported*, and the report can name the unsupported one.

Why not BLEU/ROUGE (n-gram overlap with a reference)? They measure surface word overlap,
so they punish a correct paraphrase and reward a fluent hallucination that reuses the
reference's vocabulary. Grounding is not a word-overlap property.

## 3. The RAG triad — why three metrics, not one

A single "quality" number tells you the system is worse without telling you *which stage*
to fix. RAGAS' framing is a triangle over three objects — the question `q`, the retrieved
context `C`, the answer `a` — where each metric scores one **edge**:

```
                 question q
                /          \
   context recall            answer relevancy
              /                \
        context C ── faithfulness ── answer a
```

| Metric | Edge | Blames | Needs a reference? |
|---|---|---|---|
| **Faithfulness** | C ↔ a | generation grounding | no |
| **Answer relevancy** | q ↔ a | the prompt / completeness | no |
| **Context recall** | q,C ↔ reference | retrieval | **yes** |

Each mechanism, concretely:

- **Faithfulness** decomposes the *answer* into claims and checks each against the
  context. Low = the model invented something. This is the metric Q9 exists for.
- **Answer relevancy** works *backwards*: it shows the judge only the answer and asks it
  to reconstruct the question that answer was responding to, `strictness` times, then
  measures cosine similarity between those reverse-engineered questions and the real one.
  A rambling or partial answer reconstructs to a different question and scores low. Note
  it is about *addressing the question*, not about being correct.
- **Context recall** decomposes the **reference** answer and asks whether each of its
  claims is attributable to the retrieved context. Low = retrieval didn't fetch what was
  needed, so no prompt could have saved the answer.

**Context precision is deliberately excluded.** It asks an LLM to guess whether each
retrieved chunk was useful — i.e. it estimates ranking quality, which we already measure
directly with human-approved chunk-id labels (MRR/hit@k from Q4). Paying ~25% of the call
budget for a weaker second opinion on a question already answered is a bad trade.

## 4. The trap that shaped the design: relevancy scores refusals 0

Answer relevancy's prompt explicitly classifies "noncommittal" answers, and the
implementation multiplies the score by `int(not all_noncommittal)` — a refusal scores
**exactly 0**.

For the 10 trap questions, refusing is the *correct* behavior. Scoring them on relevancy
would punish the model precisely for being right, then average that punishment into the
headline number. So traps get **faithfulness only** (`TRAP_METRICS` in `ragas_eval.py`),
and that routing lives in one function, `metrics_for_row`, that the scoring loop, the
budget estimate, and the tests all share.

Two related rules, for the same reason — a number should never be lowered by bookkeeping:

- a golden row with no reference **skips** context recall rather than scoring 0;
- a metric that raises returns **`None`**, excluded from the mean, not 0. A failed judge
  call is *missing data*; recording it as zero would silently understate the pipeline.

## 5. Quota as an architectural constraint

The judge is itself an LLM, so it costs real requests against a free tier of **15/minute,
500/day for generation** (embeddings are metered separately, 1000/day). Measured costs
per question — read off the ragas source, not guessed:

| Metric | Generation calls | Embedding calls |
|---|---|---|
| Faithfulness | 2 (statements, then verdicts) | 0 |
| Context recall | 1 | 0 |
| Answer relevancy | `strictness` | `strictness` + 1 |

Four consequences, all visible in the code:

**Two phases, with a cached answers file.** `produce` and `ragas_eval` are separate
commands. Generation is paid once; re-scoring with different metrics, or recovering from a
crash in the judge, costs zero generation calls. The answers file is also the audit trail
— it stores the exact contexts each score was computed against, so a number stays
traceable months later without re-running anything.

**An explicit rate limiter** (`eval/throttle.py`). Low concurrency is not enough: a serial
loop at ~1.5s/call issues ~40 requests/minute, well over 15. It's a **sliding window** —
at most N grants in any rolling 60s — because that's literally how the quota is phrased,
whereas a token bucket only approximates it. Tests inject a fake clock, so a 60-second
window is verified in milliseconds.

**A pre-flight budget guard.** `estimate_requests` prices the run *before* the first call
and aborts if it exceeds `--max-requests`. Failing while it's still free beats dying at
row 40 having spent the day.

**Checkpointing.** Each scored row is appended to a JSONL as it completes, so a 429 forty
minutes in still leaves everything scored so far on disk.

## 6. Two bugs the live smoke caught (and the fakes could not)

The offline suite drives fake metrics, which proves our routing and aggregation but says
nothing about whether ragas actually talks to Gemini. One live test found two real
defects, which is exactly why it exists.

**(a) `llm_factory` builds a sync client.** For the Google provider ragas calls
`instructor.from_genai(client)` with no async option, so `is_async` is False and every
`await llm.agenerate(...)` raises *"Cannot use agenerate() with a synchronous client."*
Fix: `eval/judge.py` keeps a module-level `_llm_factory` with the same signature that
patches with `use_async=True` and constructs `InstructorLLM` directly.

**(b) `gemini-embedding-2` fuses a batch into ONE vector.** Given a list of strings the
model returns a single fused vector rather than one per input. Ragas then reshapes it to
`(n_questions, -1)` and dies: *"shapes (3,1024) and (3072,1) not aligned"* — 1024 being
3072 fused, divided by 3. This is the **same trap `app/ingest/embed.py` hit at M4**, and
it reached the same conclusion: one input per call. `ThrottledEmbeddings.aembed_texts`
fans out to one request per text.

Note the failure mode of (b): at `strictness=1` the bug is *invisible*, because a one-item
batch can't be fused. It only appears at the default `strictness=3`. A latent bug that
hides at the smallest setting is the kind that surfaces months later.

Fixing (b) changed the cost model — relevancy went from `strictness + 2` to
`2*strictness + 1` requests — which required an authorized revision of the budget-guard
test. Both spec revisions are recorded in the tests themselves, since a test's *reason*
matters more than its assertion.

## 7. What these numbers are NOT

`format_table` prints these caveats next to the scores rather than in a footnote, for the
same reason the Q8 refusal rate got relabelled a lower bound: a number that looks like a
measurement will be treated as one.

- **Self-graded.** The judge defaults to `gemini-3.1-flash-lite` — the same model that
  wrote the answers. Models favor their own output, so faithfulness is biased *upward*.
  This is a known limitation accepted for now because it's the key we have; a different
  judge family would be the first upgrade.
- **Non-deterministic.** Even at temperature 0, claim decomposition varies run to run.
  Over 48 questions, a difference under ~0.05 is noise, not a regression — the same
  small-N reasoning behind the Q4 corpus-expansion gate.
- **An opinion, not ground truth.** A faithfulness score is *one model's claim-level
  entailment ratio*. It is a far better proxy than keyword matching, which is the point of
  Q9 — but it is still a proxy.

## 8. Running it

```bash
# Phase A — run the pipeline, cache the answers (~116 requests, ~8 min)
python -m eval.produce --out eval/results/

# Phase B — judge them (~500 requests at strictness 3, ~34 min)
python -m eval.ragas_eval --answers eval/results/answers-<stamp>.jsonl \
    --strictness 3 --max-requests 520

# Cheap smoke first: validate wiring on 3 questions before spending the day
python -m eval.produce --limit 3 --traps=
python -m eval.ragas_eval --answers <file> --limit 3
```

Both phases share the `--rpm` limiter (default 15). The baseline below was run with
`RERANK_ENABLED=true`, i.e. the full hybrid + cross-encoder pipeline.

## 9. Baseline

Run `2026-07-21`, `RERANK_ENABLED=true`, strictness 3, judge `gemini-3.1-flash-lite`.
Answers `eval/results/answers-20260721T123921_657092Z.jsonl`, report
`eval/results/ragas-20260721T131433_136492Z.json`. 500 judge requests, **zero metric
failures** (n=48 / n=10 on every metric).

| Set | Faithfulness | Answer relevancy | Context recall |
|---|---|---|---|
| golden (48) | **0.971** | **0.930** | **0.958** |
| trap (10) | **0.597** | — | — |

Read those two rows very differently.

**Golden is genuinely strong, and the misses are mostly judge noise.** Only 3/48 answers
scored below 1.0 on faithfulness and only 2/48 below 1.0 on recall. I checked all of them
by hand, and *both* zero-recall rows have **correct answers that match the reference**:

- Q5 ("which robot has the higher max payload") answered *Halcyon, 500 kg vs 450 kg* —
  exactly the reference — with both source chunks retrieved at ranks 0 and 2. Scored
  `context_recall 0.0`.
- Q47 ("higher 2024 revenue and by how much") answered *Aurelia, $55.8M vs $47.6M, +$8.2M*
  — again exactly the reference. Both figures verifiably present in the retrieved context.
  Scored `context_recall 0.0`.

So the headline 0.958 recall *understates* retrieval. But the reason for the understatement
is the finding worth keeping:

**The real defect these rows expose is chunk context loss, not retrieval.** Look at what
the retrieved chunk actually contains:

```
Year Revenue (USD) Net Profit (USD) Employees 2021 18.2 million ... 2024 55.8 million ...
```

A bare grid of numbers. **Nothing in the chunk says "Aurelia Robotics."** The judge is
asked whether the claim *"Aurelia Robotics had 2024 revenue of $55.8M"* is attributable to
that chunk, and strictly speaking it is not — the chunk supports "$55.8M in 2024" for an
unnamed entity. The judge is being pedantic, but it is not wrong.

Two consequences:

1. This is direct evidence for the **enrich stage already deferred in CLAUDE.md** —
   table chunks need their parent document/section identity attached, whether by an
   LLM-written summary or by simply prepending the doc title. A table chunk that loses
   its subject is weakly retrievable *and* weakly verifiable.
2. The generator nonetheless answered both correctly, which means it attributed the two
   tables to the right companies from surrounding chunks and ordering. That worked here,
   but it is inference the context does not strictly license — with two similar tables in
   the window, that is exactly the setup for a confident swap. Worth a targeted test.

**Trap 0.597 is mostly a metric artifact — do NOT read it as a 40% hallucination rate.**
Per-trap faithfulness splits cleanly by *answer shape*, not by correctness:

| Trap answer | Faithfulness |
|---|---|
| bare `I don't know.` (T2, T3, T6, T9) | 0.0 ×3, 1.0 ×1 |
| refusal + grounded explanation (T1, T4, T10) | 0.8 / 0.667 / 0.5 |
| grounded negation (T5, T7, T8) | 1.0 |

**Every trap answer was correct — zero fabrications, same as the Q8 live run.** A bare
"I don't know" decomposes to no supported claims, so faithfulness scores it 0 for
behaving perfectly. That is the *same class of error as the Q8 keyword metric*, pointing
the opposite way — and note which rows it flips: T5 and T8, the two the keyword metric
wrongly marked failures, both score **1.0** here. The two metrics have complementary blind
spots, which is precisely why `eval/refusal.py` stays beside the judge rather than being
replaced by it.

The honest statement of the trap result: **faithfulness over traps measures "did the
refusal come with grounded elaboration", not "did the model refuse".** As a hallucination
metric on refusal-expected questions it is not usable as-is. Fixing it means either
scoring traps on a refusal-specific metric or requiring the prompt to always explain its
refusal — a prompt change, so an architecture decision, not something to slip in here.

**As a regression ruler.** Golden faithfulness/recall sit near ceiling with ~3 rows of
headroom, so they will detect a *large* regression and nothing subtle; answer relevancy
(0.930, genuinely spread) is the most sensitive of the three. Given the ±0.05 noise band
over N=48, treat only relevancy as a fine-grained signal for now.

