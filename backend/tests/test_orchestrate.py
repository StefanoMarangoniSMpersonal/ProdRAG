"""M6 orchestrator tests: the first END-TO-END proof of the ingestion pipeline.

This is the immutable spec (CLAUDE.md "test-first & test-immutable"): written and
watched fail before `app/ingest/orchestrate.py` existed. It is where the earlier stages
finally run together against a real document — real parse (M2) + real chunk (M3) on the
committed Markdown corpus, into a REAL Postgres write (M5) — so "the ingestion pipeline
works" becomes literally checkable.

Two deliberate seams keep it offline and deterministic:
  - **Faked embed (M4).** `embed_texts` is monkeypatched to a recorder that returns one
    768-float vector per input — the write stage doesn't care where a vector came from,
    so we never call the paid Gemini API. The recorder also proves M6 wraps each chunk
    in the RETRIEVAL_DOCUMENT role before embedding.
  - **A tmp-dir storage backend** (`LocalDiskStorage` over `tmp_path`), so no real
    backend/storage/ is touched and `open_local` hands parse a real path.

Unlike the M1–M5 tests, this one uses the COMMITTING `session_factory` fixture (not the
rollback-based `db_session`): M6 owns the commit and drives `documents.status` through
several short transactions, so the test must let those commits land and then observe
them from a separate session — exactly as a future Celery worker / HTTP poller would.
The fixture TRUNCATEs both tables at teardown to restore isolation.

Needs a running Docker daemon (the testcontainers Postgres); it ERRORs, never SKIPs, if
the daemon is down — the "a SKIP is a false green" stance the other DB tests take.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest import orchestrate as orch
from app.models import Chunk, Document
from app.storage.local import LocalDiskStorage

DIMS = 768
FIXTURE = Path(__file__).parent / "fixtures" / "rag_test_document.md"


def _fake_vectors(texts: list[str]) -> list[list[float]]:
    """One deterministic, distinguishable 768-float vector per input text. The values
    are arbitrary — the write stage only needs length 768 — the index varies them."""
    return [[float((i + j) % 5) - 2.0 for j in range(DIMS)] for i in range(len(texts))]


class _RecordingEmbed:
    """Stand-in for M4 `embed_texts`: records the exact strings M6 asked it to embed and
    returns a matching vector list. Async, to match the real coroutine's signature."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return _fake_vectors(texts)


async def _seed_pending_document(
    factory: async_sessionmaker[AsyncSession],
    storage: LocalDiskStorage,
    *,
    data: bytes,
    filename: str,
) -> tuple[uuid.UUID, str]:
    """Persist the blob and insert a `pending` documents row pointing at it — the state
    M7 (the future upload endpoint) leaves behind. Returns (document_id, source_uri).
    Commits, so M6's own session (a separate connection) can see the row."""
    uri = await storage.save(data, filename)
    async with factory() as session:
        doc = Document(
            filename=filename, source_uri=uri
        )  # status defaults to 'pending'
        session.add(doc)
        await session.commit()
        return doc.id, uri


@pytest.fixture
def wired(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Point the orchestrator's module-level collaborators at test doubles: the
    committing container factory, a tmp-dir storage backend, and the recording embed.
    Parse (M2) and chunk (M3) are left REAL so this is a genuine end-to-end run."""
    storage = LocalDiskStorage(root=tmp_path)
    embed = _RecordingEmbed()
    monkeypatch.setattr(orch, "SessionLocal", session_factory)
    monkeypatch.setattr(orch, "get_storage", lambda: storage)
    monkeypatch.setattr(orch, "embed_texts", embed)
    return SimpleNamespace(storage=storage, embed=embed, factory=session_factory)


async def test_orchestrate_ingests_document_end_to_end(wired: SimpleNamespace) -> None:
    doc_id, _uri = await _seed_pending_document(
        wired.factory,
        wired.storage,
        data=FIXTURE.read_bytes(),
        filename="rag_test_document.md",
    )

    result = await orch.orchestrate(doc_id)

    # The returned IngestResult reports a ready doc with a positive chunk count.
    assert result.document_id == doc_id
    assert result.status == "ready"
    assert result.chunk_count > 0
    assert result.error is None

    # M6 embedded exactly one wrapped string per chunk, in RETRIEVAL_DOCUMENT role.
    assert len(wired.embed.calls) == 1
    embedded = wired.embed.calls[0]
    assert len(embedded) == result.chunk_count
    assert all(s.startswith("title: none | text: ") for s in embedded)

    # The database reflects a ready doc with contiguous chunks carrying 768-dim vectors.
    async with wired.factory() as session:
        doc = await session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == "ready"
        assert doc.error is None
        rows = (
            (
                await session.execute(
                    select(Chunk)
                    .where(Chunk.document_id == doc_id)
                    .order_by(Chunk.ordinal)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == result.chunk_count
    assert [r.ordinal for r in rows] == list(range(len(rows)))
    assert all(len(list(r.embedding)) == DIMS for r in rows)
    # A real by_title chunk of the Aurelia corpus carries recognizable content.
    assert any("Aurelia" in r.content for r in rows)


async def test_orchestrate_marks_failed_and_keeps_blob_on_error(
    wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make embedding (a mid-pipeline stage) blow up after parse+chunk have run.
    async def boom(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embed exploded")

    monkeypatch.setattr(orch, "embed_texts", boom)

    doc_id, uri = await _seed_pending_document(
        wired.factory,
        wired.storage,
        data=FIXTURE.read_bytes(),
        filename="rag_test_document.md",
    )

    result = await orch.orchestrate(doc_id)

    # Failure is reported, not raised, so a worker records it without crashing.
    assert result.status == "failed"
    assert result.chunk_count == 0
    assert "embed exploded" in (result.error or "")

    async with wired.factory() as session:
        doc = await session.get(Document, doc_id)
        assert doc is not None
        # 'failed' (not 'pending') also proves the pending->processing claim committed.
        assert doc.status == "failed"
        assert "embed exploded" in (doc.error or "")
        rows = (
            (await session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
            .scalars()
            .all()
        )

    # Atomic: the failed results transaction rolled back, so NOTHING was half-written.
    assert rows == []
    # The blob is kept — a failed run must not destroy the source (retry/debug).
    assert await wired.storage.exists(uri) is True


async def test_orchestrate_raises_on_missing_document(wired: SimpleNamespace) -> None:
    # A document_id with no row is a lost-row / caller bug: fail loudly, write nothing.
    with pytest.raises(LookupError):
        await orch.orchestrate(uuid.uuid4())
