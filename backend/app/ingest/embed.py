"""M4 — embed: text -> normalized 768-dim vectors, via the Gemini embedding API.

This is the third stage of the ingestion pipeline. It turns text into the numeric
**vector** (a list of floats — a point in a high-dimensional space) that pgvector
stores and semantic search compares. Chunks whose vectors are close together mean
similar things, which is what lets retrieval find relevant passages for a query later.

Why this takes `list[str]`, not chunks (interface A):
    M4 speaks plain strings, never Unstructured `Element` objects. Two payoffs:
      1. It keeps the third-party Unstructured type contained to parse+chunk (the
         "library stays thin" rule) — embed knows nothing about documents or chunks.
      2. The SAME function serves both sides of retrieval. At *ingest* the caller (M6)
         passes each chunk's text; at *query* time it passes the user's question.
    Whoever calls M4 chooses *what text* to embed. Today that is just the chunk's
    content; the deferred enrich stage (Phase 2) will, for a table/image chunk,
    substitute an LLM-written summary — but it does so *upstream*, by handing M4 a
    different string. M4 never changes when enrich lands, because it only ever sees
    final text (the `embed_text` seam, resolved in the orchestrator, not here).

Asymmetric retrieval roles (mandatory — CLAUDE.md) — now a text prefix, not a field:
    An embedding model embeds the same text differently depending on its *role* — a
    stored document to be found vs a search query — and matching those roles is what
    makes query-to-document similarity meaningful. `gemini-embedding-001` took a
    `task_type` config value (`RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`) for this, but
    **`gemini-embedding-2` removed it**: the backend now ignores `task_type` entirely.
    The role is instead encoded as an instruction PREFIX prepended to the input text
    (Google's documented v2 format) — documents as `title: … | text: …`, queries as
    `task: search result | query: …`.
    The caller builds that prefix with `as_retrieval_document` / `as_retrieval_query`
    (this module is the single home for the format), then hands the finished string to
    `embed_texts`, which is role-agnostic and embeds whatever it is given. A caller that
    forgets to wrap its text still gets a vector — just a silently worse one — so the
    wrapping is the call site's responsibility, like the old required `task_type` was.

Normalization (defensive; v2 already does it):
    Cosine similarity and inner-product both behave best on unit-length vectors.
    `gemini-embedding-2` is trained with Matryoshka Representation Learning (MRL) — the
    vector's dimensions are ordered by importance, so a truncated prefix is still a
    valid embedding — and it AUTO-normalizes even when we request a reduced
    `output_dimensionality` of 768.
    (`gemini-embedding-001`, by contrast, only returned unit vectors at its native 3072
    dims, so truncation there left a non-unit vector — the bug this code originally
    guarded against.) We keep the L2-normalize anyway: on an already-unit vector it is a
    no-op, and it keeps the invariant true if we ever swap in a model that doesn't
    self-normalize. Cheap insurance, no behavior change for v2.

Batching + async:
    The API accepts several texts per request, so we send them in batches of
    `embedding_batch_size` rather than one HTTP round-trip per chunk. Batches run
    sequentially for now (naive-first); the `await` seam already lets us parallelize
    later without changing any caller. We use the SDK's async client (`client.aio`) so
    an embed never blocks the event loop — the same discipline as parse/chunk.

SDK surface (google-genai, verified against 2.10.0 — batch shape re-verified live
2026-07-07):
    `client.aio.models.embed_content(model=, contents=[Content, ...], config=)` where
    `config = types.EmbedContentConfig(output_dimensionality=)` — no `task_type` for v2.
    CRUCIAL: to embed N texts in one call, pass N *Content* objects (one text each),
    NOT a `list[str]` — the SDK reads a bare string list as the many Parts of ONE
    Content and returns a SINGLE fused vector. The response carries `.embeddings`,
    aligned with `contents`, each exposing `.values` (the float vector). The opt-in
    live tests (`pytest -m live`) revalidate this against the real API.
"""

from __future__ import annotations

import math

from app.config import get_settings

# The google-genai client now lives in the shared `app.gemini_client` module (one cached
# client per process, shared with the Q8 generation stage — see that module's docstring
# for the loop-bound-connection-pool reason it must be a singleton). Imported under the
# original `_get_client` name so this module's behavior — and its immutable tests, which
# monkeypatch `embed._get_client` — are unchanged by the move.
from app.gemini_client import get_client as _get_client


def _l2_normalize(vector: list[float]) -> list[float]:
    """Scale `vector` to unit length (L2 norm = 1). A zero vector is returned unchanged
    rather than dividing by zero — Gemini shouldn't emit one, but the guard keeps a
    degenerate input from raising."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def as_retrieval_document(text: str, *, title: str | None = None) -> str:
    """Wrap `text` as a stored document for retrieval (the `RETRIEVAL_DOCUMENT` role).

    gemini-embedding-2 format: ``title: {title} | text: {text}``, with an explicit
    ``title: none`` when the chunk has no title. `title` is a seam for later —
    `by_title` chunks carry a section heading and the deferred enrich stage can supply
    one — but ingest passes none today. Callers use this at ingest, then pass the
    result to `embed_texts`.
    """
    return f"title: {title or 'none'} | text: {text}"


def as_retrieval_query(text: str) -> str:
    """Wrap `text` as a search query for retrieval (the `RETRIEVAL_QUERY` role).

    gemini-embedding-2 format: ``task: search result | query: {text}``. Its asymmetry
    with the document format above is exactly what makes query-to-document similarity
    meaningful. Callers use this at query time, then pass the result to `embed_texts`.
    """
    return f"task: search result | query: {text}"


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed `texts` into normalized float vectors with Gemini, in input order.

    `texts` are embedded EXACTLY as given — `embed_texts` is role-agnostic. The caller
    encodes the retrieval role by wrapping each string first: `as_retrieval_document` at
    ingest, `as_retrieval_query` at query time (gemini-embedding-2 has no `task_type`;
    see module docstring). Texts are embedded in batches of
    `Settings.embedding_batch_size`; the returned list has one
    `embedding_dimensions`-long vector per input text, each L2-normalized, in the same
    order as `texts`. An empty input returns `[]` without calling the API.
    """
    if not texts:
        return []

    # Lazy import (see the TYPE_CHECKING note): only a real embed pays to load the SDK.
    from google.genai import types

    settings = get_settings()
    client = _get_client()
    batch_size = settings.embedding_batch_size

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        # Each text must be its OWN Content, or v2 folds a raw list[str] into a single
        # multi-part input and returns ONE vector for the whole batch (verified against
        # the real API 2026-07-07). One Content per input == one vector per input.
        response = await client.aio.models.embed_content(
            model=settings.embedding_model,
            contents=[types.Content(parts=[types.Part(text=t)]) for t in batch],
            config=types.EmbedContentConfig(
                output_dimensionality=settings.embedding_dimensions,
            ),
        )
        # Preserve order: the API returns embeddings aligned with `contents`, and we
        # extend in batch order, so the final list matches `texts` index-for-index.
        vectors.extend(_l2_normalize(e.values) for e in response.embeddings)

    return vectors
