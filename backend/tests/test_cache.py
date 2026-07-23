"""P3 — semantic cache unit spec (test-first; no DB, no live Redis).

The *immutable spec* (CLAUDE.md "test-first & test-immutable"): written and watched
fail before `app/cache.py` exists. It pins the cache's behavioral contract — match on
*meaning* (cosine over stored query vectors), owner isolation, TTL expiry, bounded
eviction, and — the load-bearing property for a fail-open optimization — that a broken
Redis degrades to a MISS, never an exception that reaches `/ask`.

Offline by construction. Redis is faked with a dict-backed async stand-in monkeypatched
onto the `_get_redis` seam (the same module-seam faking `embed._get_client` /
`rerank._get_reranker` use), and settings are faked with a `SimpleNamespace` onto the
`get_settings` seam so each test dials threshold / TTL / capacity deterministically. No
`fakeredis` dependency, no container — these run anywhere the suite runs.

Vectors live on orthonormal axes so cosine is exact: since `embed_texts` L2-normalizes
every embedding, two unit vectors' cosine similarity IS their dot product — `_axis(i)`
with itself is 1.0, with a different axis is 0.0, and `_near(c)` sits at exactly cosine
`c` from `_axis(0)` (the correctness-boundary knob).
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app import cache

OWNER = uuid.uuid4()
OTHER_OWNER = uuid.uuid4()
DIMS = 8


def _axis(i: int) -> list[float]:
    """A unit vector on axis `i` (orthonormal: self-cosine 1.0, cross-cosine 0.0)."""
    v = [0.0] * DIMS
    v[i] = 1.0
    return v


def _near(cos: float) -> list[float]:
    """A unit vector at exactly cosine `cos` from `_axis(0)` (the threshold knob)."""
    v = [0.0] * DIMS
    v[0] = cos
    v[1] = math.sqrt(1.0 - cos * cos)
    return v


def _settings(
    *, threshold: float = 0.95, ttl: int = 3600, max_entries: int = 500
) -> SimpleNamespace:
    return SimpleNamespace(
        cache_similarity_threshold=threshold,
        cache_ttl_seconds=ttl,
        cache_max_entries=max_entries,
    )


class _FakeRedis:
    """Dict-backed async stand-in for `redis.asyncio.Redis` (only get / set-with-ex)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.sets.append((key, value, ex))


def _payload(answer: str = "The CEO is Marta Silveira.") -> cache.CachedAnswer:
    return cache.CachedAnswer(
        answer=answer,
        citations=[13],
        valid_citations=[13],
        phantom_citations=[],
        retrieved_chunk_ids=[11, 12, 13, 14],
        final_chunk_ids=[13, 11],
        context_chars=42,
        generation_model="gemini-3.1-flash-lite",
    )


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """A fresh fake Redis on `_get_redis` + default settings on `get_settings`."""
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_get_redis", lambda: fake)
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    return fake


# --- basic hit / miss ----------------------------------------------------------------


async def test_get_on_empty_store_is_a_miss(redis: _FakeRedis) -> None:
    assert await cache.cache_get(_axis(0), OWNER) is None


async def test_set_then_get_same_vector_returns_the_payload(redis: _FakeRedis) -> None:
    payload = _payload()
    await cache.cache_set(_axis(0), OWNER, payload)
    got = await cache.cache_get(_axis(0), OWNER)
    assert got == payload  # frozen dataclass equality survives the JSON round-trip


# --- the correctness boundary: cosine >= threshold -----------------------------------


async def test_below_threshold_near_vector_is_a_miss(redis: _FakeRedis) -> None:
    # Stored at axis 0; queried at cosine 0.9 — below the 0.95 threshold, so serving it
    # would be answering a DIFFERENT question. Must miss.
    await cache.cache_set(_axis(0), OWNER, _payload())
    assert await cache.cache_get(_near(0.9), OWNER) is None


async def test_above_threshold_near_vector_is_a_hit(redis: _FakeRedis) -> None:
    # Cosine 0.98 >= 0.95: a genuine paraphrase, close enough to reuse the answer.
    await cache.cache_set(_axis(0), OWNER, _payload())
    got = await cache.cache_get(_near(0.98), OWNER)
    assert got is not None


async def test_get_returns_the_closest_entry(redis: _FakeRedis) -> None:
    # Two entries both above threshold to axis 0; the nearer one (cosine 0.99) wins.
    await cache.cache_set(_near(0.96), OWNER, _payload(answer="farther"))
    await cache.cache_set(_near(0.99), OWNER, _payload(answer="closest"))
    got = await cache.cache_get(_axis(0), OWNER)
    assert got is not None and got.answer == "closest"


# --- multi-tenancy: the key includes the owner ---------------------------------------


async def test_owner_isolation_identical_vector_never_leaks(redis: _FakeRedis) -> None:
    await cache.cache_set(_axis(0), OWNER, _payload())
    # Same vector, different owner -> a different Redis key -> a miss.
    assert await cache.cache_get(_axis(0), OTHER_OWNER) is None


# --- fail-open: a broken Redis degrades to a miss / no-op, never raises ---------------


async def test_get_fails_open_when_redis_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> _FakeRedis:
        raise RuntimeError("redis is down")

    monkeypatch.setattr(cache, "_get_redis", _boom)
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    assert await cache.cache_get(_axis(0), OWNER) is None


async def test_set_fails_open_when_redis_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> _FakeRedis:
        raise RuntimeError("redis is down")

    monkeypatch.setattr(cache, "_get_redis", _boom)
    monkeypatch.setattr(cache, "get_settings", lambda: _settings())
    # Must not raise — a cache write failure can never fail a request.
    await cache.cache_set(_axis(0), OWNER, _payload())


# --- freshness + capacity ------------------------------------------------------------


async def test_expired_entry_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_get_redis", lambda: fake)
    monkeypatch.setattr(cache, "get_settings", lambda: _settings(ttl=3600))
    # Seed one entry stamped well beyond the TTL.
    stale = [
        {
            "vector": _axis(0),
            "ts": time.time() - 4000,
            "payload": asdict(_payload()),
        }
    ]
    fake.store[f"cache:{OWNER}"] = json.dumps(stale)
    assert await cache.cache_get(_axis(0), OWNER) is None


async def test_max_entries_evicts_the_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_get_redis", lambda: fake)
    monkeypatch.setattr(cache, "get_settings", lambda: _settings(max_entries=2))
    await cache.cache_set(_axis(0), OWNER, _payload(answer="A"))
    await cache.cache_set(_axis(1), OWNER, _payload(answer="B"))
    await cache.cache_set(_axis(2), OWNER, _payload(answer="C"))
    # Capacity 2: the oldest (axis 0 / "A") is evicted; the two newest survive.
    assert await cache.cache_get(_axis(0), OWNER) is None
    b = await cache.cache_get(_axis(1), OWNER)
    c = await cache.cache_get(_axis(2), OWNER)
    assert b is not None and b.answer == "B"
    assert c is not None and c.answer == "C"
