"""Q10 spec: generation reports its TOKEN USAGE (and its own timing) in-band.

The *immutable spec* (CLAUDE.md "test-first & test-immutable") for the one thing Q10
adds to the generation stage. Kept OUT of the immutable `test_generate.py` (the Q7/Q10
precedent: new claims go in a new file); that file's own asserts still pin the prompt,
the grounding instruction, the structured-output config and the parsing.

WHY: CLAUDE.md's logging contract says every RAG query is logged with "token/cost
usage", and the only place that number exists is the generation response's
`usage_metadata`. `generate()` used to drop it on the floor.

WHY NOT just add a `usage` field to `GeneratedAnswer`: that Pydantic model IS the
`response_schema` handed to Gemini, so any field added to it becomes a field the MODEL
is asked to fill in — it would invent its own token counts. The counts must therefore
ride outside the schema, on a wrapper the SDK never sees: `GenerationResult`.

The wrapper is deliberately FLAT (`.answer: str`, `.citations: list[int]`), i.e. it
keeps the exact attribute surface `GeneratedAnswer` had, so every existing caller
(`eval/produce.py`, `eval/refusal.py`) and their immutable tests keep working unchanged.

What's pinned here:
  1. Usage is read off the response as prompt/completion/total token counts.
  2. A response with NO `usage_metadata` (older SDK shapes, and the offline fakes)
     yields `usage is None` — never a crash, never a fabricated zero.
  3. The local empty-context refusal costs no call, so it reports no usage.
  4. The stage times itself (`generate_ms`) — the eval-substrate rule that already
     governs retrieval: no stage runs silently.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.generate import generate as gen_mod
from app.generate.generate import GenerationResult, generate
from app.models import Chunk
from app.retrieve.types import ScoredChunk

_CANNED_JSON = '{"answer": "The CEO is Marta Silveira.", "citations": [34]}'


def _scored(chunk_id: int, content: str) -> ScoredChunk:
    return ScoredChunk(chunk=Chunk(id=chunk_id, content=content), score=1.0)


class _FakeModels:
    """Stand-in for `client.aio.models`; returns canned JSON + optional usage."""

    def __init__(self, usage: object | None) -> None:
        self._usage = usage
        self.calls: list[dict] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._usage is None:
            # No usage_metadata attribute at all — the shape the Q8 fakes return.
            return SimpleNamespace(text=_CANNED_JSON)
        return SimpleNamespace(text=_CANNED_JSON, usage_metadata=self._usage)


def _wire(monkeypatch: pytest.MonkeyPatch, usage: object | None) -> _FakeModels:
    models = _FakeModels(usage)
    monkeypatch.setattr(
        gen_mod,
        "_get_client",
        lambda: SimpleNamespace(aio=SimpleNamespace(models=models)),
    )
    monkeypatch.setattr(
        gen_mod,
        "get_settings",
        lambda: SimpleNamespace(
            generation_model="fake-gen-model",
            generation_temperature=0.0,
            generation_max_output_tokens=256,
        ),
    )
    return models


async def test_generate_reports_token_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    # The google-genai usage shape: prompt / candidates / total token counts.
    _wire(
        monkeypatch,
        SimpleNamespace(
            prompt_token_count=120, candidates_token_count=18, total_token_count=138
        ),
    )

    result = await generate("q", [_scored(34, "ctx")])

    assert isinstance(result, GenerationResult)
    # The flat surface every existing caller relies on is preserved.
    assert result.answer == "The CEO is Marta Silveira."
    assert result.citations == [34]
    # ...plus the counts the query log needs (cost is DERIVED from these at reporting
    # time; prices change, so no dollar figure is stored here).
    assert result.usage is not None
    assert result.usage.prompt_tokens == 120
    assert result.usage.completion_tokens == 18
    assert result.usage.total_tokens == 138


async def test_generate_without_usage_metadata_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A response that carries no usage at all must degrade to "unknown", not to a
    # fabricated 0 (which would silently under-report spend) and not to an
    # AttributeError that would lose an otherwise perfectly good answer.
    _wire(monkeypatch, None)

    result = await generate("q", [_scored(34, "ctx")])

    assert result.answer == "The CEO is Marta Silveira."
    assert result.usage is None


async def test_generate_times_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same eval-substrate rule retrieval follows: the stage records its own wall time.
    _wire(monkeypatch, None)

    result = await generate("q", [_scored(34, "ctx")])

    assert "generate_ms" in result.timings_ms
    assert result.timings_ms["generate_ms"] >= 0.0


async def test_empty_context_refusal_reports_no_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The local refusal spends nothing, so there is nothing to bill and no counts to
    # report — usage must be None rather than zeros that look like a real (free) call.
    models = _wire(monkeypatch, None)

    result = await generate("q", [])

    assert models.calls == []
    assert result.usage is None
    assert result.citations == []
    assert "don't know" in result.answer.lower()
