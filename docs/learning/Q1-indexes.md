# Q1 — Retrieval indexes: HNSW (vector) + `tsvector` full-text

_Phase 2, milestone Q1. What we turned on, why these shapes, and the gotchas._

Q1 adds two database capabilities that the rest of Phase 2 reads, both **derived from data
already stored** in `chunks` (`embedding`, `content`) — so they applied to the existing
32 rows **in place, with no re-ingest**:

1. an **HNSW** index on `chunks.embedding` (semantic search, Q2);
2. a generated **`tsv tsvector`** column + a **GIN** index on it (lexical search, Q5).

Migration: `infra/db/migrations/003_retrieval_indexes.sql` (append-only successor to `002`).

---

## 1. ANN vs exact search — why HNSW

**Exact nearest-neighbour** search compares the query vector to *every* row and sorts —
O(n) per query. Correct, but linear in corpus size: fine at 32 rows, ruinous at millions.

**ANN (Approximate Nearest Neighbour)** trades a sliver of recall for sub-linear latency.
**HNSW = Hierarchical Navigable Small World**: a multi-layer graph where each node links to
a few near neighbours. Search enters at a sparse top layer, greedily hops toward the query,
and descends into denser layers — visiting a tiny fraction of nodes instead of all of them.
"Approximate" because greedy graph-walking can miss a true nearest neighbour; in practice
recall is very high and tunable.

We chose HNSW over pgvector's other index, **IVFFlat** (which buckets vectors into lists via
k-means and scans the nearest few buckets). HNSW gives better recall/latency and — crucially
— **doesn't need training data to build**, so it works on a small or growing corpus. IVFFlat's
bucket quality depends on having a representative sample at build time. (Locked in `CLAUDE.md`.)

### The three HNSW knobs

- **`m`** (build-time, default 16) — links per node. Higher = better recall, larger index,
  slower build.
- **`ef_construction`** (build-time, default 64) — candidate-list size while building. Higher
  = better-quality graph, slower build.
- **`ef_search`** (query-time, default 40) — candidate-list size while searching. Higher =
  better recall, slower query. This is the **recall dial you turn per query** without
  rebuilding — set per session with `SET hnsw.ef_search = N`. Q2 owns tuning it.

We took the defaults for `m`/`ef_construction` — the corpus is tiny and eval hasn't asked for
more. The migration line is deliberately bare (`USING hnsw (embedding vector_cosine_ops)`).

### Why cosine — and why it's "free" here

`vector_cosine_ops` picks the **cosine-distance** operator (`<=>`). Cosine measures the *angle*
between vectors, ignoring magnitude. That's the right metric because our embeddings are
**L2-normalized at ingest** (unit length). And on unit vectors, **cosine similarity ≡ inner
product** (cos θ = a·b when |a|=|b|=1), so cosine distance = 1 − a·b — the ordering is identical
to what inner product would give, and we don't pay to re-normalize at query time. The opclass
in the index **must match** the operator the query uses, or Postgres won't use the index.

### Proof it's wired (EXPLAIN spot-check)

On the 32-row dev table the planner **correctly prefers a sequential scan** — descending an
HNSW graph isn't worth it at that size:

```
Limit
  ->  Sort  (Sort Key: (embedding <=> '[...]'::vector))
        ->  Seq Scan on chunks
```

Force the planner's hand (`SET enable_seqscan = off`) and the HNSW index *is* valid and chosen:

```
Limit
  ->  Index Scan using idx_chunks_embedding_hnsw on chunks
        Order By: (embedding <=> '[...]'::vector)
```

So the index works; the planner will switch to it naturally as the corpus grows. This is
exactly why the Q1 test asserts index **presence** (via `pg_indexes`), not index **usage** —
a usage assertion would flap on a small table.

---

## 2. Full-text: the generated `tsv` column

```sql
ALTER TABLE chunks ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
```

- **`tsvector`** is Postgres's preprocessed full-text type: `content` run through the
  `'english'` config → lowercased, stopwords dropped ("the", "a", "of"), words stemmed
  ("running" → "run"), stored as sorted lexemes with positions. A query (`tsquery`) matches
  against *this*, not the raw text. (Sample from a real chunk: `'augment':16 'autonom':60` —
  note "autonomous" stemmed to `autonom`.)
- **`GENERATED ALWAYS AS (...) STORED`** — a *generated column*. Postgres computes it from
  `content` automatically and keeps it in sync; the app **never writes it**. `STORED` =
  materialized on disk at insert/update (Postgres doesn't support `VIRTUAL` for this). This is
  why it needed no re-ingest: it derives from `content`, which was already there, and populated
  itself for all 32 existing rows the moment the column was added.

On the ORM side (`app/models.py`) it's mapped read-only so the model stays a complete mirror of
the schema (the project's "Option A" convention):

```python
tsv: Mapped[str] = mapped_column(
    TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
)
```

`Computed(..., persisted=True)` is SQLAlchemy's word for `GENERATED … STORED` — it tells the ORM
the DB owns the value, so `tsv` is never sent on INSERT/UPDATE and `write_chunks` (M5) is untouched.

### GIN vs GiST for the index

```sql
CREATE INDEX idx_chunks_tsv ON chunks USING gin (tsv);
```

Two index types can serve `tsvector @@ tsquery`:

- **GIN** (Generalized Inverted Index) — an inverted index (lexeme → list of rows). **Faster
  lookups**, slower to build/update, larger. Right for a mostly-static search column read far
  more than it's written — exactly our case.
- **GiST** — lossy, smaller, faster to update, but can produce false-positive candidates that
  need rechecking. Better when the column churns constantly.

We read chunks far more than we rewrite them, so **GIN**.

### The BM25 caveat (why this is "BM25-ish", not BM25)

Postgres full-text ranking (`ts_rank` / `ts_rank_cd`, used in Q5) is **not true Okapi BM25**. It
scores by term frequency and proximity but lacks BM25's document-length normalization and IDF
saturation, so on a corpus with very uneven document lengths its ranking is cruder. It's good
enough for the lexical *half* of hybrid retrieval — and RRF fusion (Q6) plus rerank (Q7) forgive
a lot of ranking noise. The real-BM25 upgrade, if eval later demands it, is **ParadeDB /
`pg_search`** (a Postgres extension with a genuine BM25 index). Deferred, named here so we know
the ceiling of what we built.

---

## Alternatives rejected

- **IVFFlat instead of HNSW** — needs representative training data at build time; worse
  recall/latency. (See §1.)
- **Editing `002` to uncomment the pre-written lines** instead of a new `003` — works with this
  naive re-run-everything migration runner, but abandons append-only migration discipline right
  as Phase 2 starts relying on it (a future Alembic/ledger runner *checksums* applied migrations
  and forbids edits). We add the next-numbered file instead.
- **Leaving `tsv` off the model** (raw SQL only in Q5) — keeps the model lean but makes it an
  incomplete mirror of the schema. We map it (read-only) to hold the Option-A convention.

## Gotcha hit during build

The **test harness only knew about `002`**. `backend/tests/conftest.py` hardcoded applying the
single file `002_schema.sql` to the throwaway testcontainer, so a new `003` would never reach the
test schema and `test_retrieve_schema.py` couldn't see the index/column. Fixed by making conftest
**glob `infra/db/migrations/*.sql` in filename order** — mirroring `apply-migrations.ps1`, so the
test container is now built by the *same construction path* as the dev DB, and every future
migration auto-reaches the tests. This closes a "forgot to register a migration → tests go green
against a stale schema" false-green, which is the same class of bug the project's "a SKIP is a
false green" rule guards against.
