"""M4 embed tests: text -> normalized 768-dim vectors (Gemini via google-genai).

This is the *immutable spec* for M4 (CLAUDE.md "test-first & test-immutable"): written
and watched fail (red) before `app/ingest/embed.py` existed. Once written it does not
change to accommodate the code — the code bends to it.

Embedding hits a paid external API, so the always-on tests MOCK the google-genai client
boundary: they monkeypatch `embed._get_client` to a fake that records every call and
returns deterministic vectors. That keeps them fully offline (no key, no network, no
cost) while still exercising OUR real logic — batching, ordering, dimensionality
plumbing, normalization, and the retrieval-role prefix helpers. They run
unconditionally, so they can't SKIP into a false green.

Role note: gemini-embedding-2 dropped the `task_type` config field — the doc/query role
is now a text PREFIX the caller prepends (`as_retrieval_document` /
`as_retrieval_query`). So the spec here is that embed_texts sends NO task_type and
embeds exactly the string it is handed; the prefix format is pinned by its own test.

`test_embed_live_real_gemini` is the one exception: it calls the REAL Gemini API to
validate the actual model ID / dims / params (the thing a mock can't verify). It is
gated behind the `live` marker, which pytest.ini DESELECTS by default (not skips), so
the default suite stays 0-skip. Run it with `pytest -m live` and GEMINI_API_KEY set;
it ERRORs (never skips) when selected.
"""

from __future__ import annotations

import math
import os
from types import SimpleNamespace

import pytest

from app.ingest import embed
from app.ingest.embed import as_retrieval_document, as_retrieval_query, embed_texts

# The production embedding dimensionality (CLAUDE.md: 768 fits pgvector's HNSW 2000-dim
# cap). Pinned here so the spec never depends on a config value that could drift.
DIMS = 768


def _sent_texts(contents: list) -> list[str]:
    """Pull the plain strings back out of what embed_texts handed the SDK.

    v2 batch contract (proven against the real API, 2026-07-07): a `list[str]` is read
    as ONE multi-part Content and collapses to a SINGLE vector, so embed_texts wraps
    each input as its own `Content(parts=[Part(text=...)])` — one Content per input is
    what makes the API return one vector per input. This helper unwraps that shape so
    the assertions below can talk about the original strings.
    """
    return [part.text for content in contents for part in content.parts]


class _FakeModels:
    """Stand-in for `client.aio.models` — records calls, returns fake embeddings."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def embed_content(self, *, model, contents, config):
        # Record exactly what embed_texts sent, so tests can assert the plumbing.
        self.calls.append(
            {"model": model, "contents": list(contents), "config": config}
        )
        dims = config.output_dimensionality
        # Deterministic, deliberately NON-unit vectors ([1, 2, ..., dims]) so the
        # normalization assertion is meaningful: a passthrough would fail ||v|| == 1.
        values = [float(i + 1) for i in range(dims)]
        embeddings = [SimpleNamespace(values=list(values)) for _ in contents]
        return SimpleNamespace(embeddings=embeddings)


class _FakeClient:
    def __init__(self) -> None:
        self.aio = SimpleNamespace(models=_FakeModels())


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Swap the google-genai client and settings for fakes, so embed_texts runs its real
    batching/normalization logic against a recorded, offline boundary."""
    client = _FakeClient()
    monkeypatch.setattr(embed, "_get_client", lambda: client)
    # Control config directly (avoids env / lru_cache fiddling). Small batch_size so the
    # batching test needs only a handful of inputs; dims stays the real 768.
    monkeypatch.setattr(
        embed,
        "get_settings",
        lambda: SimpleNamespace(
            embedding_model="fake-embedding-model",
            embedding_dimensions=DIMS,
            embedding_batch_size=4,
        ),
    )
    return client


async def test_embed_returns_one_normalized_vector_per_input(
    fake_client: _FakeClient,
) -> None:
    texts = ["alpha", "beta", "gamma"]
    vectors = await embed_texts(texts)

    # One vector per input, each the configured dimensionality.
    assert len(vectors) == len(texts)
    for v in vectors:
        assert len(v) == DIMS
        # L2-normalized: unit length (the whole point of _l2_normalize).
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


async def test_embed_passes_dimensionality_and_omits_task_type(
    fake_client: _FakeClient,
) -> None:
    # gemini-embedding-2 has NO task_type: the role is a caller-built text prefix, not a
    # config field, so the SDK call must carry output_dimensionality but send no
    # task_type — and embed exactly the string handed in (embed_texts is role-agnostic).
    # A mock is the only way to prove what params actually reach the SDK.
    await embed_texts(["x"])

    (call,) = fake_client.aio.models.calls
    assert call["config"].output_dimensionality == DIMS
    assert getattr(call["config"], "task_type", None) is None
    assert call["model"] == "fake-embedding-model"
    # The input reaches the SDK as one Content per string (not a raw list[str], which v2
    # would fold into a single multi-part input and one vector); unwrapped, it's the
    # exact string handed in.
    assert _sent_texts(call["contents"]) == ["x"]


def test_retrieval_prefixes_match_gemini_embedding_2_format() -> None:
    # The v2 instruction format (Google docs): a query is tagged as a search task; a
    # document is title/text with an explicit "none" when it has no title. These helpers
    # are the single home for that format — callers wrap text, then call embed_texts.
    assert as_retrieval_query("hello") == "task: search result | query: hello"
    assert as_retrieval_document("body") == "title: none | text: body"
    assert as_retrieval_document("body", title="Intro") == "title: Intro | text: body"


async def test_embed_batches_and_preserves_order(fake_client: _FakeClient) -> None:
    # batch_size=4 (fake settings); 10 texts -> ceil(10/4) = 3 SDK calls of 4, 4, 2.
    texts = [f"t{i}" for i in range(10)]
    vectors = await embed_texts(texts)

    calls = fake_client.aio.models.calls
    assert len(calls) == 3
    assert [len(c["contents"]) for c in calls] == [4, 4, 2]
    # Flattening the per-batch contents (unwrapped from their per-input Content) recon-
    # structs the input in order: nothing dropped, reordered, or duplicated across the
    # batch boundaries.
    flattened = [t for c in calls for t in _sent_texts(c["contents"])]
    assert flattened == texts
    assert len(vectors) == len(texts)


async def test_embed_empty_input_makes_no_calls(fake_client: _FakeClient) -> None:
    # An empty batch must short-circuit — no wasted (billable) API round-trip.
    vectors = await embed_texts([])
    assert vectors == []
    assert fake_client.aio.models.calls == []


@pytest.mark.live
async def test_embed_live_real_gemini() -> None:
    # Opt-in: hits the REAL Gemini API. Deselected by default (pytest.ini -m "not
    # live"); run with `pytest -m live` and GEMINI_API_KEY set. It validates what a
    # mock can't — the real model ID, dims, and that v2 ACCEPTS the prefixed input.
    # ERRORs, never skips, on failure.
    assert os.getenv("GEMINI_API_KEY"), "GEMINI_API_KEY must be set for the live test"

    vectors = await embed_texts(
        [as_retrieval_document("What is retrieval-augmented generation?")]
    )

    assert len(vectors) == 1
    assert len(vectors[0]) == DIMS
    assert math.isclose(math.sqrt(sum(x * x for x in vectors[0])), 1.0, rel_tol=1e-3)
