"""P5 spec: retrieve() re-expressed as an explicit LangGraph graph.

P5 is a *refactor of working code*, not new behaviour: `retrieve()` keeps its exact
public contract and byte-identical output, but its internals become a LangGraph graph
(nodes = steps, edges = arrows, a conditional edge = a branch, a shared `state` object
threaded through, and a `reducer` merging two branches' writes to one state key).

Two kinds of test live here, and they honour test-first differently:

  - **Structural (red-first).** They reference the NEW `retrieve_mod._graph()` builder,
    which does not exist before the refactor -> they fail with `AttributeError` (red)
    until P5 lands. This is the genuinely-new surface P5 introduces.
  - **Characterisation / safety-net (green before AND after).** A behaviour-preserving
    refactor's proof is that observable output does not move: these drive the public
    `retrieve()` and pin the flow (parallel arms, the rerank/HyDE branches, the
    supplied-embedding short-circuit, the reducer-merged timings). They pass on the old
    linear code too — that is the point; they are the net that catches a refactor that
    silently changed behaviour. They are immutable once written, same as any test.

Seam discipline is identical to `test_retrieve.py` / `test_retrieve_hyde.py`: the graph
nodes must resolve `SessionLocal` / `embed_texts` / `search_semantic` / `search_lexical`
/ `rerank` / `generate_hypothetical` / `get_settings` as **module globals in
`retrieve.py` at call time**, so every `monkeypatch.setattr(retrieve_mod, ...)` below
(and in the immutable Q3/P4 suites) still lands after the refactor.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.retrieve.retrieve as retrieve_mod
from app.ingest.embed import as_retrieval_document, as_retrieval_query
from app.models import DEV_OWNER_ID, Chunk, Document
from app.retrieve.retrieve import retrieve

DIMS = 768
_MARKER_VEC = [1.0] + [0.0] * (DIMS - 1)


# --------------------------------------------------------------------------------------
# Offline fakes (no Postgres, no Gemini) — mirror test_retrieve_hyde.py so a test reads
# back exactly which text/vector reached each arm and which nodes ran.
# --------------------------------------------------------------------------------------


def _settings(**overrides) -> SimpleNamespace:
    """A fake Settings with the fields retrieve()/its nodes read; rerank+HyDE OFF."""
    base = dict(
        retrieval_k=10,
        retrieval_candidate_k=50,
        retrieval_rrf_k_constant=60,
        rerank_enabled=False,
        hyde_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingEmbedder:
    """Async fake `embed_texts`: records each input list, returns the marker vector."""

    def __init__(self, vector: list[float] = _MARKER_VEC) -> None:
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


class _RecordingRerank:
    """Async fake `rerank`: records that it ran, echoes the pool truncated to top_n."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, query, pool, *, top_n):
        self.calls.append({"query": query, "pool": list(pool), "top_n": top_n})
        return list(pool)[:top_n]


class _FakeSession:
    """No-op async context manager standing in for an AsyncSession."""

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
    rerank: _RecordingRerank | None = None,
) -> None:
    monkeypatch.setattr(retrieve_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(retrieve_mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(retrieve_mod, "embed_texts", embedder)
    monkeypatch.setattr(retrieve_mod, "search_semantic", semantic)
    monkeypatch.setattr(retrieve_mod, "search_lexical", lexical)
    if rerank is not None:
        monkeypatch.setattr(retrieve_mod, "rerank", rerank)


async def _async(value: str) -> str:
    return value


# --------------------------------------------------------------------------------------
# Structural — red-first: the NEW _graph() builder P5 introduces.
# --------------------------------------------------------------------------------------


def test_graph_is_memoized() -> None:
    """`_graph()` compiles the read-path graph ONCE and hands back the same object.

    Red before P5 (`retrieve_mod` has no `_graph`). After: the builder is memoized
    (`@lru_cache`), so the graph is compiled a single time and reused per query — the
    same "build the heavy thing once" pattern P0 warm-up / `_get_reranker` use."""
    first = retrieve_mod._graph()
    second = retrieve_mod._graph()
    assert first is second


def test_graph_has_expected_nodes() -> None:
    """The compiled graph exposes the pipeline stages as named nodes.

    Red before P5. The node set names the flow the linear function only implied:
    an embed step, the two parallel search arms, a fuse rejoin, and a rerank step."""
    node_names = set(retrieve_mod._graph().get_graph().nodes)
    assert {"embed", "semantic", "lexical", "fuse", "rerank"} <= node_names


# --------------------------------------------------------------------------------------
# Characterisation / safety-net — behaviour preserved through the refactor.
# --------------------------------------------------------------------------------------


def _axis_vec(*components: float) -> list[float]:
    v = [0.0] * DIMS
    for i, c in enumerate(components):
        v[i] = c
    return v


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as session:
        doc = Document(filename="p5.md", source_uri="file://p5-key/p5.md")
        session.add(doc)
        await session.flush()
        doc_id = doc.id
        await session.commit()
    return doc_id


async def _seed_chunk(
    factory: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
    *,
    ordinal: int,
    content: str,
    embedding: list[float],
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> None:
    async with factory() as session:
        session.add(
            Chunk(
                document_id=document_id,
                owner_id=owner_id,
                ordinal=ordinal,
                content=content,
                embedding=embedding,
                char_count=len(content),
            )
        )
        await session.commit()


async def test_result_byte_identical_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph produces the SAME observable output as the linear path (real pgvector).

    A on the query axis (nearest), B at 45deg (middle), C orthogonal (farthest). The
    lexical arm shares no lexeme with "q", so RRF reduces to the semantic order: A,B,C,
    each carrying a small positive RRF score, and `candidate_chunk_ids` mirrors that
    fused order (no rerank -> chunks == fused_pool[:k])."""
    doc_id = await _seed_document(session_factory)
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="A", embedding=_axis_vec(1.0)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=1, content="B", embedding=_axis_vec(1.0, 1.0)
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=2, content="C", embedding=_axis_vec(0.0, 1.0)
    )

    monkeypatch.setattr(retrieve_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(retrieve_mod, "embed_texts", _RecordingEmbedder(_axis_vec(1.0)))

    result = await retrieve("q", k=3)

    assert [sc.chunk.content for sc in result.chunks] == ["A", "B", "C"]
    # RRF-fused scores (rank-based), not cosine: strictly descending, all in (0, 1).
    scores = [sc.score for sc in result.chunks]
    assert scores[0] > scores[1] > scores[2] > 0.0
    assert scores[0] < 1.0
    # candidate_chunk_ids is the fused pool order; with no rerank it equals the returned
    # chunks' ids in order.
    assert result.candidate_chunk_ids == [sc.chunk.id for sc in result.chunks]


async def test_parallel_arms_reducer_merges_both_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both search arms run and BOTH their timings survive the fan-in.

    The two arms are parallel branches writing partial `timings` in the SAME graph
    superstep; only the reducer on the `timings` state key lets `semantic_ms` and
    `lexical_ms` coexist instead of one clobbering the other. Seeing both keys is the
    proof the reducer merged the branches."""
    semantic, lexical = _RecordingSemantic(), _RecordingLexical()
    _wire(
        monkeypatch,
        settings=_settings(),
        embedder=_RecordingEmbedder(),
        semantic=semantic,
        lexical=lexical,
    )

    result = await retrieve("q", k=5)

    assert "semantic_ms" in result.timings_ms
    assert "lexical_ms" in result.timings_ms
    # Both arms actually ran (one call each, same owner + pool width).
    assert len(semantic.calls) == 1
    assert len(lexical.calls) == 1


async def test_rerank_conditional_edge_on_routes_through_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rerank_enabled=True routes fuse -> rerank node (the conditional edge fires)."""
    rerank = _RecordingRerank()
    _wire(
        monkeypatch,
        settings=_settings(rerank_enabled=True),
        embedder=_RecordingEmbedder(),
        semantic=_RecordingSemantic(),
        lexical=_RecordingLexical(),
        rerank=rerank,
    )

    result = await retrieve("q", k=5)

    assert len(rerank.calls) == 1
    assert rerank.calls[0]["top_n"] == 5
    assert "rerank_ms" in result.timings_ms


async def test_rerank_conditional_edge_off_skips_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rerank_enabled=False routes fuse -> END; the rerank node never runs."""
    rerank = _RecordingRerank()
    _wire(
        monkeypatch,
        settings=_settings(rerank_enabled=False),
        embedder=_RecordingEmbedder(),
        semantic=_RecordingSemantic(),
        lexical=_RecordingLexical(),
        rerank=rerank,
    )

    result = await retrieve("q", k=5)

    assert rerank.calls == []
    assert "rerank_ms" not in result.timings_ms


async def test_hyde_conditional_edge_on_embeds_hypothetical_as_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hyde_enabled=True: the embed node generates a hypothetical and embeds it in the
    DOCUMENT role for the semantic arm (the HyDE branch of the embed step)."""
    embedder, semantic = _RecordingEmbedder(), _RecordingSemantic()
    _wire(
        monkeypatch,
        settings=_settings(hyde_enabled=True),
        embedder=embedder,
        semantic=semantic,
        lexical=_RecordingLexical(),
    )
    monkeypatch.setattr(
        retrieve_mod, "generate_hypothetical", lambda q: _async("hypothetical passage")
    )

    result = await retrieve("who founded it?", k=5)

    assert embedder.calls == [[as_retrieval_document("hypothetical passage")]]
    assert semantic.calls[0]["embedding"] == _MARKER_VEC
    assert "hyde_ms" in result.timings_ms


async def test_hyde_conditional_edge_off_embeds_raw_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hyde_enabled=False: the embed node takes the plain branch — the raw query in the
    QUERY role — and never calls generate_hypothetical."""
    embedder = _RecordingEmbedder()
    _wire(
        monkeypatch,
        settings=_settings(hyde_enabled=False),
        embedder=embedder,
        semantic=_RecordingSemantic(),
        lexical=_RecordingLexical(),
    )

    called = False

    async def _spy(query: str) -> str:
        nonlocal called
        called = True
        return "should not happen"

    monkeypatch.setattr(retrieve_mod, "generate_hypothetical", _spy)

    result = await retrieve("original question", k=5)

    assert called is False
    assert embedder.calls == [[as_retrieval_query("original question")]]
    assert "hyde_ms" not in result.timings_ms


async def test_supplied_embedding_short_circuits_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied query_embedding skips the embed call entirely (embed_ms 0.0).

    The P3 cache path: the vector was already computed for the cache lookup, so we
    must NOT pay Gemini again — the embed node records embed_ms 0.0 and hands the given
    vector straight to the semantic arm."""
    embedder, semantic = _RecordingEmbedder(), _RecordingSemantic()
    _wire(
        monkeypatch,
        settings=_settings(),
        embedder=embedder,
        semantic=semantic,
        lexical=_RecordingLexical(),
    )

    supplied = _axis_vec(0.0, 1.0)
    result = await retrieve("q", k=5, query_embedding=supplied)

    assert embedder.calls == []
    assert result.timings_ms["embed_ms"] == 0.0
    assert semantic.calls[0]["embedding"] == supplied
