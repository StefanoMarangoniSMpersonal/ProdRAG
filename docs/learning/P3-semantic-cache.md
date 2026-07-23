# P3 — Semantic cache: skipping the pipeline for a question we just answered

_Phase 2.5, milestone P3. Adds a cache **in front of retrieval**: if a semantically
equivalent question was answered recently, return the stored answer and skip the entire
read path — no embed-search-fuse-rerank, and crucially no billable LLM call. This note
opens with the whole query pipeline (so the cache has a place to sit in your head), then
works through the caching design decisions and where we landed naive-first on each._

---

## 1. The query pipeline: who does which work on `POST /ask`

Before the cache, the moving parts. A query is **request-shaped** (the answer comes back
in the same HTTP response, ~1-3 s) — the deliberate contrast with **job-shaped**
ingestion (`POST /documents` returns `202` and you poll, because a hi_res parse runs for
tens of seconds). One request touches five modules, each owning exactly one job:

| Stage | Module | What it owns |
|---|---|---|
| HTTP shell + audit | `app/api/ask.py` | request/response shape, the input/output guards' HTTP mapping, the two audit sinks |
| input guard | `app/guards.py` | reject blank / over-long queries **pre-spend** (`400`) |
| retrieval | `app/retrieve/retrieve.py` | query string → the ranked chunks that answer it |
| generation | `app/generate/generate.py` | those chunks → a grounded, cited answer (the one LLM call) |
| output guard | `app/guards.py` | verify every citation was a chunk the model was actually shown |

The flow, cache **off** (exactly today's Q10/P2 behavior):

```
POST /ask  (ask.py)
  validate_query(query)                guards.py    blank / >4000 chars → 400, pre-spend
  result = await retrieve(query)       retrieve.py  ── the retrieval funnel ──
     embed query (RETRIEVAL_QUERY role) ............ embed.py  (google-genai, 768-dim, L2-normed)
     asyncio.gather(                                 TWO sessions, one per arm, run concurrently
        search_semantic(vec) ......................  semantic.py  pgvector cosine / HNSW
        search_lexical(str)  ......................  lexical.py   Postgres full-text (BM25-ish)
     )
     reciprocal_rank_fusion(sem, lex) .............. fuse.py   rank-based, scale-free (RRF)
     rerank(query, pool)  [gated]  ................. rerank.py cross-encoder, wide→narrow
  answer = await generate(query, result.chunks)     generate.py  Gemini, temp 0, JSON schema
  check  = check_citations(answer.citations, ids)   guards.py  partition valid vs phantom
  log "ask.query" line  +  QueryLog row             the frozen audit contract (two sinks)
  return AskResponse
```

Two ownership subtleties worth internalizing, because the cache respects both:

- **`retrieve()` owns its own DB sessions.** It opens *two* (`SessionLocal`), one per
  concurrent arm — a single async connection can't service two queries at once, so
  `asyncio.gather` needs a session each. The handler never passes a session to
  `retrieve()`; the `Depends(get_session)` in `ask()` exists **solely** for the audit
  row. (This is the same "the orchestrator owns the sessions" contract ingestion's
  `orchestrate()` follows.)
- **The audit is best-effort and frozen.** Every query writes *two* sinks — a structured
  stdout `ask.query` line (the live tail) and a durable `query_logs` row (the queryable
  history). The row write is wrapped so a logging failure can never turn an answer the
  user already paid for into a `500`. Every milestone must *preserve and accumulate*
  this contract, never weaken it. P3 accumulates: it adds `cache_hit`.

## 2. Where the cache sits, and what changes on a hit

The cache goes **in front of retrieval** — the earliest point where we can shortcut,
because if we can answer from cache we want to skip *everything* downstream. Cache
**on**:

```
POST /ask  (ask.py)
  validate_query(query)                guards.py
  ── if cache_enabled ──────────────────────────────────────────────────────────
  qvec = await embed_query(query)      retrieve.py   ONE embed, shared with the miss path
  hit  = await cache.cache_get(qvec, owner_id)   cache.py   cosine scan, ≥ threshold?
    └─ HIT  → log(cache_hit=true, tokens=null) + QueryLog row → return stored answer
                (retrieve + generate NEVER run)
  ── MISS (or cache disabled) ──────────────────────────────────────────────────
  result = await retrieve(query, query_embedding=qvec)   ← passes the shared vector down
  answer = await generate(query, result.chunks)
  check  = check_citations(answer.citations, final_ids)
  await cache.cache_set(qvec, owner_id, post_guard_payload)   store for next time
  log("ask.query", cache_hit=false) + QueryLog row
  return AskResponse
```

A hit's whole win is the two things it **doesn't** do: no vector + full-text search over
Postgres, and no Gemini generation call. The generation call is the expensive one — it
costs money and dominates the latency — so a hit turns a ~1-3 s billable request into a
sub-100 ms free one.

## 3. Exact-match vs semantic caching (why we match on meaning)

The obvious cache keys on the **query string**: normalize it, hash it, look it up. It's
trivially correct — the same string always means the same thing. But it's nearly useless
for natural-language questions: "who is Aurelia's CEO?", "who runs Aurelia Robotics?" and
"name the CEO of Aurelia" are three cache **misses** despite being one question. Every
rephrase pays full price.

A **semantic cache** keys on the query's **embedding** and calls it a hit when a stored
vector is close enough in meaning: `cosine(query, stored) ≥ threshold`. Now all three
phrasings land near each other in vector space and hit the same entry. This is only
cheap because we already have an embedding model in the hot path — the query gets
embedded either way (retrieval needs the vector). The cache reuses that same vector; it
adds no model we didn't already run.

> **Rejected: a server-side vector index (RediSearch / redis-stack KNN).** Redis *can*
> index vectors and do k-nearest-neighbor server-side. It's the right answer at millions
> of cached queries. At our scale — a handful per owner — it buys nothing and costs a
> heavier Redis image and a schema to manage. We scan in Python instead (below), behind
> the `cache_get`/`cache_set` seam, so the index is a drop-in swap later, never a
> reshape.

## 4. The caching design axes — and our naive-first choice on each

Caching looks like one idea ("remember the answer") but it's a stack of independent
decisions. Naming them is the real lesson; the specific choices are secondary.

1. **Granularity — what do we cache?** The whole *answer* (not the retrieved chunks, not
   the embedding). We cache the end product so a hit skips the most work.
2. **Key design — what must the key include?** *Everything that changes the answer.* Our
   key is `(owner_id, query_vector)`. The owner is in the key (§6). Two things it
   *doesn't* yet include but morally should: the **corpus version** (a re-ingest can
   change the right answer) and the **model/prompt version** (a new generation model
   answers differently). Today those are fixed, so TTL (§7) covers staleness bluntly; a
   maturing system would fold a corpus/model epoch into the key so a bump invalidates
   instantly. *A cache bug is almost always a key that's missing a dimension the answer
   actually depends on.*
3. **Topology — where does it live?** Redis, shared and out-of-process, so every API
   worker sees the same cache (an in-process dict wouldn't survive a restart or scale to
   two workers). We reuse the Redis already running for Celery, on a **separate logical
   DB (`/1`** vs the broker's `/0`) so a cache flush can never touch the job queue.
4. **Invalidation / TTL — when does an entry stop being valid?** A wall-clock **TTL**
   (default 1 h). It's the crude-but-honest lever: it bounds *staleness* without needing
   to know *why* an answer went stale. The precise alternative — event-based
   invalidation, where ingesting a document evicts the cached answers it affects — is the
   deferred upgrade (§7).
5. **Eviction — what goes when it's full?** A per-owner cap (`cache_max_entries`, 500)
   dropping the **oldest** (a crude FIFO/LRU). This bounds both memory and the scan cost,
   since the scan is linear in entry count.
6. **Cache stampede / single-flight.** If ten identical queries arrive before any has
   populated the cache, all ten miss and all ten generate. The fix is *single-flight*:
   the first computes, the rest wait on it. Deferred — at single-tenant scale the
   thundering herd isn't real yet, but it's the first thing to add under load.
7. **Negative caching.** Should we cache "I don't know"? We currently *do* (a refusal is a
   valid post-guard answer). Sometimes you deliberately *don't*, so a transient empty
   retrieval isn't frozen in. A knob for later.
8. **Isolation / multi-tenancy.** The key includes the owner so no one is served
   another's answer (§6).
9. **Poisoning.** A cache that returns a **wrong** answer is worse than no cache — it's
   confidently wrong, fast, and repeatable. For a semantic cache the poison vector is the
   **threshold** (§5).
10. **Fail-open vs fail-closed.** A cache is an optimization, not a source of truth, so it
    **fails open**: any Redis error → `cache_get` returns a miss and `cache_set` no-ops,
    and the real pipeline runs. A cache outage degrades *latency*, never correctness or
    availability. (Contrast a *security* cache — an auth-decision cache fails **closed**.)
11. **Serialization / schema versioning.** Entries are JSON. `CachedAnswer` is a frozen
    dataclass; if its fields change, old entries could deserialize wrong — TTL bounds that
    blast radius today, a schema-version tag in the key is the durable fix.
12. **Observability.** You cannot tune a cache you can't measure. `cache_hit` in both
    audit sinks makes **hit rate** and **cost saved** computable straight from the query
    log — the numbers that tell you whether the cache is earning its complexity.

## 5. The similarity threshold is a correctness dial

For an exact-match cache, a hit is a hit — the key matched. For a **semantic** cache,
"close enough" is a judgment call you encode as a number, and that number trades two
failure modes against each other:

- **Threshold too high (→ 1.0):** only near-identical queries hit. Safe, but the hit rate
  collapses toward exact-match — you paid for embeddings and got little back.
- **Threshold too low:** *different* questions collide. "What's Aurelia's **revenue**?"
  hits the entry cached for "what's Aurelia's **headcount**?" and you serve a confidently
  wrong answer, fast. **This is cache poisoning**, and it's the semantic cache's unique
  hazard — an exact-match cache literally cannot do this.

We default to **0.95** — deliberately tight, biased toward *correctness over hit rate*.
Serving the right answer a bit less often beats serving a wrong answer even rarely. It's
env-tunable (`CACHE_SIMILARITY_THRESHOLD`); the honest way to move it is with the eval
harness, not vibes — the query log records every hit, so you can audit whether hits were
genuinely the same question.

(Cosine collapses to a **dot product** in our code because `embed_texts` L2-normalizes
every vector to unit length. For unit vectors `a·b == cos(a,b)`, so the scan multiplies
and sums, no norm division. It's a real optimization *and* a reason the vectors must stay
normalized — a denormalized entry would score wrong.)

## 6. Isolation rides in the key

The Redis key is `cache:{owner_id}`. Owner-in-the-key **is** the multi-tenancy boundary:
owner B's query, even with a byte-identical vector to something owner A cached, looks up a
different key and misses. This mirrors what the retrieval arms already enforce
(`owner_id` predicate on both searches) — the cache can't be the hole that leaks across
the boundary the rest of the read path guards. Today `owner_id` is the `DEV_OWNER_ID`
seam (auth is deferred to Phase 3); the key is already the right shape for real tenants.

## 7. Staleness: a hit bypasses fresh retrieval (and the guard)

The uncomfortable truth of any cache: **a hit serves the past.** Ingest a new document
now, and a query whose answer *should* change keeps returning the cached answer until its
TTL lapses (default 1 h). That's the deliberate trade — bounded staleness for a big
latency/cost win — and the honest fix is **event-based invalidation**: ingestion evicts
the cached answers its document could affect. Deferred (it needs a document→query mapping
we don't have yet); the TTL is the blunt stand-in that needs no such wiring.

A subtler point: a hit **bypasses the P2 output guard**, because generation didn't run so
there are no fresh citations to check. We handle this by storing the **post-guard**
answer — the already-repaired citation list — so a hit *inherits* the exact P2 guarantee
the miss that created it earned. We never cache a raw, unchecked model answer.

## 8. Sharing the query vector (the one change to `retrieve()`)

A semantic cache must embed the query *before* retrieval, to have a vector to look up. But
`retrieve()` also embeds the query internally. Naively wiring the cache would embed the
same query **twice** on every miss — two Gemini calls for one question. The fix is a small
additive refactor:

- A new `embed_query(query) -> list[float]` helper in `retrieve.py` is the single home for
  the query-role embed. `ask()` calls it once; the cache looks up with that vector.
- `retrieve()` gains an **optional** `query_embedding` param. Passed → it skips its
  internal embed and records `embed_ms = 0.0`. Omitted (every existing caller, all the
  immutable Q3 tests) → it embeds exactly as before. Purely additive: no caller changes,
  no immutable test breaks. The same vector will also feed **P4/HyDE**, so this refactor
  pays twice.

## 9. Why `cache_enabled` defaults **off**

Cache-off makes `ask()` **byte-identical** to Q10/P2: no early embed, no Redis call, the
`cache_hit` key omitted from both sinks, and `retrieve(query, query_embedding=None)` ==
`retrieve(query)`. That's what keeps the **immutable** `test_ask.py` / `test_retrieve.py`
green *untouched* (per CLAUDE.md's test-immutable rule) and lets the whole offline suite
run with **no Redis**. It also mirrors the `rerank_enabled` precedent — a heavy,
externally-dependent optimization ships gated, proven live, and flipped on deliberately.
The cache-on behavior gets its own spec (`test_cache.py` unit + `test_ask_cache.py`
endpoint), so the new path is fully covered without editing a frozen test.

## 10. What's deferred (named, not built)

Event-based invalidation on ingest (§7); corpus/model version in the key (§4.2);
single-flight against stampede (§4.6); a durable `cache_hit` column (it rides the
`timings_ms` JSONB today — no migration this milestone); a real server-side vector index
(§3); tunable negative caching (§4.7); and the cache-before-vs-after question for the P4
rewrite stage (decided at P4). Each has a seam already in place, so none is a reshape —
just a later drop-in.
