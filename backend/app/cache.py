"""P3 — semantic answer cache: skip retrieve+generate for a question we just answered.

Every `POST /ask` runs the whole read path — embed the query, hybrid search, RRF fuse,
(optional) rerank, and a BILLABLE Gemini generation — even when the same question (or a
paraphrase of it) came through minutes ago. This module is the shortcut in front of that
work: store each answered query's *vector* alongside its answer, and on the next query
return the stored answer if a stored vector is close enough in MEANING.

SEMANTIC, not exact-match (the architect decision): the lookup key is the query's
embedding, and a hit is `cosine(query, stored) >= threshold`, so "who runs Aurelia?" can
hit the entry cached for "who is Aurelia's CEO?". Exact string matching would cache
almost nothing (every rephrase misses); matching on the vector is the whole point of
having an embedding model already in the hot path.

NAIVE storage, deliberately (naive-first, behind a seam): one Redis key per owner,
`cache:{owner_id}`, holding a JSON array of `{vector, ts, payload}` entries. A lookup is
one `GET` + a Python cosine scan over that bounded array (a few hundred entries max).
At our scale — a handful of cached queries per owner — a linear scan is instant and
needs no infra beyond the Redis we already run for Celery. The `cache_get` / `cache_set`
functions are the seam: a real server-side vector index (RediSearch / redis-stack KNN)
drops in behind them later without touching `ask.py`. Cosine reduces to a DOT PRODUCT
here because `embed_texts` L2-normalizes every vector to unit length.

FAIL-OPEN, always: the cache is an optimization, never a source of truth. Any Redis
error (down, timeout, malformed payload) is logged and swallowed — `cache_get` returns
`None` (treated as a miss, so the real pipeline runs) and `cache_set` is a no-op. A
cache outage must degrade latency, never correctness or availability. That is why
nothing here re-raises into the request handler.

Isolation rides in the KEY: `owner_id` is part of the Redis key, so one owner can never
be served another's cached answer even on an identical query vector — the same
per-owner boundary the retrieval arms enforce, applied to the cache. Today `owner_id`
is the `DEV_OWNER_ID` seam; when real auth lands (Phase 3) the key already fits it.

Seams (`_get_redis`, `get_settings` as module globals): imported/defined here as module
names so a test monkeypatches `cache._get_redis` with a dict-backed fake and
`cache.get_settings` with dialed thresholds — the whole cache is drivable with no live
Redis (mirrors `gemini_client.get_client` / `rerank._get_reranker`). The `redis.asyncio`
import is lazy inside `_get_redis`, so importing this module stays instant and the
offline suite never needs the client.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    # Type-only: the real client is built lazily inside _get_redis, so importing this
    # module never pulls redis.asyncio (kept off the offline suite's import path).
    from redis.asyncio import Redis

logger = logging.getLogger("app.cache")


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    """A complete answered-query record — everything to rebuild the response + audit.

    Stored on a MISS (the post-guard answer) and replayed verbatim on a HIT, so a hit
    reproduces exactly what a fresh run would have returned AND written to the audit
    sinks — no re-run, no partial reconstruction. Frozen because a cached answer is a
    fact about one past call, not a mutable buffer.

    Note what is ABSENT: token counts. A hit spends no generation, so it has no tokens
    to report; the audit records `null` there (never a fabricated 0). `citations` is the
    RAW model list; `valid_citations` is the P2-repaired (client-facing) list — both are
    kept so a hit inherits P2's repair-and-flag exactly as the miss that created it did.
    """

    answer: str
    citations: list[int]
    valid_citations: list[int]
    phantom_citations: list[int]
    retrieved_chunk_ids: list[int]
    final_chunk_ids: list[int]
    context_chars: int
    generation_model: str | None


@lru_cache
def _get_redis() -> Redis:
    """Build (once per process) the async Redis client for the cache DB.

    Cached like `gemini_client.get_client`: the client owns a connection pool bound to
    the event loop that first uses it, so there must be exactly one per process. Points
    at `cache_redis_url` (DB /1 by default — isolated from Celery's broker on /0, so a
    cache flush never touches the queue). `decode_responses=True` so `get` yields `str`,
    ready for `json.loads`.
    """
    from redis.asyncio import Redis

    return Redis.from_url(get_settings().cache_redis_url, decode_responses=True)


def _key(owner_id: uuid.UUID) -> str:
    """The per-owner Redis key. Owner in the key IS the isolation boundary."""
    return f"cache:{owner_id}"


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors — a dot product, since both are unit length.

    `embed_texts` L2-normalizes every embedding, so |a| = |b| = 1 and cosine collapses
    to the dot product. If lengths ever disagree (a corrupt entry), `strict=True` raises
    and the fail-open wrapper turns it into a miss rather than a bad match.
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


async def cache_get(
    query_embedding: list[float], owner_id: uuid.UUID
) -> CachedAnswer | None:
    """Return the closest cached answer at or above the similarity threshold, else None.

    One `GET` of the owner's entry array, then a linear scan: drop entries older than
    the TTL, compute cosine to each survivor, and return the payload of the single
    closest entry whose similarity is `>= cache_similarity_threshold`. Below it — or
    an empty/absent key — is a MISS (`None`), so the caller runs the real pipeline.
    Fail-open: any error (Redis down, malformed JSON, dimension mismatch) is logged and
    becomes a miss — the cache never breaks a request.
    """
    try:
        settings = get_settings()
        raw = await _get_redis().get(_key(owner_id))
        if not raw:
            return None
        entries = json.loads(raw)
        now = time.time()
        threshold = settings.cache_similarity_threshold
        ttl = settings.cache_ttl_seconds

        best: dict | None = None
        best_score = threshold  # a hit must clear the threshold to beat this floor
        for entry in entries:
            if now - entry["ts"] > ttl:
                continue  # stale — treat as absent (a lazy per-scan sweep)
            score = _cosine(query_embedding, entry["vector"])
            if score >= best_score:
                best_score = score
                best = entry
        if best is None:
            return None
        return CachedAnswer(**best["payload"])
    except Exception:  # noqa: BLE001 — fail-open: a cache error must never fail a query
        logger.exception("cache.get_failed owner_id=%s", owner_id)
        return None


async def cache_set(
    query_embedding: list[float], owner_id: uuid.UUID, payload: CachedAnswer
) -> None:
    """Store `payload` under `query_embedding` for `owner_id` (best-effort, fail-open).

    Read-modify-write the owner's entry array: prune expired entries, append the new
    `{vector, ts, payload}`, and trim to `cache_max_entries` by dropping the OLDEST
    (entries are appended in time order, so the tail is newest) — a crude FIFO/LRU that
    bounds both the scan cost and memory. The whole key is re-`SET` with `EX = TTL` so
    an untouched owner's cache expires on its own. Any error is logged and swallowed: a
    failed cache write can never fail the request whose answer it was trying to save.
    """
    try:
        settings = get_settings()
        redis = _get_redis()
        key = _key(owner_id)
        now = time.time()
        ttl = settings.cache_ttl_seconds

        raw = await redis.get(key)
        entries = json.loads(raw) if raw else []
        entries = [e for e in entries if now - e["ts"] <= ttl]
        entries.append(
            {"vector": list(query_embedding), "ts": now, "payload": asdict(payload)}
        )
        if len(entries) > settings.cache_max_entries:
            entries = entries[-settings.cache_max_entries :]  # keep the newest N
        await redis.set(key, json.dumps(entries), ex=ttl)
    except Exception:  # noqa: BLE001 — fail-open: a cache write must never fail a query
        logger.exception("cache.set_failed owner_id=%s", owner_id)
