-- 002_schema.sql — ingestion schema (documents + chunks)
--
-- Applied against the RUNNING Postgres (see infra/db/migrations/apply-migrations.ps1),
-- NOT via infra/db/init/ — that dir only runs once on a fresh volume, so keeping the
-- evolving schema here means we never have to wipe pgdata to change it.
--
-- Option A (see plan/CLAUDE.md): this SQL is the source of truth for the schema;
-- app/models.py mirrors it by hand (no Alembic yet). Keep the two in sync.
--
-- Idempotent: safe to re-run (IF NOT EXISTS everywhere).

CREATE EXTENSION IF NOT EXISTS vector;  -- normally enabled by init/001; kept here so 002 is self-sufficient

-- documents: one row per uploaded file; system of record for ingestion status.
CREATE TABLE IF NOT EXISTS documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),           -- exposed in API/citations
    owner_id     uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',  -- dev user until auth; RLS later
    filename     text NOT NULL,
    content_type text,
    byte_size    integer,
    checksum     text,                                                 -- sha256 of raw bytes (dedup / skip-if-unchanged later)
    source_uri   text NOT NULL,                                        -- local path now, s3://... later
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','processing','ready','failed')),
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- chunks: retrieval-sized pieces of a document, each with its 768-dim embedding.
CREATE TABLE IF NOT EXISTS chunks (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,     -- internal, high-volume: bigint for insert locality
    document_id   uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    owner_id      uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    ordinal       integer NOT NULL,                                    -- 0-based position within the document
    content       text NOT NULL,                                       -- raw chunk text (rerank, LLM context, tsvector later)
    embedding     vector(768) NOT NULL,                                -- gemini-embedding-2 @ 768 dims
    char_count    integer NOT NULL,
    token_count   integer,
    element_type  text,                                                -- Unstructured type: Title/NarrativeText/Table/...
    section_title text,                                                -- promoted heading (citations + retrieval context)
    page_number   integer,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,                   -- catch-all for source-specific extras
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, ordinal)
);

-- Cheap, useful non-vector indexes (create now).
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_documents_owner_id ON documents(owner_id);
CREATE INDEX IF NOT EXISTS idx_chunks_owner_id    ON chunks(owner_id);

-- DEFERRED until after the first rows land (building an ANN index on an empty table buys nothing):
--   CREATE INDEX idx_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
-- DEFERRED to Phase 2 (keyword search; derived from content, so no re-ingest needed):
--   ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
--   CREATE INDEX idx_chunks_tsv ON chunks USING gin (tsv);
