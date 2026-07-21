# Q8 — Generation: grounded, cited answer

_Phase 2, milestone Q8. The final read-path stage: `retrieve()` (Q3–Q7) produces a ranked
`list[ScoredChunk]`; `generate()` turns those chunks into an answer drawn only from them, with
citations — or an honest "I don't know"._

## The problem Q8 targets

Everything before this milestone existed to put the right handful of chunks in front of the
model. Q8 is where those chunks become an *answer*. The single risk here has a name:
**hallucination** — the model answering from its own pre-training instead of your documents. On
a RAG system that's the worst failure mode, because a fabricated answer looks exactly as
confident and authoritative as a real one, and it's unaccountable (no source to check).

So the goal of this stage is not "fluent answer" — a raw LLM already does that. It's
**faithfulness**: the answer is *supported by the retrieved context and nothing else*, and when
the context doesn't contain the answer, the model says so rather than guessing.

## The three grounding levers (cheapest first)

`app/generate/generate.py` stacks three mechanisms. None is individually sufficient; together
they make ungrounded answers unlikely.

### 1. Prompt grounding — the system instruction

A **system instruction** is a standing directive given to the model separately from the user's
turn (so the rules can't be buried in, or overridden by, the pasted context). Ours says, in
effect: *answer only from the provided context; if the answer isn't there, say you don't know;
cite the chunk ids you used.*

This is the highest-leverage lever and costs nothing per call — it's just text. It maps
one-to-one onto the three things that can go wrong: inventing facts (→ "only from context"),
guessing when the answer is absent (→ "say you don't know"), and answering without attribution
(→ "cite the chunk ids"). It is also imperfect on its own — a model *can* still stray — which is
why it isn't the only mechanism.

Why "only from context" actually helps: it reframes the task from *"what do you know about
X?"* (which invites the model's parametric memory) to *"what does this text say about X?"* (a
reading-comprehension task over the passages). The second framing is what curbs hallucination.

### 2. Temperature 0

**Temperature** scales the randomness of the model's token sampling: high = more varied and
"creative", low = more deterministic. Grounded extraction wants determinism — reach for the
best-*supported* next token, not a surprising one — so we set `generation_temperature = 0`.
Creativity is a liability here, not a feature: the same question over the same context should
give the same grounded answer every time (this also makes the eval reproducible).

### 3. Structured output

We constrain the model to emit **JSON matching a schema** rather than free prose:
`response_mime_type="application/json"` plus `response_schema=GeneratedAnswer`. `GeneratedAnswer`
is a Pydantic model:

```python
class GeneratedAnswer(BaseModel):
    answer: str
    citations: list[int]   # chunk ids
```

Two payoffs. First, the *same* class is both what we tell the model to fill in and what we
parse the response into (`GeneratedAnswer.model_validate_json(response.text)`) — the model's
output and our type can't drift apart. Second, citations come back as structured data
(`[34, 37]`), not a sentence like "based on chunks 34 and 37" that we'd have to regex out of
prose. The Q10 `/ask` endpoint and the refusal eval both consume that typed field directly.

## Citations are chunk-level (ADR 0002)

`citations` is a list of `chunk.id` ints. Each chunk is rendered in the prompt as
`[chunk {id}]\n{content}`, giving the model a stable handle to refer back to. We picked
chunk-level over finer **char-span** citations (`{chunk_id, start, end}`) because `chunk.id` is
already the citation target `retrieve()` hands us — zero plumbing — whereas char-spans ask the
LLM to emit reliable character offsets (error-prone) and need span-validation code. Spans can be
added later behind the *same* `GeneratedAnswer` model without touching any caller, so there was
no reason to pay for them before chunk-level grounding is even proven.

## The refusal path, and how we measure it

The "I don't know" branch is the anti-hallucination behavior, so we test it explicitly. Two
guards produce a refusal:

- **No context at all** → `generate()` short-circuits *locally*, returning "I don't know" with
  no citations and **no API call** (mirrors the embed stage's empty-input guard — never spend a
  billable call that could only hallucinate).
- **Context present but irrelevant** → the system instruction is what makes the model decline.

To probe the second case we reserved **10 trap questions** (T1–T10 in `golden_questions.md`) —
questions with no fabricable answer in the corpus (e.g. "What color is the Falcon-9X robot?").
`eval/refusal.py` runs each through retrieve → generate and checks for an explicit refusal
phrase (`is_refusal`), reporting an `explicit_refusal_rate`.

### Why that rate is only a *lower bound* (a real lesson from the first run)

The first live run (`gemini-3.1-flash-lite`) scored **8/10** — and both "misses" turned out to
be the *metric's* fault, not the model's. The model actually hallucinated **nothing**:

- **T5 — "Thornfield's football coach?"** The corpus says Thornfield has no football program.
  The model answered *"Thornfield does not have a football program"* and cited that chunk. That
  is the ideal, grounded, non-hallucinated answer — but it contains no "I don't know" phrase, so
  a refusal keyword check scores it as a miss. This is the deep point: **refusing was never the
  real target.** When the corpus supports a *negative fact*, correctly stating it (with a
  citation) is a pass, not a refusal.
- **T8 — "verdict on the appeal?"** The model genuinely refused — *"the text does not state the
  verdict"* — but phrased it "does not state", and the keyword list only had "not stated". A pure
  coverage gap. You can't enumerate every surface form of "I don't know".

So the two failures expose the two ways string-matching leaks: a **concept gap** (T5: a correct
negation isn't a refusal at all) and a **coverage gap** (T8: a real refusal phrased off-list).
The fix here is honesty, not a bigger keyword list: the report calls the number an
`explicit_refusal_rate` **lower bound**, treats `non_refusals` as a **manual review queue** (never
a "hallucination count"), and surfaces those answers to inspect. `is_refusal` is left exactly as
a coarse explicit-refusal detector — its logic unchanged.

The subtlety it *does* pin correctly: refusal is judged on the **answer text**, not on empty
citations — a confident but uncited answer ("Blue.") is a grounding failure, not a clean refusal.

### The real measurement is Q9's job

Separating a correct grounded negation from a fabrication is a *semantic* judgment that string
matching structurally cannot make. That's exactly what an **LLM-as-judge** does, and it's the
whole point of Q9 (RAGAS **faithfulness**: is every claim in the answer supported by the
retrieved context?). Q8's trap check is the cheap regression tripwire; Q9 is the real verdict.

## Why a separate generation client (and a shared one)

Only the *embedding* Gemini client existed before Q8. Generation reuses the **same
`google-genai` SDK and API key** — no new dependency — but calls a different endpoint:
`client.aio.models.generate_content(...)` instead of `embed_content(...)`. To keep exactly one
process-cached client (its httpx connection pool is bound to the event loop that opened it — the
reason `app/worker.py` runs one loop per process), the client factory was promoted into a shared
`app/gemini_client.py`; `embed.py` now imports it under its old `_get_client` name so nothing —
including its immutable tests — changed.

## What Q8 deliberately leaves for later

- **`POST /ask`** (Q10) — the HTTP endpoint that wires retrieve → rerank → generate and logs
  each query. Q8 is just the `generate()` function.
- **RAGAS answer eval** (Q9) — faithfulness / answer relevance / context precision·recall over
  the golden set. Q8 ships only the cheap refusal check.
- **Char-span citations**, streaming, LangGraph orchestration, Guardrails — all later.
