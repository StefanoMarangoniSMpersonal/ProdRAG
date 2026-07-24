"""P4 HyDE spec: a query STRING -> a hypothetical answer PASSAGE, via the Gemini LLM.

The *immutable spec* for `app/retrieve/hyde.py` (CLAUDE.md "test-first & test-immutable"):
written and watched fail (red — `ModuleNotFoundError`) before the module exists. The code
bends to it, never the reverse.

HyDE (Hypothetical Document Embeddings) transforms the query BEFORE retrieval: instead of
embedding the user's question, we ask an LLM to write a short passage that *would* answer
it, then (in retrieve.py) embed that passage in the DOCUMENT role so it lands in the same
space as the real chunks. This module owns only the generation half — "question in,
hypothetical passage out."

Like `test_generate.py`, generation hits a paid external API, so these always-on tests
MOCK the google-genai client boundary: monkeypatch `hyde._get_client` to a fake that
records every call and returns a canned passage. That keeps them fully offline (no key,
network, or cost) while exercising OUR real logic — the prompt we build, the config we
pass, and returning the model's text. They run unconditionally, so they can't SKIP into a
false green.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieve import hyde as hyde_mod
from app.retrieve.hyde import generate_hypothetical

# A canned hypothetical the fake returns for every mocked call. No surrounding whitespace,
# so the return-value assert pins that the module hands back the model's text verbatim.
_CANNED = "Aurelia Robotics was founded in 2011 and is led by CEO Marta Silveira."


class _FakeModels:
    """Stand-in for `client.aio.models` — records calls, returns a canned passage."""

    def __init__(self, text: str = _CANNED) -> None:
        self.calls: list[dict] = []
        self._text = text

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=self._text)


class _FakeClient:
    def __init__(self, text: str = _CANNED) -> None:
        self.aio = SimpleNamespace(models=_FakeModels(text))


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Swap the google-genai client and settings for fakes, so generate_hypothetical runs
    its real prompt-building / config logic against a recorded, offline boundary."""
    client = _FakeClient()
    monkeypatch.setattr(hyde_mod, "_get_client", lambda: client)
    monkeypatch.setattr(
        hyde_mod,
        "get_settings",
        lambda: SimpleNamespace(
            hyde_model="fake-hyde-model",
            hyde_temperature=0.0,
            hyde_max_output_tokens=256,
        ),
    )
    return client


async def test_generate_hypothetical_returns_model_text(
    fake_client: _FakeClient,
) -> None:
    # The one job: return the model's passage so retrieve.py can embed it. Verbatim
    # (trimmed) — no reshaping, no JSON, just the text.
    out = await generate_hypothetical("Who is the CEO of Aurelia Robotics?")
    assert out == _CANNED


async def test_generate_hypothetical_sends_query_to_the_hyde_model(
    fake_client: _FakeClient,
) -> None:
    # The query must reach the model (that's what it writes a hypothetical for), and the
    # call must target the dedicated hyde_model — a cheap/fast model separate from the
    # answer model, so a mock is the only way to prove the plumbing.
    await generate_hypothetical("Who is the CEO of Aurelia Robotics?")

    (call,) = fake_client.aio.models.calls
    assert call["model"] == "fake-hyde-model"
    assert "Who is the CEO of Aurelia Robotics?" in call["contents"]


async def test_generate_hypothetical_instructs_writing_a_passage(
    fake_client: _FakeClient,
) -> None:
    # The system instruction is what turns a QUESTION into an answer-shaped PASSAGE — the
    # whole premise of HyDE. Pin that it tells the model to write an answer/passage.
    await generate_hypothetical("q")

    (call,) = fake_client.aio.models.calls
    system = call["config"].system_instruction.lower()
    assert "passage" in system or "answer" in system


async def test_generate_hypothetical_applies_temperature_and_token_cap(
    fake_client: _FakeClient,
) -> None:
    # Determinism (temperature 0 at N=1) and a short passage (token cap, to fight
    # verbosity dilution + cost) are load-bearing HyDE settings — pin every param sent.
    await generate_hypothetical("q")

    (call,) = fake_client.aio.models.calls
    cfg = call["config"]
    assert cfg.temperature == 0.0
    assert cfg.max_output_tokens == 256
