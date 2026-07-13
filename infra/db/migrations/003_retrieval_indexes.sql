-- 003_retrieval_indexes.sql — Phase 2 / Q1: switch on the retrieval indexes.
--
-- Append-only successor to 002_schema.sql (which is treated as immutable applied
-- history — we add the next-numbered file rather than editing a shipped migration, so
-- the habit already holds when this naive runner is swapped for Alembic/a ledger runner
-- that CHECKSUMS applied migrations and refuses edits). Applied by apply-migrations.ps1,
-- which re-runs every *.sql on each invocation, so IF NOT EXISTS everywhere keeps it
-- idempotent — same contract as 002.
--
-- Both objects derive from data already stored in `chunks` (embedding, content), so this
-- needs NO re-ingest: the existing rows gain the index / generated column in place.

-- Semantic search substrate (Q2). HNSW = Hierarchical Navigable Small World, an
-- approximate-nearest-neighbour index: sub-linear vector search instead of a full scan.
-- `vector_cosine_ops` is the cosine-distance opclass — correct because embeddings are
-- L2-normalized at ingest, so cosine is the matching metric (cosine ≡ inner product on
-- unit vectors). Deferred until now on purpose: an ANN index on an empty table buys
-- nothing; it's built once real rows exist.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Lexical search substrate (Q5). `tsv` is a GENERATED ... STORED column: Postgres keeps
-- it in sync with `content` automatically (to_tsvector normalizes → lowercase, stops,
-- stems), so the app never writes it. STORED = materialized on disk at insert/update.
-- ADD COLUMN IF NOT EXISTS mirrors the documents.attempt upgrade pattern in 002.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

-- GIN (Generalized Inverted Index) over the tsvector — the right index for full-text
-- match (@@): faster reads than GiST for a mostly-static search column, at higher build
-- cost we don't care about here.
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (tsv);
