---
name: ingest-inspect
description: >
  Run a single document through the ingestion pipeline (parse → chunk → embed metadata)
  and print the resulting chunks so I can eyeball chunking quality WITHOUT writing to the
  main store. Use this whenever I'm working on parsing or chunking, want to see how a
  document gets split, am tuning chunk size/overlap, or say "show me the chunks", "how did
  this get chunked", "inspect ingestion", or "why is this document being split like that".
---

# Ingest Inspect

Chunking quality is upstream of all retrieval quality, and it's easy to get wrong on
real documents (tables, multi-column layouts, lists, code). This skill makes the chunker's
behavior visible so I can judge it by eye before committing a strategy.

## Procedure

1. **Take the target document** (a path I give you, or ask which one). Run it through the
   real parsing + chunking code — do **not** reimplement chunking here, call the project's
   own pipeline in a dry-run / inspect mode.
2. **Print, for each chunk:** its index, token/char length, the source location (page /
   section if available), the metadata attached, and the chunk text (truncate very long
   ones but make boundaries obvious).
3. **Flag likely problems automatically:**
   - a chunk that splits mid-table, mid-list, or mid-code block,
   - chunks far outside the target size (too small = fragmented, too big = imprecise),
   - overlap that's missing or excessive,
   - boilerplate/nav text that should have been stripped,
   - lost structure (headings detached from their content).
4. **Summarize:** chunk count, size distribution (min/median/max), and a one-line verdict
   on whether the current strategy looks healthy for this document type.

## Notes

- This is a **dry run** — do not write chunks or embeddings into the production tables.
- If parsing (Unstructured) produced garbled output for a complex layout, say so plainly;
  that's a parsing problem, not a chunking one, and points at the document-processing step.
