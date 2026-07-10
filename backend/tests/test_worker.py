"""Async-layer tests: the attempt-fence, the Celery task bridge, and the reaper.

These pin the behaviour that makes "Celery + a stuck-job reaper" safe. The pipeline
itself is proven by test_orchestrate.py; here we prove the *concurrency* guarantees the
reaper depends on, plus the thin Celery wiring around `orchestrate`.

The fence (this file's core):
    Turning ingestion into a queue means a job can be delivered twice — a reaper
    requeues a document whose worker is presumed dead. If that worker is actually alive
    (a too-eager timeout), two workers now process one document. The fence — a monotonic
    `attempt` counter bumped once per claim, checked at the results/failure commit —
    guarantees only the LATEST claim can write, so the superseded run corrupts nothing.
    We simulate the "someone else claimed mid-flight" race by having the (faked) embed
    stage bump `attempt` from a separate committing session before orchestrate reaches
    its results transaction.

Like the M6 tests, these use the COMMITTING `session_factory` (real testcontainers
Postgres) because the fence lives in real transactions/commits a rollback fixture can't
exercise. Needs a running Docker daemon; ERRORs (never SKIPs) if it's down.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest import orchestrate as orch
from app.models import Chunk, Document
from app.storage.local import LocalDiskStorage

DIMS = 768
FIXTURE = Path(__file__).parent / "fixtures" / "rag_test_document.md"


def _fake_vectors(texts: list[str]) -> list[list[float]]:
    """One 768-float vector per input — the write stage only checks length 768."""
    return [[float((i + j) % 5) - 2.0 for j in range(DIMS)] for i in range(len(texts))]


async def _seed_pending_document(
    factory: async_sessionmaker[AsyncSession],
    storage: LocalDiskStorage,
    *,
    data: bytes,
    filename: str,
) -> uuid.UUID:
    """Persist a blob + a `pending` documents row (the state M7 leaves behind). Commits
    so orchestrate's own session (a separate connection) can see it."""
    uri = await storage.save(data, filename)
    async with factory() as session:
        doc = Document(filename=filename, source_uri=uri)
        session.add(doc)
        await session.commit()
        return doc.id


async def _bump_attempt(
    factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> None:
    """Simulate a competing worker claiming the same document: bump `attempt` (the fence
    token) from a SEPARATE committing session, so orchestrate's results transaction sees
    a token past the one it claimed with."""
    async with factory() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(attempt=Document.attempt + 1, updated_at=func.now())
        )
        await session.commit()


async def _read(
    factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> tuple[Document, list[Chunk]]:
    async with factory() as session:
        doc = await session.get(Document, document_id)
        result = await session.execute(
            select(Chunk).where(Chunk.document_id == document_id)
        )
        rows = result.scalars().all()
    assert doc is not None
    return doc, list(rows)


@pytest.fixture
def wired(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Point orchestrate's collaborators at test doubles (committing factory, tmp-dir
    storage). Parse + chunk stay REAL; embed is set per-test."""
    storage = LocalDiskStorage(root=tmp_path)
    monkeypatch.setattr(orch, "SessionLocal", session_factory)
    monkeypatch.setattr(orch, "get_storage", lambda: storage)
    return SimpleNamespace(storage=storage, factory=session_factory)


async def test_superseded_run_writes_nothing(
    wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose fence token is superseded mid-flight must write ZERO chunks and must
    NOT flip the document to 'ready' — the winner (higher attempt) owns the outcome."""
    doc_id = await _seed_pending_document(
        wired.factory, wired.storage, data=FIXTURE.read_bytes(), filename="doc.md"
    )

    async def embed_then_get_superseded(texts: list[str]) -> list[list[float]]:
        # A competing worker claims the doc while we're "embedding": attempt 1 -> 2.
        await _bump_attempt(wired.factory, doc_id)
        return _fake_vectors(texts)

    monkeypatch.setattr(orch, "embed_texts", embed_then_get_superseded)

    result = await orch.orchestrate(doc_id)

    # The superseded run reports it stepped aside, wrote nothing.
    assert result.status == "superseded"
    assert result.chunk_count == 0

    doc, rows = await _read(wired.factory, doc_id)
    assert rows == []  # nothing written by the loser
    assert doc.status != "ready"  # never falsely marked ready
    assert doc.attempt == 2  # the competing claim's token stands


async def test_superseded_on_failure_does_not_record_failed(
    wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a superseded run ALSO errors, it must not stamp 'failed' over the winner:
    the failure record is fenced on the same token as the results commit."""
    doc_id = await _seed_pending_document(
        wired.factory, wired.storage, data=FIXTURE.read_bytes(), filename="doc.md"
    )

    async def bump_then_boom(texts: list[str]) -> list[list[float]]:
        await _bump_attempt(wired.factory, doc_id)
        raise RuntimeError("embed exploded after being superseded")

    monkeypatch.setattr(orch, "embed_texts", bump_then_boom)

    result = await orch.orchestrate(doc_id)

    assert result.status == "superseded"

    doc, rows = await _read(wired.factory, doc_id)
    assert rows == []
    assert doc.status != "failed"  # did NOT clobber the winner with a failure


async def test_claim_skips_non_pending_document(wired: SimpleNamespace) -> None:
    """A redundant delivery for a doc that isn't 'pending' (e.g. already 'ready') is
    a graceful no-op — the claim's pending-gate matches nothing, so orchestrate returns
    without touching the row or running the pipeline."""
    doc_id = await _seed_pending_document(
        wired.factory, wired.storage, data=FIXTURE.read_bytes(), filename="doc.md"
    )
    # Move it out of 'pending' as if a prior run already finished it.
    async with wired.factory() as session:
        await session.execute(
            update(Document).where(Document.id == doc_id).values(status="ready")
        )
        await session.commit()

    result = await orch.orchestrate(doc_id)

    assert result.status == "skipped"
    assert result.chunk_count == 0

    doc, rows = await _read(wired.factory, doc_id)
    assert doc.status == "ready"  # untouched
    assert rows == []


# --- The Celery task bridge ---------------------------------------------------------
# NOTE: a SYNC test on purpose. The task body calls asyncio.run(orchestrate(...)), which
# raises if a loop is already running — so we must NOT be inside pytest-asyncio's loop.
# Eager mode (task_always_eager) runs .delay() inline in-process, so we can assert the
# bridge without a broker or worker.


def test_orchestrate_task_bridges_to_orchestrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """orchestrate_task.delay(str(id)) parses the id back to a UUID and drives the async
    `orchestrate` coroutine to completion via asyncio.run — the task's whole point."""
    from app import worker

    calls: list[uuid.UUID] = []

    async def recorder(document_id: uuid.UUID):
        calls.append(document_id)
        return SimpleNamespace(status="ready", chunk_count=0)

    monkeypatch.setattr(worker, "orchestrate", recorder)
    monkeypatch.setattr(worker.celery.conf, "task_always_eager", True)
    monkeypatch.setattr(worker.celery.conf, "task_eager_propagates", True)

    doc_id = uuid.uuid4()
    worker.orchestrate_task.delay(str(doc_id))

    # Called exactly once, with the id parsed back to a real UUID (not the wire str).
    assert calls == [doc_id]


# --- The stuck-job reaper -----------------------------------------------------------


async def _seed_processing(
    factory: async_sessionmaker[AsyncSession],
    *,
    attempt: int,
    stale: bool,
) -> uuid.UUID:
    """Insert a document already in 'processing' with a given fence `attempt` and an
    `updated_at` either an hour old (stale) or fresh — the reaper keys on age."""
    async with factory() as session:
        doc = Document(filename="x.md", source_uri="local://x")
        session.add(doc)
        await session.flush()
        doc_id = doc.id
        updated = func.now() - timedelta(hours=1) if stale else func.now()
        await session.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(status="processing", attempt=attempt, updated_at=updated)
        )
        await session.commit()
        return doc_id


async def test_reaper_requeues_stale_and_fails_poison(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reaper flips a stale 'processing' row back to 'pending' and re-enqueues it;
    a stale row already at the max-attempts cap is failed instead (poison-pill); a fresh
    'processing' row is left alone."""
    from app import worker

    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    enqueued: list[str] = []
    monkeypatch.setattr(
        worker, "orchestrate_task", SimpleNamespace(delay=enqueued.append)
    )

    # Default cap is 3 (settings.ingest_max_processing_attempts).
    stale_retryable = await _seed_processing(session_factory, attempt=1, stale=True)
    stale_poison = await _seed_processing(session_factory, attempt=3, stale=True)
    fresh = await _seed_processing(session_factory, attempt=1, stale=False)

    reaped = await worker._reap()

    # Both stale rows were acted on; the fresh one was not seen.
    assert reaped == 2

    async with session_factory() as session:
        retryable = await session.get(Document, stale_retryable)
        poison = await session.get(Document, stale_poison)
        untouched = await session.get(Document, fresh)
    assert retryable is not None and retryable.status == "pending"
    assert poison is not None and poison.status == "failed"
    assert poison.error is not None and "attempt" in poison.error.lower()
    assert untouched is not None and untouched.status == "processing"

    # Only the retryable row was re-enqueued, as its string id; the poison one was not.
    assert enqueued == [str(stale_retryable)]
