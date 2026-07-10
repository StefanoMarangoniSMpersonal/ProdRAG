-- ProdRAG — dev DB exploration queries (read-only; safe to run anytime)
--
-- Open this file in VS Code with the SQLTools + PostgreSQL extensions installed,
-- connect to "ProdRAG dev (local Postgres)", then run a statement with the inline
-- ▶ "Run on active connection" (or Ctrl+E, Ctrl+E). Each query below is a self-
-- contained way to see what the ingestion pipeline has written.
--
-- Connection (all dev defaults, non-secret): localhost:5432 / prodrag / prodrag / prodrag.

-- 1. What tables exist, and how much is in them.
SELECT 'documents' AS table, count(*) FROM documents
UNION ALL
SELECT 'chunks', count(*) FROM chunks;

-- 2. Every ingested document + its lifecycle status. `status` walks
--    pending -> processing -> ready | failed (see app/ingest/orchestrate.py).
SELECT id, filename, status, content_type, byte_size, source_uri, created_at
FROM documents
ORDER BY created_at DESC;

-- 3. The chunk view — the retrievable units. `element_type` is the Unstructured
--    class (CompositeElement = prose, Table/TableChunk = an isolated table);
--    `dims` confirms every embedding is the locked 768-dim vector.
SELECT
    ordinal,
    element_type,
    char_count,
    vector_dims(embedding) AS dims,
    left(section_title, 24)                          AS section_title,
    left(replace(content, E'\n', ' '), 60)           AS content_preview
FROM chunks
ORDER BY document_id, ordinal;

-- 4. Table chunks keep their grid as HTML in the `metadata` JSONB (the
--    infer_table_structure decision) — this is what a future rerank/citation step
--    or the deferred table-summary enrich stage will read.
--    (uses ->> rather than the JSONB `?` operator, which SQLTools would mistake
--    for a bind-parameter placeholder and prompt you for a value.)
SELECT ordinal, jsonb_pretty(metadata) AS metadata
FROM chunks
WHERE metadata ->> 'text_as_html' IS NOT NULL
ORDER BY ordinal;

-- 5. Peek at an actual embedding vector (the full 768 numbers are noisy, so just
--    the first few). Proves the vectors are real, not placeholders.
SELECT ordinal, substring(embedding::text for 60) || ' ...' AS embedding_head
FROM chunks
ORDER BY ordinal
LIMIT 5;

-- 6. Join chunks back to their document (how retrieval will assemble context +
--    citations: a chunk always knows which document and section it came from).
SELECT d.filename, c.ordinal, c.section_title, c.char_count
FROM chunks c
JOIN documents d ON d.id = c.document_id
ORDER BY d.filename, c.ordinal;
