# Q4 — Retrieval eval baseline (hit@k / MRR)

> Teaching note for the milestone that turns "the retrieval feels okay" into a number.
> Companion to the code in `eval/` and the spec in `backend/tests/test_eval_metrics.py`.

## Why this milestone exists

Q1–Q3 built a retriever. Q5–Q7 will try to make it *better* — lexical search, RRF fusion,
a cross-encoder reranker. Every one of those adds latency and complexity, and every one is
sold as "this improves quality." **Without a measurement you cannot tell an improvement
from a regression dressed up as one.** Q4 builds the ruler. It records the semantic-only
**baseline** so each later stage has a number to beat, and the "did that help?" question has
an answer instead of a vibe.

## The two metrics

Both compare the **ranked list** `retrieve()` returns against a **ground truth**: for each
question, the set of chunk ids that actually contain its answer (the "gold", built by
`build_golden.py`).

- **hit@k** ("hit at k") — did *at least one* relevant chunk land in the top *k*? A yes/no
  per question. Averaged over all questions it becomes **hit-rate@k**, a fraction. hit@3 =
  "was a right chunk in the top 3." It rewards *finding* the answer within a budget of *k*
  chunks (which matters because the generator can only read so many).
- **MRR (Mean Reciprocal Rank)** — for each question take the rank of the *first* relevant
  chunk (rank 1, 2, 3…), invert it (1/1, 1/2, 1/3…), and average across questions. It
  rewards putting the right chunk *high*, not merely somewhere in the top-k. First-hit at
  rank 1 → 1.0; at rank 5 → 0.2; never → 0.

Why both: hit-rate@k answers "is the answer retrievable at all within k?"; MRR answers "how
*near the top* is it?". A change can lift MRR (better ordering) without changing hit@10 (same
chunks, reshuffled) — you want to see both.

The metric functions (`eval/metrics.py`) are deliberately **pure** — ranked ids + relevant
set in, a number out, no DB/network/clock. That is why they can be pinned with hand-built
rankings in the test, and why you can *trust* them: the ruler itself has no moving parts.

## The trap that shaped the design: chunk ids churn

Relevance is recorded as `chunk_id`s in `eval/golden.jsonl`. But a chunk's id is a
Postgres `BIGINT IDENTITY` — an auto-incrementing number assigned at INSERT. A re-ingest is
delete-then-insert, so **every re-ingest hands out fresh ids.** A hand-typed golden file
would silently point at the wrong (or non-existent) chunks the next time you load the corpus,
and every metric would be computed against garbage *with no error*.

So the id mapping is a **derived artifact**: `eval/build_golden.py` reads the expected
answers from `golden_questions.md`, finds the chunk(s) whose text contains each answer, and
writes `golden.jsonl` for human review. Re-ingest → re-run the builder. (Alternatives
considered and rejected: hand-typed ids — the silent-failure trap above; substring-only, no
ids — re-ingest-proof but conflates "retrieved the right chunk" with "some chunk contains
this string" and breaks on paraphrase.)

**Source-scoped matching (why the golden set carries a Source File column).** A bare
substring match has a subtler failure than paraphrase: the *same string in the wrong
document*. "Marta Silveira" is Aurelia's CEO, but if an unrelated corpus doc also mentions a
"Marta Silveira," a naive match would credit that chunk too — a false positive that silently
inflates the metric. `golden_questions.md` tags every question with its **Source File**, and
`match_chunks` only matches within `<source_file>.md`. The scope gate is what makes a
substring match trustworthy enough to seed a draft. (`build_golden.py` also flags three
review cases: *source missing* — the named file isn't ingested; *0 matches* — paraphrased,
add ids by hand; *>1 match* — confirm each is genuinely relevant.)

## Why 15 questions / 13 chunks was a *smoke test*, not a trustworthy baseline (and what we did about it)

The harness was first built against the original corpus — **15 questions / 13 chunks** — as a
plumbing check. That is too small to trust a *delta*, for two independent reasons that are
easy to conflate.

**Question count drives statistical noise.** hit-rate@k is a proportion, so its uncertainty
is the standard error `√(p(1−p)/N)`, where N is the number of *questions*. At N=15 and
p≈0.8, SE ≈ 0.10 → a 95% confidence interval of roughly **±20 points**. The gains Q5–Q7
produce are typically **5–15 points** — *inside that noise band.* You literally cannot tell
whether the reranker helped or you got lucky. Halving the interval takes 4× the questions
(the √N law): ~50 questions → ±11 pts, ~100 → ±8 pts.

**Chunk count drives whether the task is even hard** (the *ceiling effect*). With 13 chunks
and k=10, `retrieve()` returns 10 of the 13 that exist — so hit@10 is ≈100% no matter how
bad ranking is. For ranking to be stressed, k must be a small fraction of the corpus (with
k up to 10, you want at least a few hundred chunks). The current corpus is *excellent* on
the other axis — adversarial distractors (Aurelia vs Halcyon vs Cedarwood, swapped
CEOs/prices/incidents) — it is just far too small a haystack.

**Conclusion, recorded as a gate — now acted on.** The harness code is identical at any size,
so it was built and smoke-checked against 15/13 first. The gate (in `docs/PHASE2-PLAN.md`)
then required expanding **before** trusting any Q5–Q7 delta, and that expansion is done: the
golden set now holds **48 questions over 9 documents** (8 new adversarial fictional-company
docs under `eval/corpus/` — Thornfield University, Lakeview Medical Center, NovaBridge, Port
Kessler, Valdoria, … — plus the original `rag_test_document.md`). At N=48 the 95% CI tightens
to ≈±11–14 pts, and 9 docs' worth of chunks give k=10 a real haystack to rank within. The
golden Q&A stays un-ingested (it's the answer key); re-run `build_golden.py` after any
re-ingest.

## How to run it

```bash
# 1. (re)build the golden id mapping — needs the dev DB up + GEMINI_API_KEY set
python -m eval.build_golden        # writes eval/golden.jsonl + a review report; eyeball it

# 2. record a baseline — real embed + real pgvector, results saved under eval/results/
python -m eval.run --golden eval/golden.jsonl --out eval/results/
```

Both run from the repo root; `eval/run.py` puts `backend/` on the path and loads
`backend/.env` itself, so the app imports and the API key resolve regardless of cwd. The
offline unit tests (`backend/tests/test_eval_metrics.py`) monkeypatch `retrieve` and never
touch Postgres or Gemini; the one live end-to-end test is `@pytest.mark.live` (deselected by
default, run with `-m live`).
