-- Runs once, on first initialization of an empty Postgres data directory
-- (via docker-entrypoint-initdb.d). Enables the pgvector extension so the
-- `vector` type and HNSW index are available for later phases.
CREATE EXTENSION IF NOT EXISTS vector;
