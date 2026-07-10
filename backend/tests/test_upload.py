"""M7 upload-endpoint tests: the HTTP entry point to ingestion.

The immutable spec (CLAUDE.md "test-first & test-immutable"): written and watched fail
before `app/api/documents.py` existed. M7 is a thin HTTP shell over the finished M6
orchestrator, so these tests pin the *wiring* of the 202-and-poll contract, not the
pipeline itself:

    POST /documents      -> store blob + insert a `pending` row + kick the job + 202
    GET  /documents/{id} -> the poll a client uses to watch `status` reach ready/failed

Two deliberate seams keep it offline and fast (the M6-test pattern):
  - **Faked enqueue.** The endpoint's module-level `orchestrate_task.delay` (the Celery
    send) is monkeypatched to a recorder — so we prove the job was ENQUEUED with the new
    doc id without a broker, a worker, or running parse/embed/write. The real pipeline's
    own end-to-end proof is `test_orchestrate.py`.
  - **A tmp-dir storage backend + the committing container factory.** `get_storage` is
    swapped for a `LocalDiskStorage` over `tmp_path`, and the request's DB session is
    overridden to the testcontainers Postgres via the *committing* `session_factory`
    (the endpoint must durably commit the `pending` row before the job looks it up).

The app is driven in-process over ASGI with `httpx.AsyncClient` + `ASGITransport`. The
enqueue is a synchronous `.delay(...)` call inside the handler, so by the time a POST
returns the recorder has already captured the id — which is exactly how we assert the
job was kicked.

Needs a running Docker daemon (the testcontainers Postgres); it ERRORs, never SKIPs, if
the daemon is down — the "a SKIP is a false green" stance the other DB tests take.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import documents
from app.db import get_session
from app.main import app
from app.models import Document
from app.storage.local import LocalDiskStorage

FILENAME = "rag_test_document.md"
CONTENT_TYPE = "text/markdown"
BODY = b"# Aurelia\n\nThe city of Aurelia sits on the river.\n"


class _RecordingEnqueue:
    """Stand-in for `orchestrate_task.delay`: records the (string) document ids the
    endpoint enqueued a job for. Synchronous, like Celery's `.delay` send. Its return
    value (normally an AsyncResult) is ignored — the client polls documents.status."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, document_id: str) -> None:
        self.calls.append(document_id)


@pytest.fixture
async def wired(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Point the endpoint's collaborators at test doubles and hand back an ASGI client.

    Storage -> a tmp-dir backend; `orchestrate_task.delay` -> the recorder; the request
    DB session -> the committing container factory (via FastAPI's dependency override,
    since the endpoint depends on `get_session`). The override is removed at teardown so
    the app is left clean for the next test.
    """
    storage = LocalDiskStorage(root=tmp_path)
    recorder = _RecordingEnqueue()
    monkeypatch.setattr(documents, "get_storage", lambda: storage)
    monkeypatch.setattr(documents.orchestrate_task, "delay", recorder)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, storage=storage, recorder=recorder, factory=session_factory
        )
    app.dependency_overrides.pop(get_session, None)


async def test_upload_stores_row_and_kicks_job(wired: SimpleNamespace) -> None:
    resp = await wired.client.post(
        "/documents",
        files={"file": (FILENAME, BODY, CONTENT_TYPE)},
    )

    # 202 Accepted + the new doc id and a pending status (NOT a claim it's ingested).
    assert resp.status_code == 202
    payload = resp.json()
    doc_id = uuid.UUID(payload["id"])  # a real UUID
    assert payload["status"] == "pending"

    # The persisted row carries everything M7 owns (byte_size/checksum/content_type),
    # which M6 deliberately left to the row's creator.
    async with wired.factory() as session:
        doc = await session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == "pending"
        assert doc.filename == FILENAME
        assert doc.byte_size == len(BODY)
        assert doc.checksum == hashlib.sha256(BODY).hexdigest()
        assert doc.content_type == CONTENT_TYPE
        source_uri = doc.source_uri

    # The blob really landed in storage and holds the exact bytes we uploaded.
    assert await wired.storage.exists(source_uri) is True
    assert await wired.storage.load(source_uri) == BODY

    # The ingestion job was enqueued exactly once, for this doc id (the id is serialized
    # to a string for the JSON broker, so that's what the recorder captures).
    assert wired.recorder.calls == [str(doc_id)]


async def test_upload_rejects_empty_file(wired: SimpleNamespace) -> None:
    resp = await wired.client.post(
        "/documents",
        files={"file": ("empty.md", b"", CONTENT_TYPE)},
    )

    # An empty upload is a caller error we can know synchronously -> reject BEFORE the
    # 202, and before any row is written or any job is kicked.
    assert resp.status_code == 400

    async with wired.factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(Document))
        ).scalar_one()
    assert count == 0
    assert wired.recorder.calls == []


async def test_poll_returns_status_for_known_document(wired: SimpleNamespace) -> None:
    created = await wired.client.post(
        "/documents",
        files={"file": (FILENAME, BODY, CONTENT_TYPE)},
    )
    doc_id = created.json()["id"]

    resp = await wired.client.get(f"/documents/{doc_id}")

    # The poll the 202-and-poll contract depends on: it reflects the row's status.
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == doc_id
    assert payload["status"] == "pending"
    assert payload["filename"] == FILENAME
    assert payload["error"] is None


async def test_poll_unknown_document_is_404(wired: SimpleNamespace) -> None:
    resp = await wired.client.get(f"/documents/{uuid.uuid4()}")
    assert resp.status_code == 404
