"""Builds the RAGAS judge: a Gemini LLM + embeddings, with the quota enforced inside.

A RAGAS metric calls its LLM *internally* — `Faithfulness.ascore(...)` fires two Gemini
requests we never see at our call site. That leaves exactly one place to enforce the
free-tier quota (15 req/min): the LLM object the metric holds. `ThrottledLLM` and
`ThrottledEmbeddings` are thin delegating wrappers that acquire a slot from a shared
`AsyncRateLimiter` before forwarding each call, which makes the rate limit a property of
the judge itself rather than a discipline the scoring loop has to remember (and could
forget when a metric is added).

They SUBCLASS ragas' base classes instead of being duck-typed proxies because the metric
constructors type-validate what they're handed and reject a bare proxy. The surface is
small enough that delegation is honest: the LLM base declares only `generate`/`agenerate`,
the embedding base only `embed_text`/`aembed_text` (+ the batch variants).

Provider choice: ragas' NATIVE Google provider (`llm_factory(..., provider="google",
client=...)`), not the LangChain wrapper the plan originally sketched. It consumes the
`google.genai.Client` we already build in `app/gemini_client.py`, so the judge shares the
app's ONE process-cached client — the same client whose httpx pool is deliberately bound
to a single event loop (see that module). The LangChain route would have added a
dependency and a second client for no gain.

Judge model == generation model by default. That is deliberate (it's the model we have a
key for) but it means the model is GRADING ITS OWN OUTPUT — a known upward bias that the
report labels out loud rather than hides.

Module-level seams (`_get_client`, `_llm_factory`, `_GoogleEmbeddings`) so tests can build
a judge with fakes and never touch the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- import bootstrap (must run before importing `app`) ------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
try:  # best-effort; offline tests monkeypatch the seams and never need the key
    from dotenv import load_dotenv

    load_dotenv(_BACKEND / ".env")
except Exception:  # pragma: no cover - dotenv always present via pydantic-settings
    pass

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

from ragas.embeddings import GoogleEmbeddings as _GoogleEmbeddings  # noqa: E402
from ragas.embeddings.base import BaseRagasEmbedding  # noqa: E402
from ragas.llms.base import (  # noqa: E402
    InstructorBaseRagasLLM,
    InstructorLLM,
    InstructorModelArgs,
)

from app.config import get_settings  # noqa: E402

# Shared, process-cached google-genai client — the same one embed/generate use.
from app.gemini_client import get_client as _get_client  # noqa: E402

from eval.throttle import AsyncRateLimiter  # noqa: E402


def _llm_factory(model: str, provider: str, client: Any) -> InstructorLLM:
    """Build a ragas `InstructorLLM` that can actually be AWAITED.

    Not `ragas.llms.llm_factory`, and that is not a stylistic preference. For the Google
    provider that factory calls `instructor.from_genai(client)` with no async option,
    producing a SYNC `Instructor`; ragas then sets `is_async=False` and every
    `await llm.agenerate(...)` raises "Cannot use agenerate() with a synchronous client."
    Our scoring loop is async end to end, so that path is unusable. (The live smoke test
    caught this — the offline fakes never could.)

    Patching with `use_async=True` yields an `AsyncInstructor`, which ragas detects as
    async-capable, and we hand it to `InstructorLLM` with the same arguments the factory
    would have used. Same class, same behavior, just the async client.

    Kept as a module-level name with the factory's own signature so it remains the
    monkeypatch seam the tests replace.
    """
    import instructor

    patched = instructor.from_genai(client, use_async=True)
    return InstructorLLM(
        client=patched,
        model=model,
        provider=provider,
        model_args=InstructorModelArgs(),
    )


class ThrottledLLM(InstructorBaseRagasLLM):
    """Delegates to a real ragas LLM, taking a quota slot before every call."""

    def __init__(self, inner: Any, limiter: AsyncRateLimiter) -> None:
        self._inner = inner
        self.limiter = limiter

    def generate(self, prompt: str, response_model: Any) -> Any:
        # The sync path is never used by our async scoring loop (and can't await the
        # limiter), but the base class declares it, so delegate rather than lie.
        return self._inner.generate(prompt, response_model)

    async def agenerate(self, prompt: str, response_model: Any) -> Any:
        await self.limiter.acquire()
        return await self._inner.agenerate(prompt, response_model)


class ThrottledEmbeddings(BaseRagasEmbedding):
    """Delegates to real ragas embeddings, taking a quota slot before every call."""

    def __init__(self, inner: Any, limiter: AsyncRateLimiter) -> None:
        self._inner = inner
        self.limiter = limiter

    def embed_text(self, text: str, **kwargs: Any) -> Any:
        return self._inner.embed_text(text, **kwargs)

    async def aembed_text(self, text: str, **kwargs: Any) -> Any:
        await self.limiter.acquire()
        return await self._inner.aembed_text(text, **kwargs)

    async def aembed_texts(self, texts: list[str], **kwargs: Any) -> Any:
        """Embed a list by calling the SINGLE-text path once per item.

        Not a delegation to the inner batch method, and not an optimization oversight.
        Handed a list of strings, `gemini-embedding-2` returns ONE FUSED vector for the
        whole list instead of one per input. Ragas' answer relevancy then reshapes that
        single vector into `(n_questions, -1)` and fails on the dimension mismatch (seen
        live as "shapes (3,1024) and (3072,1) not aligned" — 1024 is 3072 fused/3).

        `app/ingest/embed.py` hit the identical trap and reached the identical conclusion:
        one input per call. The fan-out also keeps quota accounting honest, since each
        text really is a separate request.
        """
        return [await self.aembed_text(text, **kwargs) for text in texts]


@dataclass(frozen=True, slots=True)
class Judge:
    """The judge the metrics are constructed with, plus what it is (for the report)."""

    llm: ThrottledLLM
    embeddings: ThrottledEmbeddings
    limiter: AsyncRateLimiter
    model: str
    embedding_model: str


def build_judge(
    *,
    rpm: int,
    model: str | None = None,
    embedding_model: str | None = None,
) -> Judge:
    """Build a rate-limited RAGAS judge over the app's shared Gemini client.

    `model` defaults to `Settings.generation_model` — the judge is then the same model
    that produced the answers (self-grading; see module docstring). `embedding_model`
    defaults to `Settings.embedding_model` so answer relevancy measures similarity in the
    SAME vector space the retriever uses.

    Both wrappers share ONE limiter: the quota is per API key, not per call type, so two
    independent limiters would each allow the full rate and together double it.
    """
    settings = get_settings()
    model = model or settings.generation_model
    embedding_model = embedding_model or settings.embedding_model

    client = _get_client()
    limiter = AsyncRateLimiter(rpm)

    llm = _llm_factory(model, provider="google", client=client)
    embeddings = _GoogleEmbeddings(client=client, model=embedding_model)

    return Judge(
        llm=ThrottledLLM(llm, limiter),
        embeddings=ThrottledEmbeddings(embeddings, limiter),
        limiter=limiter,
        model=model,
        embedding_model=embedding_model,
    )
