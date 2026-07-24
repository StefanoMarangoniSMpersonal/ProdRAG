"""P4 — HyDE: a query STRING -> a hypothetical answer PASSAGE, via the Gemini LLM.

HyDE (Hypothetical Document Embeddings) is a query-transform trick that attacks the
query<->document ASYMMETRY at the root. A question and the passage that answers it are
written differently ("Who founded Aurelia Robotics?" vs "Aurelia Robotics was founded in
2011 by..."), so a question's embedding can land a little away from the answer's — leaving
the right chunk one rank too low. HyDE closes the gap by NOT embedding the question:
instead it asks an LLM to hallucinate a short passage that *would* answer the question,
then (back in `retrieve.py`) embeds that passage in the DOCUMENT role, so it lands in the
same space as the real chunks. The hallucination's factual accuracy doesn't matter — only
that it is answer-SHAPED and topically near the truth; the hybrid lexical arm and the
downstream grounded generation are what keep a wrong hypothetical from doing harm.

This module owns only the generation half: "question in, hypothetical passage out." The
embed + wire-into-the-semantic-arm half lives in `retrieve.py` (which imports
`generate_hypothetical` as a monkeypatchable seam and calls it only when
`Settings.hyde_enabled`).

SDK surface: identical to `generate.py` — the SAME shared google-genai client and
GEMINI_API_KEY (no new dependency, no second key). The one difference is we want PLAIN
TEXT out, not JSON: a hypothetical passage is just text to embed, so there's no
`response_schema` / `response_mime_type`. `_get_client` is a module-level alias so a test
can monkeypatch `hyde._get_client` and never touch the network (the same offline-boundary
pattern embed.py / generate.py / retrieve.py use).
"""

from __future__ import annotations

from app.config import get_settings

# Shared, process-cached google-genai client (the same one embedding + generation use).
# Bound to a module-level name so `test_hyde.py` can monkeypatch `hyde._get_client`.
from app.gemini_client import get_client as _get_client

# The standing instruction that turns a QUESTION into an answer-shaped PASSAGE. We ask for
# a short, factual, self-contained passage (not a chat reply, not a restatement of the
# question) because that is what will embed nearest the real answer chunks. It is fine —
# expected, even — for the passage to assert specifics it can't verify: HyDE wants a
# plausible answer shape, and accuracy is not this stage's job.
SYSTEM_INSTRUCTION = (
    "You are helping a search system. Given a user's question, write a short, factual "
    "passage (2-4 sentences) that directly answers it, as if excerpted from a reference "
    "document. State the answer plainly and specifically; do not restate the question, "
    "hedge, or add meta-commentary. If you are unsure of exact details, write a "
    "plausible answer anyway — the passage is used only to guide retrieval."
)


async def generate_hypothetical(query: str) -> str:
    """Ask the LLM for a hypothetical answer passage to `query`; return it as plain text.

    Uses the dedicated `hyde_model` (a fast/cheap model, independent of the answer model),
    temperature `hyde_temperature` (0 for a deterministic single hypothesis, N=1), and
    caps the length at `hyde_max_output_tokens`. Returns the model's text, trimmed. The
    caller (`retrieve.py`) is responsible for fail-open — a raised error there degrades to
    embedding the raw query — so this stays a thin, single-purpose call.
    """
    # Lazy import (see module docstring): only a real HyDE call pays to load the SDK, so
    # importing this module — and thus retrieve.py — stays instant with HyDE gated off.
    from google.genai import types

    settings = get_settings()
    client = _get_client()

    response = await client.aio.models.generate_content(
        model=settings.hyde_model,
        contents=f"Question: {query}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=settings.hyde_temperature,
            max_output_tokens=settings.hyde_max_output_tokens,
        ),
    )
    return (response.text or "").strip()
