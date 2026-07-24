"""P4 spec: the HyDE gate INSIDE retrieve() — which text reaches which retrieval arm.

The immutable spec for the HyDE wiring in `app/retrieve/retrieve.py`. The Q3 spec
(`test_retrieve.py`) pins the semantic-only / plain-embed path and stays untouched; this
file pins only what P4 adds:

  - HyDE ON  -> the SEMANTIC arm searches with the vector of the HYPOTHETICAL passage,
                embedded in the DOCUMENT role (`as_retrieval_document`), NOT the raw query.
  - HyDE ON  -> the LEXICAL arm still searches the RAW query string (a synthetic passage
                would dilute BM25's keyword match — the arms take different texts).
  - HyDE ON  -> `timings_ms` gains a `hyde_ms` key.
  - FAIL-OPEN -> if the hypothetical generation raises, the semantic arm falls back to the
                raw query embedded in the QUERY role; retrieval still returns a result.
  - HyDE OFF (default) -> generate_hypothetical is never called (a true no-op gate).

Fully offline: no Postgres, no Gemini. The retrieval arms (`search_semantic`,
`search_lexical`) and `SessionLocal` are monkeypatched to fakes that RECORD their inputs,
so a test can read back exactly which vector / string each arm was handed. `embed_texts`
is a recorder returning a marker vector; `generate_hypothetical` is faked per test. This
is the same module-seam monkeypatch discipline `test_retrieve.py` uses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.retrieve.retrieve as retrieve_mod
from app.ingest.embed import as_retrieval_document, as_retrieval_query
from app.retrieve.retrieve import retrieve

DIMS = 768
_HYDE_VEC = [1.0] + [0.0] * (DIMS - 1)  # marker vector the recorder returns


def _settings(**overrides) -> SimpleNamespace:
    """A fake Settings with the fields retrieve() reads; HyDE ON, rerank OFF by default.

    rerank OFF keeps the path torch-free and offline; callers override `hyde_enabled`."""
    base = dict(
        retrieval_k=10,
        retrieval_candidate_k=50,
        retrieval_rrf_k_constant=60,
        rerank_enabled=False,
        hyde_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingEmbedder:
    """Async fake `embed_texts`: records each input text list, returns the marker vector
    once per input. Recording the inputs is how a test tells the DOCUMENT-role HyDE embed
    apart from the QUERY-role fallback embed."""

    def __init__(self, vector: list[float] = _HYDE_VEC) -> None:
        self._vector = vector
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector for _ in texts]


class _RecordingSemantic:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, session, embedding, *, k, owner_id):
        self.calls.append({"embedding": embedding, "k": k, "owner_id": owner_id})
        return []


class _RecordingLexical:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, session, query, *, k, owner_id):
        self.calls.append({"query": query, "k": k, "owner_id": owner_id})
        return []


class _FakeSession:
    """A no-op async context manager standing in for an AsyncSession — the recording arms
    never touch it, so it only has to open and close."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: SimpleNamespace,
    embedder: _RecordingEmbedder,
    semantic: _RecordingSemantic,
    lexical: _RecordingLexical,
) -> None:
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(retrieve_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(retrieve_mod, "embed_texts", embedder)
    monkeypatch.setattr(retrieve_mod, "search_semantic", semantic)
    monkeypatch.setattr(retrieve_mod, "search_lexical", lexical)


async def test_hyde_on_semantic_arm_gets_document_embedded_hypothetical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, semantic, lexical = (
        _RecordingEmbedder(),
        _RecordingSemantic(),
        _RecordingLexical(),
    )
    _wire(
        monkeypatch,
        settings=_settings(),
        embedder=embedder,
        semantic=semantic,
        lexical=lexical,
    )

    async def _fake_hypothetical(query: str) -> str:
        return "A hypothetical passage answering the question."

    monkeypatch.setattr(retrieve_mod, "generate_hypothetical", _fake_hypothetical)

    await retrieve("who founded it?", k=5)

    # The one embed call must be the HYPOTHETICAL wrapped in the DOCUMENT role — not the
    # raw query, and not the QUERY-role wrapper. This is HyDE's whole premise.
    assert embedder.calls == [
        [as_retrieval_document("A hypothetical passage answering the question.")]
    ]
    # And that hypothetical's vector is what the semantic arm searched with.
    (sem_call,) = semantic.calls
    assert sem_call["embedding"] == _HYDE_VEC


async def test_hyde_on_lexical_arm_still_gets_raw_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, semantic, lexical = (
        _RecordingEmbedder(),
        _RecordingSemantic(),
        _RecordingLexical(),
    )
    _wire(
        monkeypatch,
        settings=_settings(),
        embedder=embedder,
        semantic=semantic,
        lexical=lexical,
    )
    monkeypatch.setattr(
        retrieve_mod, "generate_hypothetical", lambda q: _async("a hypothetical")
    )

    await retrieve("original user question", k=5)

    # The lexical arm indexes real lexemes; it must see the RAW query, never the
    # synthetic passage (which would dilute the keyword match).
    (lex_call,) = lexical.calls
    assert lex_call["query"] == "original user question"


async def test_hyde_on_records_hyde_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        settings=_settings(),
        embedder=_RecordingEmbedder(),
        semantic=_RecordingSemantic(),
        lexical=_RecordingLexical(),
    )
    monkeypatch.setattr(
        retrieve_mod, "generate_hypothetical", lambda q: _async("a hypothetical")
    )

    result = await retrieve("q", k=5)

    assert "hyde_ms" in result.timings_ms


async def test_hyde_generation_failure_falls_back_to_raw_query_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, semantic, lexical = (
        _RecordingEmbedder(),
        _RecordingSemantic(),
        _RecordingLexical(),
    )
    _wire(
        monkeypatch,
        settings=_settings(),
        embedder=embedder,
        semantic=semantic,
        lexical=lexical,
    )

    async def _boom(query: str) -> str:
        raise RuntimeError("HyDE model unavailable")

    monkeypatch.setattr(retrieve_mod, "generate_hypothetical", _boom)

    result = await retrieve("fallback question", k=5)

    # Fail-open: a dead HyDE call degrades to embedding the RAW query in the QUERY role
    # (exactly the non-HyDE path), never breaks retrieval.
    assert embedder.calls == [[as_retrieval_query("fallback question")]]
    (sem_call,) = semantic.calls
    assert sem_call["embedding"] == _HYDE_VEC
    assert result.query == "fallback question"


async def test_hyde_off_never_calls_generate_hypothetical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        settings=_settings(hyde_enabled=False),
        embedder=_RecordingEmbedder(),
        semantic=_RecordingSemantic(),
        lexical=_RecordingLexical(),
    )

    called = False

    async def _spy(query: str) -> str:
        nonlocal called
        called = True
        return "should not happen"

    monkeypatch.setattr(retrieve_mod, "generate_hypothetical", _spy)

    result = await retrieve("q", k=5)

    assert called is False
    assert "hyde_ms" not in result.timings_ms


async def _async(value: str) -> str:
    """Tiny coroutine helper so a lambda can return an awaitable of a fixed string."""
    return value
