"""M7 — the ingestion entry point: upload a document, then poll its status.

This is the HTTP shell over the finished M6 orchestrator. Everything hard (parse ->
chunk -> embed -> write, the status lifecycle, the transaction boundary) already lives
in `orchestrate`; M7 only has to:

  1. Receive the uploaded bytes and store them (M1 storage seam).
  2. Insert a `documents` row as `pending`, filling the columns M6 deliberately left to
     the row's creator — `byte_size` / `checksum` / `content_type` — because M6 is
     path-only and never realizes the file into memory.
  3. Kick the ingestion job and return **202 Accepted + the doc id**, WITHOUT waiting
     for it. The client then polls `GET /documents/{id}` until `status` is ready/failed.

Why 202-and-poll instead of running the pipeline inline and returning the result:
    A hi_res parse + embed can take tens of seconds; holding the HTTP connection open
    that long invites proxy timeouts and pins a worker/connection for the whole job. 202
    ("accepted, work started, check back") is the standard shape for deferred work — and
    it's the SAME shape a Celery worker will have, so the future swap is one line
    (`background_tasks.add_task(orchestrate, id)` -> `orchestrate_task.delay(id)`) with
    no change to this endpoint or the client. The 202 is NOT a claim that ingestion
    succeeded; the real outcome lands in `documents.status`, read via the poll.

Why `BackgroundTasks` (not `asyncio.create_task` or an inline `await`):
    FastAPI runs a background task AFTER the response is flushed, tied to the request
    lifecycle — so the 202 returns promptly while `orchestrate` runs on the same event
    loop. `create_task` would be fire-and-forget (droppable on shutdown, no lifecycle
    hook); an inline `await` would block the response on the whole pipeline (the thing
    we are explicitly avoiding).

Why `orchestrate` and `get_storage` are module-level names:
    Imported here as globals so a test can monkeypatch THIS module's copy (swap
    `orchestrate` for a recorder, point storage at a tmp dir) — the same seam M4/M6
    tests use. `background_tasks.add_task(orchestrate, ...)` and `get_storage()` both
    read the name at call time, so the patched version is what runs.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.ingest.orchestrate import orchestrate
from app.models import Document
from app.storage import get_storage

router = APIRouter(tags=["documents"])


class DocumentCreatedResponse(BaseModel):
    """The 202 body: enough for the client to start polling. `status` is always
    `pending` here — the real state transitions happen in the background job."""

    id: uuid.UUID
    status: str


class DocumentStatusResponse(BaseModel):
    """The poll body: the current lifecycle state plus `error` (populated only when
    `status == 'failed'`) so the client can surface a reason without another call."""

    id: uuid.UUID
    status: str
    filename: str
    error: str | None = None


@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentCreatedResponse,
)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> DocumentCreatedResponse:
    """Persist an uploaded file, register it as `pending`, and kick ingestion.

    Returns 202 + the new document id immediately; the client polls
    `GET /documents/{id}` for the outcome.
    """
    # M7 is where the bytes are realized (M6 stays path-only). We need them in hand to
    # size + checksum the file and to hand to storage, so read the whole upload here.
    data = await file.read()
    if not data:
        # An empty upload is a caller error we can know synchronously — reject BEFORE
        # the 202, before writing a row or kicking a job. (Only the ingestion OUTCOME is
        # deferred to the poll; validation we can do now, we do now.)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    filename = file.filename or "file"
    storage = get_storage()
    source_uri = await storage.save(data, filename)

    # owner_id defaults to DEV_OWNER_ID and status to 'pending' (see app/models.py).
    doc = Document(
        filename=filename,
        content_type=file.content_type,
        byte_size=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
        source_uri=source_uri,
    )
    session.add(doc)
    # Commit so the row is durable BEFORE the job runs — orchestrate looks it up by id
    # in its own session, so a not-yet-committed row would be a lost-row LookupError.
    await session.commit()

    # Kick the pipeline after the 202 is sent. Celery-swap seam: replace this one line
    # with `orchestrate_task.delay(doc.id)` and nothing else changes.
    background_tasks.add_task(orchestrate, doc.id)

    return DocumentCreatedResponse(id=doc.id, status=doc.status)


@router.get(
    "/documents/{document_id}",
    response_model=DocumentStatusResponse,
)
async def get_document_status(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> DocumentStatusResponse:
    """Report a document's current ingestion status (the poll; 404 if unknown)."""
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    return DocumentStatusResponse(
        id=doc.id,
        status=doc.status,
        filename=doc.filename,
        error=doc.error,
    )
