"""Q8 generate tests: retrieved chunks -> grounded, cited answer (Gemini/google-genai).

This is the *immutable spec* for Q8 (CLAUDE.md "test-first & test-immutable"): written
and watched fail (red) before `app/generate/generate.py` existed. Once written it does
not change to accommodate the code — the code bends to it.

Generation hits a paid external API, so the always-on tests MOCK the google-genai client
boundary: they monkeypatch `generate._get_client` to a fake that records every call and
returns a canned JSON answer. That keeps them fully offline (no key, network, or cost)
while still exercising OUR real logic — the context block we build from the retrieved
chunks, the grounding system instruction, the structured-output config we pass, parsing
the JSON into the `GeneratedAnswer` Pydantic model, and the empty-context refusal
short-circuit. They run unconditionally, so they can't SKIP into a false green.

`test_generate_live_real_gemini` is the one exception: it calls the REAL Gemini API to
prove grounding actually works end to end — answers *from* the context, and *refuses*
("I don't know") when the context lacks the answer. Gated behind the `live` marker,
which pytest.ini DESELECTS by default (not skips), so the suite stays 0-skip. Run
it with `pytest -m live` and GEMINI_API_KEY set; it ERRORs (never skips) when selected.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.generate import generate as gen_mod
from app.generate.generate import GeneratedAnswer, generate
from app.models import Chunk
from app.retrieve.types import ScoredChunk

# A canned answer the fake returns for every mocked call. Its citations are ints so the
# spec pins that `GeneratedAnswer.citations` is `list[int]` (the chunk-id contract).
_CANNED_JSON = '{"answer": "The CEO is Marta Silveira.", "citations": [34, 37]}'


def _scored(chunk_id: int, content: str) -> ScoredChunk:
    """A ScoredChunk carrying a transient (session-less) Chunk row — generate() only
    reads `.chunk.id` and `.chunk.content`, so a detached row is all the spec needs.
    """
    return ScoredChunk(chunk=Chunk(id=chunk_id, content=content), score=1.0)


class _FakeModels:
    """Stand-in for `client.aio.models` — records calls, returns canned JSON."""

    def __init__(self, text: str = _CANNED_JSON) -> None:
        self.calls: list[dict] = []
        self._text = text

    async def generate_content(self, *, model, contents, config):
        # Record exactly what generate() sent, so tests can assert the plumbing.
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=self._text)


class _FakeClient:
    def __init__(self, text: str = _CANNED_JSON) -> None:
        self.aio = SimpleNamespace(models=_FakeModels(text))


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Swap the google-genai client and settings for fakes, so generate() runs its real
    prompt-building / config / parsing logic against a recorded, offline boundary."""
    client = _FakeClient()
    monkeypatch.setattr(gen_mod, "_get_client", lambda: client)
    # Control config directly (avoids env / lru_cache fiddling).
    monkeypatch.setattr(
        gen_mod,
        "get_settings",
        lambda: SimpleNamespace(
            generation_model="fake-gen-model",
            generation_temperature=0.0,
            generation_max_output_tokens=256,
        ),
    )
    return client


async def test_generate_prompt_carries_every_chunk_content_and_id(
    fake_client: _FakeClient,
) -> None:
    # The model can only ground its answer on — and cite — context it actually receives,
    # so the prompt MUST carry each retrieved chunk's text AND its id (the cite target).
    chunks = [
        _scored(34, "Aurelia Robotics was founded in 2011."),
        _scored(37, "Marta Silveira is the CEO of Aurelia Robotics."),
    ]
    await generate("Who is the CEO of Aurelia Robotics?", chunks)

    (call,) = fake_client.aio.models.calls
    prompt = call["contents"]
    assert isinstance(prompt, str)
    for sc in chunks:
        assert sc.chunk.content in prompt
        assert str(sc.chunk.id) in prompt
    # The user's question must reach the model too, not just the context.
    assert "Who is the CEO of Aurelia Robotics?" in prompt


async def test_generate_system_instruction_enforces_grounding(
    fake_client: _FakeClient,
) -> None:
    # The whole anti-hallucination contract lives in the system instruction: answer ONLY
    # from the context, say you don't know when it's absent, and CITE chunk ids.
    # A mock is the only way to prove that instruction actually reaches the SDK.
    await generate("q", [_scored(1, "some context")])

    (call,) = fake_client.aio.models.calls
    system = call["config"].system_instruction.lower()
    assert "only" in system and "context" in system
    assert "don't know" in system or "do not know" in system
    assert "cite" in system


async def test_generate_passes_model_temperature_and_structured_output(
    fake_client: _FakeClient,
) -> None:
    # Grounded generation wants determinism (temperature 0) and a machine-parseable
    # answer: JSON constrained to the GeneratedAnswer schema. Pin every param sent to
    # the SDK.
    await generate("q", [_scored(1, "some context")])

    (call,) = fake_client.aio.models.calls
    assert call["model"] == "fake-gen-model"
    cfg = call["config"]
    assert cfg.temperature == 0.0
    assert cfg.max_output_tokens == 256
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is GeneratedAnswer


async def test_generate_parses_json_into_model(fake_client: _FakeClient) -> None:
    # The canned JSON must come back as a typed GeneratedAnswer with int citations — the
    # structured-output contract the /ask endpoint (Q10) and refusal eval depend on.
    result = await generate("q", [_scored(34, "ctx"), _scored(37, "ctx")])

    assert isinstance(result, GeneratedAnswer)
    assert result.answer == "The CEO is Marta Silveira."
    assert result.citations == [34, 37]
    assert all(isinstance(c, int) for c in result.citations)


async def test_generate_empty_chunks_refuses_without_calling(
    fake_client: _FakeClient,
) -> None:
    # No retrieved context = nothing to ground on. generate() must refuse locally rather
    # than spend a (billable) call that can only hallucinate — mirrors embed's guard.
    result = await generate("q", [])

    assert isinstance(result, GeneratedAnswer)
    assert result.citations == []
    assert "don't know" in result.answer.lower()
    assert fake_client.aio.models.calls == []


@pytest.mark.live
async def test_generate_live_real_gemini() -> None:
    # Opt-in: hits the REAL Gemini API. Deselected by default (-m 'not live');
    # run with `pytest -m live` and GEMINI_API_KEY set. Proves what a mock can't — the
    # real model id / structured-output params work, AND that grounding holds: answers
    # from context and refuses when the answer isn't there. ERRORs, never skips.
    assert os.getenv("GEMINI_API_KEY"), "GEMINI_API_KEY must be set for the live test"

    grounded = [_scored(7, "Marta Silveira is the CEO of Aurelia Robotics.")]
    answered = await generate("Who is the CEO of Aurelia Robotics?", grounded)
    assert "silveira" in answered.answer.lower()
    assert 7 in answered.citations

    # The context is about a CEO; the question asks something the context never states.
    refused = await generate("What is Aurelia Robotics' annual revenue?", grounded)
    assert "don't know" in refused.answer.lower() or refused.citations == []
