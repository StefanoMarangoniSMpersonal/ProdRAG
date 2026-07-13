# Q2 — Semantic search: query vector → nearest chunks, scored

_Phase 2, milestone Q2. The first stage that reads the substrate Q1 switched on: naive
vector search._

Q2 is `app/retrieve/semantic.py::search_semantic(session, query_embedding, *, k,
owner_id=DEV_OWNER_ID) -> list[ScoredChunk]`. Given a query **vector** (Q3 adds the
query-string → vector step), it returns the `k` chunks whose stored embeddings are nearest
by cosine, each tagged with a relevance score. Deliberately the *naive* version — one
`ORDER BY distance LIMIT k`; lexical (Q5), fusion (Q6), and rerank (Q7) build on top.

---

## 1. The pgvector `<=>` cosine operator

pgvector adds vector distance operators to Postgres. The one we use is **`<=>`, cosine
distance**, surfaced in SQLAlchemy as `Chunk.embedding.cosine_distance(vec)`:

```python
distance = Chunk.embedding.cosine_distance(query_embedding)   # the <=> operator
select(Chunk, distance).order_by(distance).limit(k)
```

Cosine distance measures the *angle* between two vectors, ignoring magnitude:

- **0.0** — identical direction (most similar)
- **1.0** — orthogonal (unrelated)
- **2.0** — opposite direction

**Why cosine (and why it's "free"):** our embeddings are L2-normalized at ingest (unit
length), and on unit vectors cosine similarity ≡ inner product, so cosine is both the
natural metric and the one the Q1 HNSW index was built for (`vector_cosine_ops`). The index
opclass **must match** the query operator or Postgres won't use the index — Q1 and Q2 are
deliberately paired on cosine.

---

## 2. Distance → score: why we flip to "higher = better"

`<=>` returns a *distance* (smaller = nearer). But a `ScoredChunk` carries a `score`, and
the retrieve package fixes **one convention: higher = more relevant**, so that semantic,
lexical (`ts_rank`), and rerank scores all point the same way and fusion/rerank code never
has to track which stage means what. So semantic search stores **cosine similarity**:

```
score = 1.0 - cosine_distance
#  1.0  identical      0.0  orthogonal      -1.0  opposite
```

Two implementation details worth copying:

- We `SELECT` the distance expression **alongside** the row and reuse that one value for
  *both* the `ORDER BY` and the score — so the ranking and the reported score can never
  disagree (recomputing the score separately would risk drift).
- The score is computed in Python from the returned distance (`1.0 - distance`), not in
  SQL — trivial, and keeps the query about ordering only.

This direction is pinned by a test (`test_search_semantic_score_is_similarity`): the
identical-direction chunk must score ~1.0 and the orthogonal one ~0.0. A regression to raw
distance flips those and goes red.

---

## 3. The `ef_search` recall knob — wired, not just taught

HNSW is *approximate* (Q1 §1): the graph walk can miss a true nearest neighbour.
**`hnsw.ef_search`** is the query-time dial for that — the size of the candidate list the
search keeps in flight. Higher = explores more of the graph = better recall, slower query.
pgvector's default is **40**.

It's a **runtime setting scoped per transaction**. We apply it inside `search_semantic`:

```python
await session.execute(
    text("SELECT set_config('hnsw.ef_search', :ef, true)"),
    {"ef": str(settings.retrieval_hnsw_ef_search)},
)
```

Two subtleties:

- **`set_config(name, value, is_local)`, not plain `SET`.** Postgres's `SET hnsw.ef_search =
  N` does **not** accept a bound parameter for the value — only a literal — so a
  parameterized `SET LOCAL … = :ef` fails to parse. `set_config` is the function form that
  *does* take a bound param (injection-safe), with `is_local=true` meaning LOCAL: scoped to
  this transaction, auto-reverted at commit/rollback, never leaking to another query on the
  same pooled connection. `value` must be text, hence `str(...)`.
- **Default 40 changes nothing observable now.** Wiring the knob is a "build with scaling in
  mind" move: the recall dial is a real, config-driven part of the interface today
  (`Settings.retrieval_hnsw_ef_search`, env `RETRIEVAL_HNSW_EF_SEARCH`), so when Q4 eval
  shows recall is too low it's a config turn, not a code edit. On the tiny dev corpus it has
  no measurable effect (the planner uses a seq scan anyway — Q1 §1).

---

## 4. The owner filter is the visibility seam

Every query is scoped by `owner_id`:

```python
.where(Chunk.owner_id == owner_id)   # default DEV_OWNER_ID
```

Today that's the **future RLS boundary** in disguise (owner = me, single dev user
pre-auth). But it's more general than "owner": it's the **visibility predicate** — the one
clause that decides which chunks are even candidates for search. It generalizes, at the auth
milestone, from owner-based to **role/clearance-based** filtering (RBAC/ABAC) by broadening
*this single predicate* plus adding a document-classification column and a JWT
`app_metadata` role claim — all **additive**: no reshape of vectors, callers, or the
function signature. The whole role/clearance model is **deferred** (no roles exist
pre-auth), captured here so the analysis isn't lost.

One caveat for that future, true of *any* metadata filter on an ANN index: a very
**selective** visibility filter can hurt HNSW recall (the graph walk spends its candidate
budget on rows that get filtered out). The fix, when it matters, is a partial index per
class or a higher `ef_search` — and it costs the same whether the filter is owner or role,
so the decision to defer is free. A test (`test_search_semantic_filters_by_owner`) proves
the filter holds even when another owner's chunk is the *best* vector match.

---

## Alternatives rejected

- **Exact brute-force scan** (compare the query to every row) — accurate but O(rows); the
  HNSW approximate index exists precisely to avoid it. Naive-first ≠ naive-slowest: we lean
  on the index Q1 already built.
- **Storing raw cosine distance as the score** (lower = better) — truest to what pgvector
  returns, but flips the package's "higher = better" convention, forcing every downstream
  stage to remember which direction each score points. Rejected for a uniform convention.
- **Defining `ScoredChunk` in `semantic.py`** — one fewer file, but makes `semantic.py` the
  accidental owner of a type lexical/rerank depend on (importing `lexical` would drag in
  `semantic`). It lives in the neutral `app/retrieve/types.py` instead.
- **A speculative visibility abstraction now** (a `Visibility` predicate object) — designing
  an access-control layer before auth or role data exist. Deferred; the `owner_id` param is
  already the seam.

## Gotcha hit during build

`SET LOCAL hnsw.ef_search = :ef` **does not parse** — Postgres `SET` takes only a literal,
never a bound parameter. Switched to `SELECT set_config('hnsw.ef_search', :ef, true)`, which
is the parameterizable, transaction-local equivalent (see §3).
