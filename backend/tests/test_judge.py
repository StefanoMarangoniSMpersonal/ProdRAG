"""Spec for the Q9 judge wiring: every judge call must pass through the rate limiter.

RAGAS metrics call their LLM internally — we never see those calls at our call site, so
we cannot throttle them from the scoring loop. The only place to intercept them is the
LLM object the metric holds. `ThrottledLLM`/`ThrottledEmbeddings` are thin delegating
wrappers that `await limiter.acquire()` before forwarding, which makes the free-tier
quota a property of the JUDGE OBJECT, not something the loop has to remember.

They subclass ragas' `InstructorBaseRagasLLM` / `BaseRagasEmbedding` rather than being
duck-typed proxies because the metric constructors type-validate what they are handed; a
bare proxy is rejected at construction time.

`build_judge` is tested offline by monkeypatching its three module-level seams
(`_get_client`, `_llm_factory`, `_GoogleEmbeddings`) — no API key, no network.
"""

from __future__ import annotations

import pytest
from eval import judge as judge_mod
from eval.judge import ThrottledEmbeddings, ThrottledLLM, build_judge
from eval.throttle import AsyncRateLimiter


class _Recorder:
    """Stands in for the real ragas LLM/embeddings; records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def agenerate(self, prompt, response_model):
        self.calls.append(("agenerate", prompt, response_model))
        return "generated"

    def generate(self, prompt, response_model):
        self.calls.append(("generate", prompt, response_model))
        return "generated-sync"

    async def aembed_text(self, text, **kwargs):
        self.calls.append(("aembed_text", text))
        return [0.1, 0.2]

    async def aembed_texts(self, texts, **kwargs):
        self.calls.append(("aembed_texts", tuple(texts)))
        return [[0.1, 0.2] for _ in texts]

    def embed_text(self, text, **kwargs):
        self.calls.append(("embed_text", text))
        return [0.1, 0.2]


async def test_llm_acquires_a_slot_before_every_generation() -> None:
    inner = _Recorder()
    limiter = AsyncRateLimiter(0)  # unlimited: we assert the COUNT, not the pacing
    llm = ThrottledLLM(inner, limiter)

    result = await llm.agenerate("prompt", str)

    assert result == "generated"
    assert inner.calls == [("agenerate", "prompt", str)]
    assert limiter.requests == 1


async def test_llm_throttles_each_call_not_just_the_first() -> None:
    """Faithfulness alone makes two calls per row; both must be counted."""
    limiter = AsyncRateLimiter(0)
    llm = ThrottledLLM(_Recorder(), limiter)

    await llm.agenerate("a", str)
    await llm.agenerate("b", str)

    assert limiter.requests == 2


async def test_embeddings_acquire_for_both_single_and_batch_calls() -> None:
    """A batch embed fans out into ONE CALL PER TEXT, each taking a quota slot.

    SPEC REVISION (architect-authorized, 2026-07-21). This test originally asserted that
    `aembed_texts` forwards the whole list downstream as a single call. That is the
    natural reading of a batch API, and it is WRONG for the model we use: given a list
    of strings, `gemini-embedding-2` returns ONE fused vector for the whole list, not
    one vector per input. Ragas' answer relevancy then reshapes that single vector into
    `(n_questions, -1)` and dies on the dimension mismatch — observed live as
    "shapes (3,1024) and (3072,1) not aligned", 1024 being 3072 fused/3.

    This is the same gotcha `app/ingest/embed.py` already records for the ingest path
    (send one input per call, never a bare list). Encoding it here too means the judge
    cannot silently regress into it, and the per-text fan-out makes the request count
    honest: 3 embeddings cost 3 slots, not 1.
    """
    inner = _Recorder()
    limiter = AsyncRateLimiter(0)
    emb = ThrottledEmbeddings(inner, limiter)

    assert await emb.aembed_text("q") == [0.1, 0.2]
    vectors = await emb.aembed_texts(["a", "b"])

    # One vector per input text — never one fused vector for the batch.
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert limiter.requests == 3
    assert inner.calls == [
        ("aembed_text", "q"),
        ("aembed_text", "a"),
        ("aembed_text", "b"),
    ]


def test_build_judge_wires_the_shared_client_into_throttled_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge reuses the app's ONE cached Gemini client, wrapped in the limiter.

    Reusing `app.gemini_client.get_client` matters beyond DRY: that client owns an httpx
    pool bound to the event loop that first used it, and the codebase keeps exactly one
    per process on purpose (see app/gemini_client.py).
    """
    sentinel_client = object()
    built: dict = {}

    def fake_get_client():
        return sentinel_client

    def fake_llm_factory(model, provider, client):
        built["llm"] = (model, provider, client)
        return _Recorder()

    def fake_embeddings(client, model):
        built["emb"] = (client, model)
        return _Recorder()

    monkeypatch.setattr(judge_mod, "_get_client", fake_get_client)
    monkeypatch.setattr(judge_mod, "_llm_factory", fake_llm_factory)
    monkeypatch.setattr(judge_mod, "_GoogleEmbeddings", fake_embeddings)

    j = build_judge(rpm=15)

    assert isinstance(j.llm, ThrottledLLM)
    assert isinstance(j.embeddings, ThrottledEmbeddings)
    # Same client object for both — not two clients, not two pools.
    assert built["llm"][2] is sentinel_client
    assert built["emb"][0] is sentinel_client
    assert built["llm"][1] == "google"
    # Both wrappers share ONE limiter, or the quota would be enforced twice over at
    # half strength each.
    assert j.llm.limiter is j.limiter
    assert j.embeddings.limiter is j.limiter


def test_build_judge_records_the_models_it_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report must be able to say WHICH judge produced the scores."""
    monkeypatch.setattr(judge_mod, "_get_client", lambda: object())
    monkeypatch.setattr(
        judge_mod, "_llm_factory", lambda model, provider, client: _Recorder()
    )
    monkeypatch.setattr(
        judge_mod, "_GoogleEmbeddings", lambda client, model: _Recorder()
    )

    j = build_judge(
        rpm=15, model="some-judge-model", embedding_model="some-embed-model"
    )

    assert j.model == "some-judge-model"
    assert j.embedding_model == "some-embed-model"
