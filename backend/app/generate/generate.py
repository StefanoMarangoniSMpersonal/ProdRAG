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


async def generate(query: str, chunks: list[ScoredChunk]) -> GeneratedAnswer:
    """Answer `query` grounded in `chunks`; return the answer plus the cited chunk ids.

    The answer is constrained to the provided context by the system instruction,
    temperature 0, and a JSON schema (see module docstring). When `chunks` is empty
    there is nothing to ground on, so we refuse locally — returning an "I don't know"
    with no citations — rather than spend a billable call that could only hallucinate
    (mirrors embed's empty-input short-circuit).
    """
    if not chunks:
        return GeneratedAnswer(
            answer="I don't know — no relevant context was retrieved.",
            citations=[],
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

    # `.text` is the JSON string (application/json); validate it into the model.
    return GeneratedAnswer.model_validate_json(response.text)
