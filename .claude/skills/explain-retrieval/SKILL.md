---
name: explain-retrieval
description: >
  Trace a single query through the retrieval pipeline and show every stage's output so I
  can see exactly what got retrieved and why. Use this whenever I want to understand or
  teach myself the retrieval flow, inspect what context an answer was built from, or say
  "what did it retrieve", "show me the retrieval", "walk me through retrieval for this
  query", or "why these chunks". This is a learning/inspection skill — favor showing the
  full pipeline over giving a short answer.
---

# Explain Retrieval

Make the retrieval pipeline legible for one query. This is the window into the part of RAG
that's hardest to reason about blind, and the main tool for learning why retrieval behaves
as it does. Read-only — never modifies the pipeline.

## Procedure

Given a query, run the real retrieval code and display, stage by stage:

1. **Query processing** — any rewriting/expansion applied, and the final query embedded.
   Confirm the query embedding uses the same model (and asymmetric query mode, if any) as
   the documents.
2. **Semantic candidates** — top-N from pgvector with their distances/scores.
3. **Lexical candidates** — top-N from Postgres full-text / BM25.
4. **Fusion** — the Reciprocal Rank Fusion result, showing how the two lists combined and
   which items moved.
5. **Rerank** — the cross-encoder's reordering and scores; highlight anything the reranker
   promoted or demoted sharply.
6. **Final context** — the 5–10 chunks actually packed into the Gemini prompt, in order,
   with their source citations.

Then give a one-paragraph plain-English read: is this retrieval healthy for this query?
Are the top chunks actually relevant? Is anything obviously missing that BM25 or a
different chunking would have caught?

## Notes

- Read-only. If I then want to change something, that's a separate explicit step.
- Pair naturally with `retrieval-debugger` (the subagent) when retrieval is *wrong* and
  needs diagnosis rather than just display.
- Prefer completeness here over brevity — the point is for me to see the whole flow.
