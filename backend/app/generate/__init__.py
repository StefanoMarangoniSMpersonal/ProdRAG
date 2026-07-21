"""Generation stage (Q8): retrieved chunks -> grounded, cited answer via Gemini.

The read side's final stage. `retrieve()` (Q3–Q7) produces a ranked `list[ScoredChunk]`;
`generate()` turns that context into an answer that is drawn ONLY from the context,
refuses ("I don't know") when the context lacks the answer, and cites the chunk ids it
used. Mirrors the `app/ingest/` package shape: one module per stage, module-level seams,
lazy heavy imports.
"""
