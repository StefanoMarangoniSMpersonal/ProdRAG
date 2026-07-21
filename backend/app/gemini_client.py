"""Shared google-genai client — one cached `Client` per process, for every Gemini call.

Both Gemini stages talk to the same API with the same key: embedding (M4,
`ingest/embed.py`) and generation (Q8, `generate/generate.py`). This module is their
single client factory so there is exactly ONE cached client per process, not one per
stage.

Why one shared client matters (not just DRY): the SDK's `Client` owns an httpx
connection pool, and that pool is bound to the event loop that first used it. The Celery
worker deliberately runs ONE persistent loop per process precisely so those pooled
connections open and close on the same live loop (see `app/worker.py`) — a second,
separately-cached client would defeat that guarantee. `@lru_cache` gives us the
process-wide singleton the discipline assumes.

The SDK import is lazy (inside the function): importing this module stays instant, and
the heavy `google.genai` import is only paid when a real Gemini call happens.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    # Type-only import: keeps `import app.gemini_client` instant; the SDK is imported
    # lazily, only when a client is actually built.
    from google.genai import Client


@lru_cache
def get_client() -> Client:
    """Build the google-genai client once per process from the API key in settings.

    Cached because the client is reusable and holds config (and its loop-bound
    connection pool — see module docstring); there's no reason to rebuild it per call.
    Raises a clear error (rather than a deep SDK one) if the key is missing, since
    that's the single most likely misconfiguration.
    """
    from google import genai

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set — the Gemini stages (embed / generate) need a "
            "Gemini API key. Put it in backend/.env (never in code)."
        )
    return genai.Client(api_key=settings.gemini_api_key)
