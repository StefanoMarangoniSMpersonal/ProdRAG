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
   `/eval/results/`. If none exists, this run establishes the baseline — say so.
2. **Run the harness** against `/eval/golden.jsonl`:
   ```bash
   python -m eval.run --golden eval/golden.jsonl --out eval/results/
   ```
   Report metrics including at least: context precision / recall (did we retrieve the
   right chunks), faithfulness (is the answer grounded in the context), and answer
   relevance. Include retrieval-only metrics (e.g. hit@k, MRR) if the harness produces them.
3. **Compare to baseline.** Show a small before/after table of each metric with the delta.
   Flag any metric that regressed, even if the headline number improved.
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
