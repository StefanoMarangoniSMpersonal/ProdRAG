"""Spec: GET /ask/config exposes the read-path defaults the chat UI initialises from.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the read-only config
endpoint. The chat's control panel must show what the SERVER would do by default before you
override anything per message (the architect's decision: the panel mirrors the running env,
it doesn't guess). So `/ask/config` returns the three live-tunable knobs straight off
`Settings`. No DB, no Gemini — a pure settings read, so this test needs neither.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.api import ask as ask_mod
from app.config import Settings
from app.main import app


async def test_ask_config_returns_the_read_path_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Distinct-from-default values so we prove the endpoint reflects Settings, not a
    # hard-coded literal.
    monkeypatch.setattr(
        ask_mod,
        "get_settings",
        lambda: Settings(
            rerank_enabled=True, cache_enabled=True, rerank_score_floor=-2.5
        ),
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ask/config")

    assert resp.status_code == 200
    assert resp.json() == {
        "rerank_enabled": True,
        "cache_enabled": True,
        "rerank_score_floor": -2.5,
    }
