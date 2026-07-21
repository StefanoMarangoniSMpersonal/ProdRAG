---
name: eval-run
description: >
  Run the RAG evaluation harness against the golden question/answer set and report how
  quality changed. Use this skill whenever I want to measure retrieval or answer quality,
  after ANY change to chunking, embeddings, retrieval, reranking, prompts, or the model —
  and whenever I say "did that help", "run the eval", "check quality", "did we regress",
  or compare two approaches. Treat evaluation as mandatory after core-pipeline changes,
  even if I forget to ask.
---

# Eval Run

Measure whether a change to the RAG pipeline actually improved things, using the golden
set in `/eval/golden.jsonl` and the RAGAS harness in `/eval`.

## Procedure

1. **Confirm the baseline exists.** Look for the most recent saved eval result under
   `/eval/results/` (`retrieval-*.json` for Q4 retrieval, `ragas-*.json` for Q9 answer
   quality). If none exists, this run establishes the baseline — say so.
2. **Run the harness.** Which one depends on what changed:

   **Retrieval** (chunking, embeddings, search, fusion, rerank) — cheap, ~1 API call
   per question:
   ```bash
   python -m eval.run --golden eval/golden.jsonl --out eval/results/
   ```
   Reports hit@{1,3,5,10} and MRR against hand-labeled `relevant_chunk_ids`.

   **Answer quality** (prompt, generation model, or any retrieval change whose effect on
   the *answer* matters) — the Q9 LLM judge, in TWO phases:
   ```bash
   # A: run the pipeline and cache answers (~116 requests for 58 questions)
   python -m eval.produce --out eval/results/
   # B: judge those cached answers (~500 requests at strictness 3)
   python -m eval.ragas_eval --answers eval/results/answers-<stamp>.jsonl \
       --strictness 3 --max-requests 520
   ```
   Reports faithfulness (grounding — blames generation), answer relevancy (blames the
   prompt), and context recall (blames retrieval). Context *precision* is deliberately
   not measured: MRR/hit@k already covers ranking with human labels.

   **Budget first.** Phase B is expensive against the free tier (15 req/min; 500/day
   generation, 1000/day embedding). Always smoke with `--limit 3` before a full run, and
   never re-run phase A just to re-score — phase B reads the cached answers file.
3. **Compare to baseline.** Show a small before/after table of each metric with the delta.
   Flag any metric that regressed, even if the headline number improved. For RAGAS
   metrics, treat any delta under ~0.05 over 48 questions as **noise, not signal** — the
   judge is non-deterministic even at temperature 0.
4. **Interpret, don't just report.** For each notable change, say what it implies about
   *which stage* moved the needle — e.g. "faithfulness up but context recall flat →
   the generation prompt improved, retrieval did not." Point me to the likely next lever.
5. **Surface failures.** List 2–3 specific golden questions that scored worst, with the
   retrieved chunks, so I can eyeball what went wrong. Qualitative review of real failures
   matters as much as the aggregate numbers.

## Notes

- Do **not** modify the pipeline as part of running eval — measure only. Fixes are a
  separate, explicit step.
- If the golden set is very small (<15 items), remind me the numbers are noisy and worth
  expanding before trusting a small delta.
- Keep each result file so future runs always have a baseline to compare against.
- **Report RAGAS scores honestly.** They come from an LLM judge that defaults to the same
  model that wrote the answers (self-graded, biased upward) and are non-deterministic. A
  faithfulness score is one model's claim-level entailment ratio, not ground truth. Never
  present it as a measured fact — `format_table` already prints these caveats; don't strip
  them when summarizing.
- The cheap keyword check `python -m eval.refusal` still exists and is still only an
  **explicit-refusal lower bound**. Use it for fast regression checks; use RAGAS
  faithfulness for the real fabricated-vs-grounded judgment.
