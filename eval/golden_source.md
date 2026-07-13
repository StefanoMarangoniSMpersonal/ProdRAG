# Golden Question Set — source (Aurelia / Halcyon / Cedarwood & Finch corpus)

> **Provenance & why this file exists.** This Q&A set was originally embedded at the end of
> `backend/tests/fixtures/rag_test_document.md` (the eval corpus). It was **extracted out** on
> 2026-07-14 because leaving the answer key inside the ingested corpus is test-set leakage — a
> retrieval/RAGAS eval would trivially "win" by retrieving its own oracle. This file is the
> human-readable oracle; it is **never ingested** into the store.
>
> This is *raw source material*, not the machine-consumed golden set. Milestone **Q4** turns it into
> `eval/golden.jsonl` (each question → the relevant `chunk_id`s of the ingested clean corpus, for
> hit@k / MRR). The **Expected Answer** column becomes the reference answer for RAGAS at **Q9**.
> The corpus these questions are asked over is sections 1–18 of `rag_test_document.md`.

Use these to test single-fact retrieval, cross-document disambiguation (Aurelia vs. Halcyon vs. Cedarwood & Finch), and negative/absence handling.

| # | Question | Expected Answer |
|---|----------|------------------|
| 1 | Who is the CEO of Aurelia Robotics? | Marta Silveira |
| 2 | Who is the CEO of Halcyon Dynamics? | Jasper Van Doorn |
| 3 | Which company is a furniture maker rather than a robotics company? | Cedarwood & Finch |
| 4 | What is the price of the Sparrow Mini? | $9,200 per unit |
| 5 | What is the price of Halcyon Dynamics' comparable compact robot, the Wren Compact? | $8,700 per unit |
| 6 | Why was the Kestrel line discontinued? | Due to a critical hinge defect found in 1,200 units |
| 7 | Why was the Fernhollow Bookshelf discontinued? | Because it warped in humid climates |
| 8 | What caused the 2022 incident at Aurelia Robotics' Rotterdam client site? | A faulty firmware update (version 3.2.1) that caused robots to collide with shelving |
| 9 | What caused the 2023 incident at Halcyon Dynamics' Lyon client site? | A firmware update (version 2.4.0) that caused sensor drift and minor pallet collisions |
| 10 | What was Aurelia Robotics' net profit in 2023? | $6.9 million |
| 11 | What was Halcyon Dynamics' net profit in 2023? | $5.0 million |
| 12 | Has Halcyon Dynamics ever discontinued a product line? | No, it has never discontinued a product line since founding |
| 13 | Has Cedarwood & Finch ever raised venture funding? | No, it has been fully self-financed since 2009 |
| 14 | Who founded Cedarwood & Finch? | Owen Bramblett and his sister, Clara Bramblett |
| 15 | What material is the Wickham Rocking Chair made from? | Black walnut |

### Suggested "trap" / absence-testing questions (not directly answerable — good for testing hallucination resistance)

- "Who is the CTO of Cedarwood & Finch?" → Expected: No such role/person mentioned in the document (Cedarwood & Finch has no CTO listed).
- "What year was Halcyon Dynamics' Series C round?" → Expected: Not mentioned; only a Series B round (2021) is described.
- "What color is the Falcon-9X robot?" → Expected: Not specified in the document.
