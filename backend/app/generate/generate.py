"""Q8 — generate: retrieved chunks -> a grounded, cited answer, via the Gemini LLM.

This is the FINAL stage of the RAG read path. Everything upstream (embed the query ->
hybrid semantic+lexical search -> RRF fusion -> cross-encoder rerank) exists to put the
right handful of chunks in front of this stage; here we hand those chunks to a
*generation* model and ask it to write the answer.

The one job of this stage is FAITHFULNESS — the answer must be supported by the
retrieved context and nothing else. A RAG system's whole value proposition is "answers
grounded in your documents, with citations," so an answer the model invented from its
own pre-training (a "hallucination") is worse than useless: it looks authoritative and
is unaccountable. Three mechanisms enforce grounding here, cheapest-first:

  1. Prompt grounding (the system instruction). We tell the model, as a standing
     instruction, to answer ONLY from the provided context and to say it doesn't know
     when the context lacks the answer. This is the single highest-leverage lever and
     costs nothing per call. It is also imperfect (a model can still stray), which is
     why it is not the only mechanism.
  2. Low temperature. "Temperature" scales the randomness of token sampling: higher =
     more varied/creative, lower = more deterministic. Grounded extraction wants
     determinism, not creativity — we set it to 0 (`Settings.generation_temperature`) so
     the model reaches for the most-supported continuation, not a surprising one.
  3. Structured output. We constrain the model to emit JSON matching `GeneratedAnswer`
     (`response_mime_type="application/json"` + `response_schema`), so the answer AND
     its citations come back as typed data we can parse and (later) validate — no
     brittle string-scraping of prose for "which chunks did you use."

Citations are CHUNK-LEVEL (architect decision, ADR 0002): `citations` is a list of the
`chunk.id`s the answer drew from. Each chunk in the prompt is labeled with its id so the
model can refer back to them; `chunk.id` is already the citation target `retrieve()`
hands us, so nothing downstream has to reshape. Finer-grained char-span citations can be
added later behind this same Pydantic model without touching callers.

Input seam (fixed): `generate(query, chunks)` consumes exactly what `retrieve()` returns
— a `list[ScoredChunk]`. It reads only `chunk.id` (the citation target) and
`chunk.content` (the grounding text) per chunk, so it is decoupled from the ORM and from
how retrieval ranked them.

SDK surface (google-genai, same client + key as embedding — no new dependency):
    `client.aio.models.generate_content(model=, contents=<str>, config=)` where
    `config = types.GenerateContentConfig(system_instruction=, temperature=,
    max_output_tokens=, response_mime_type="application/json",
    response_schema=GeneratedAnswer)`. The response exposes `.text` (the JSON string,
    since we asked for JSON), which we validate into `GeneratedAnswer`. We use the SDK's
    async client (`client.aio`) so a generation never blocks the event loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

from pydantic import BaseModel, Field

from app.config import get_settings

# Shared, process-cached google-genai client (same one embedding uses). Imported under a
# module-level name so `test_generate.py` can monkeypatch `generate._get_client` — the
# same offline-boundary pattern embed.py and retrieve.py rely on.
from app.gemini_client import get_client as _get_client
from app.retrieve.types import ScoredChunk

# The grounding contract, given to the model as a standing system instruction (separate
# from the per-query user turn so the rules aren't buried in — or overridden by — the
# context). Its three clauses map to the three failure modes we care about: inventing
# facts, guessing when the answer is absent, and answering without attribution.
SYSTEM_INSTRUCTION = (
    "You are a retrieval-augmented assistant. Answer the user's question using "
    "ONLY the information in the provided context passages. Do not use any outside "
    "or prior knowledge. If the answer is not contained in the context, say you "
    "don't know rather than guessing. Cite the passages you used by their chunk id. "
    "Return your answer as JSON with two fields: 'answer' (the text answer) and "
    "'citations' (the list of chunk ids you relied on; empty if you don't know)."
)


class GeneratedAnswer(BaseModel):
    """The structured answer the stage returns (and the schema the model fills in).

    Doubles as the `response_schema` we hand the SDK, so the model's JSON and our parsed
    type are guaranteed to share one definition. `citations` are chunk ids (ints) — the
    chunk-level granularity locked in ADR 0002.
    """

    answer: str = Field(description="The answer, drawn only from the provided context.")
    citations: list[int] = Field(
        default_factory=list,
        description="Chunk ids the answer relied on; empty when the answer is unknown.",
    )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one generation call cost, in tokens (Q10).

    Tokens, not dollars: prices change independently of the code and per model, so a
    stored dollar figure would rot. Cost is derived at reporting time from these counts
    plus the model id. Every field is optional because the SDK may not report usage.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """The generation stage's full output: the answer, plus what it cost and took (Q10).

    FLAT on purpose — `.answer` (str) and `.citations` (list[int]) reproduce exactly the
    attribute surface `GeneratedAnswer` exposes, so every caller written against the Q8
    return type keeps working unchanged. The additions ride alongside:

    `usage` — the token counts the CLAUDE.md per-query log contract asks for. They
    can't live on `GeneratedAnswer`: that model IS the `response_schema` handed to
    Gemini, so a field added there becomes a field the MODEL fills in (it would invent
    its own token counts). Usage is knowledge about the call, not part of the answer,
    so it belongs on the wrapper the SDK never sees. `None` when no call was made (the
    empty-context refusal) or the response reported none — never a fabricated 0.

    `timings_ms` — the same eval-substrate rule retrieval follows: nothing runs
    silently.
    """

    answer: str
    citations: list[int]
    usage: TokenUsage | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


def _read_usage(response: object) -> TokenUsage | None:
    """Extract token counts from a generation response, tolerantly.

    `usage_metadata` is read with `getattr` rather than attribute access because not
    every response shape carries it. A missing count degrades to `None` ("unknown"), not
    to 0 — a zero would silently under-report spend, and an AttributeError would throw
    away a perfectly good answer over a bookkeeping detail.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    return TokenUsage(
        prompt_tokens=getattr(usage, "prompt_token_count", None),
        completion_tokens=getattr(usage, "candidates_token_count", None),
        total_tokens=getattr(usage, "total_token_count", None),
    )


def _build_prompt(query: str, chunks: list[ScoredChunk]) -> str:
    """Render the user turn: the labeled context passages followed by the question.

    Each passage is tagged `[chunk {id}]` so the model has a stable handle to cite. The
    chunks arrive best-first (retrieval already ranked them); we keep that order so the
    most relevant context leads.
    """
    passages = "\n\n".join(
        f"[chunk {sc.chunk.id}]\n{sc.chunk.content}" for sc in chunks
    )
    return f"Context:\n{passages}\n\nQuestion: {query}"


async def generate(query: str, chunks: list[ScoredChunk]) -> GenerationResult:
    """Answer `query` from `chunks`; return the answer, its citations and its cost.

    The answer is constrained to the provided context by the system instruction,
    temperature 0, and a JSON schema (see module docstring). When `chunks` is empty
    there is nothing to ground on, so we refuse locally — returning an "I don't know"
    with no citations — rather than spend a billable call that could only hallucinate
    (mirrors embed's empty-input short-circuit); that path reports no usage, because no
    call was made.
    """
    started = perf_counter()
    if not chunks:
        return GenerationResult(
            answer="I don't know — no relevant context was retrieved.",
            citations=[],
            timings_ms={"generate_ms": round((perf_counter() - started) * 1000, 1)},
        )

    # Lazy import (see module docstring): only a real generation pays to load the SDK.
    from google.genai import types

    settings = get_settings()
    client = _get_client()

    response = await client.aio.models.generate_content(
        model=settings.generation_model,
        contents=_build_prompt(query, chunks),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=settings.generation_temperature,
            max_output_tokens=settings.generation_max_output_tokens,
            # Constrain the model to emit JSON matching GeneratedAnswer, so the answer
            # and its citations come back as typed data, not prose we'd have to scrape.
            response_mime_type="application/json",
            response_schema=GeneratedAnswer,
        ),
    )

    # `.text` is the JSON string (application/json); validate it into the model, then
    # flatten it onto the result wrapper alongside the call's cost and wall time.
    parsed = GeneratedAnswer.model_validate_json(response.text)
    return GenerationResult(
        answer=parsed.answer,
        citations=parsed.citations,
        usage=_read_usage(response),
        timings_ms={"generate_ms": round((perf_counter() - started) * 1000, 1)},
    )
