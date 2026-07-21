-- 004_query_logs.sql — Phase 2 / Q10: the per-query log.
--
-- Append-only successor to 003 (002/003 are treated as immutable applied history — we
-- add the next-numbered file rather than editing a shipped migration). Applied by
-- apply-migrations.ps1, which re-runs every *.sql on each invocation, so IF NOT EXISTS
-- keeps it idempotent — same contract as 002/003. The test harness (backend/tests/
-- conftest.py) globs the same directory, so this reaches the test schema automatically.
--
-- WHY this table exists: CLAUDE.md's logging rule — "Log every RAG query: user message,
-- retrieved chunk IDs, reranked order, final context, the answer, and token/cost usage.
-- This log is the raw material for evaluation." /ask also emits a structured stdout line
-- for live tailing; this table is the durable, QUERYABLE half — you can ask "which
-- queries retrieved chunk 42?" or "what did we spend last week?" in SQL, which a log
-- stream can't answer.

CREATE TABLE IF NOT EXISTS query_logs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Tenancy/RLS seam, exactly as on documents/chunks: the column exists from day one
    -- (single-tenant today = DEV_OWNER_ID), so turning on per-user Row-Level Security
    -- later is a policy change, never a backfill.
    owner_id            uuid NOT NULL,
    query               text NOT NULL,
    answer              text NOT NULL,
    -- Chunk ids, as ordered arrays (bigint[] matches chunks.id). Arrays, not JSONB:
    -- these are homogeneous, ordered id lists — ORDER IS THE DATA here (rank 1 vs rank
    -- 4) — and Postgres keeps them typed and indexable.
    citations           bigint[] NOT NULL DEFAULT '{}',  -- what the ANSWER cited
    retrieved_chunk_ids bigint[] NOT NULL DEFAULT '{}',  -- pre-rerank (fused pool order)
    final_chunk_ids     bigint[] NOT NULL DEFAULT '{}',  -- post-rerank = context sent
    -- We store the context's chunk IDS + size, not its TEXT. Chunk content is
    -- reconstructible by joining `chunks`, so copying it here would duplicate the whole
    -- corpus once per query. Trade-off: if a document is deleted, this log can no longer
    -- reproduce the exact prompt. Reversible — add a `context_text` column later if that
    -- reproducibility ever matters more than the storage.
    context_chars       integer,
    -- Token counts, NOT dollars: prices change per model and over time, so a stored cost
    -- would rot. Cost is derived at reporting time from these plus generation_model.
    prompt_tokens       integer,
    completion_tokens   integer,
    total_tokens        integer,
    generation_model    text,
    -- Per-stage wall times (embed/semantic/lexical/fuse/rerank/generate/total). JSONB
    -- here precisely because the stage set CHANGES as the pipeline evolves — a schema
    -- migration per new stage would be the wrong shape for this one.
    timings_ms          jsonb NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- The access pattern this table actually gets: "my most recent queries" (a dashboard, a
-- debugging session, an eval export). owner_id first, created_at DESC second, so the
-- index serves the filter and the ordering in one scan.
CREATE INDEX IF NOT EXISTS idx_query_logs_owner_created
    ON query_logs (owner_id, created_at DESC);
