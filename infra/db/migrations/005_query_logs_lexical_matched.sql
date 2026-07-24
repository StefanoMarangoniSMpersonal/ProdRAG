-- 005_query_logs_lexical_matched.sql — pre-Phase-3: record whether lexical retrieval fired.
--
-- Append-only successor to 004 (002–004 are immutable applied history — we add the
-- next-numbered file rather than editing a shipped migration). Applied by
-- apply-migrations.ps1, which re-runs every *.sql on each invocation, so ADD COLUMN
-- IF NOT EXISTS keeps it idempotent. The test harness (backend/tests/conftest.py) globs
-- the same directory, so this reaches the test schema automatically.
--
-- WHY: /ask fuses a semantic (vector) and a lexical (full-text) arm. We want to know, per
-- query, WHETHER the lexical arm contributed at all — just a boolean, not the id list — so
-- we can ask in SQL "how often does full-text actually fire?" (e.g. to justify the arm, or
-- spot corpora where it never matches). A dedicated boolean column, not a JSONB field:
-- it's a first-class, low-cardinality, aggregatable fact, so it earns a typed, indexable
-- column of its own rather than riding inside timings_ms the way cache_hit does.
--
-- Nullable, no default: NULL means "not recorded" — a cache hit answers WITHOUT retrieving,
-- so there is no lexical signal to record. A miss records a real true/false. Adding a
-- nullable column with no default is a metadata-only change (no table rewrite), and it
-- touches only query_logs — NOT chunks — so it does not churn chunk ids and the eval
-- golden set stays valid with no re-ingest.

ALTER TABLE query_logs
    ADD COLUMN IF NOT EXISTS lexical_matched boolean;
