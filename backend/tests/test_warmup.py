"""P0 — reranker warm-up on startup: the immutable spec.

Written and watched fail before `app.main`'s lifespan handler existed (CLAUDE.md
"test-first & test-immutable"). The read path's rerank stage lazily loads a ~90 MB
cross-encoder on first use, so the FIRST live query pays a ~24.8 s cold tax. P0 warms
that model once at process startup — but ONLY when rerank is enabled, so the default
(rerank OFF) path never imports torch and the offline suite stays torch-free.

This test pins that contract at the seam, with no torch, no download, no CPU:

  - `rerank._get_reranker` is the module-level `@lru_cache` loader. We monkeypatch it
    with a call-recorder, so "was the model warmed?" becomes "was the seam called?".
  - The lifespan is driven DIRECTLY via `app.router.lifespan_context(app)`. httpx's
    `ASGITransport` (used by test_ask/test_upload) does NOT fire ASGI lifespan events, so
    entering this context manager is the way to exercise startup — and, conversely, is
    why adding the lifespan can't perturb those ASGITransport-driven tests.

No Docker / DB needed — this is pure app-lifecycle, so it always runs and stays fast.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.main import app
from app.retrieve import rerank


class _Recorder:
    """A sync stand-in for `_get_reranker`: counts calls, optionally raises.

    Sync on purpose — the real `_get_reranker` is a plain sync callable the lifespan
    dispatches via `asyncio.to_thread`; the fake must be callable the same way.
    """

    def __init__(self, *, boom: bool = False) -> None:
        self.calls = 0
        self._boom = boom

    def __call__(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("model download failed")
        return object()  # stands in for the loaded scorer


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(rerank, "_get_reranker", rec)
    return rec


async def test_warmup_skipped_when_rerank_disabled(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rerank_enabled defaults to False; make the gate explicit for the reader.
    monkeypatch.setattr(get_settings(), "rerank_enabled", False)

    async with app.router.lifespan_context(app):
        # Inside the running app: the gate is off, so the model was NEVER loaded — the
        # deliberate laziness that keeps the default path from importing torch.
        assert recorder.calls == 0
    assert recorder.calls == 0


async def test_warmup_loads_once_when_rerank_enabled(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rerank_enabled", True)

    async with app.router.lifespan_context(app):
        # Warmed exactly once on startup, BEFORE any request would arrive.
        assert recorder.calls == 1


async def test_warmup_failure_does_not_crash_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gate on, but loading the model blows up (e.g. offline, can't fetch weights).
    boom = _Recorder(boom=True)
    monkeypatch.setattr(rerank, "_get_reranker", boom)
    monkeypatch.setattr(get_settings(), "rerank_enabled", True)

    # A best-effort optimisation must never turn into a boot failure: entering the
    # lifespan must complete despite the raise (the first live query just pays the cold
    # cost, exactly as before P0).
    async with app.router.lifespan_context(app):
        pass
    assert boom.calls == 1  # it was attempted, the failure was swallowed
