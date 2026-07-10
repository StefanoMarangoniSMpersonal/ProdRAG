"""M7 — the ingestion entry point: upload a document, then poll its status.

This is the HTTP shell over the finished M6 orchestrator. Everything hard (parse ->
chunk -> embed -> write, the status lifecycle, the transaction boundary) already lives
in `orchestrate`; M7 only has to:

  1. Receive the uploaded bytes and store them (M1 storage seam).
  2. Insert a `documents` row as `pending`, filling the columns M6 deliberately left to
     the row's creator — `byte_size` / `checksum` / `content_type` — because M6 is
     path-only and never realizes the file into memory.
  3. Enqueue the ingestion job and return **202 Accepted + the doc id**, WITHOUT waiting
     for it. The client then polls `GET /documents/{id}` until `status` is ready/failed.

Why 202-and-poll instead of running the pipeline inline and returning the result:
    A hi_res parse + embed can take tens of seconds; holding the HTTP connection open
    that long invites proxy timeouts and pins a worker/connection for the whole job. 202
    ("accepted, work started, check back") is the standard shape for deferred work. The
    202 is NOT a claim that ingestion succeeded; the real outcome lands in
    `documents.status`, read via the poll.

Why Celery `.delay` (the enqueue):
    `orchestrate_task.delay(str(doc.id))` serializes the id onto Redis and returns at
    once; a separate worker process pulls it and runs `orchestrate`. The handler never
    touches the pipeline, so the 202 is bounded by a blob write + one row insert. (This
    replaced an interim FastAPI `BackgroundTasks` kick; the job was job-shaped, so
    the swap was one line, with no change to the endpoint's contract or the poll.)

Why `orchestrate_task` and `get_storage` are module-level names:
    Imported here as globals so a test can monkeypatch THIS module's copy (swap
    `orchestrate_task.delay` for a recorder, point storage at a tmp dir) — the same seam
    the M4/M6/upload tests use. `orchestrate_task.delay(...)` and `get_storage()` both
    read the name at call time, so the patched version is what runs.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Document
from app.storage import get_storage
from app.worker import orchestrate_task

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

    # Enqueue the job onto Redis; a worker process runs it. The id is serialized as a
    # string (JSON broker) and re-parsed to a UUID in the task. The endpoint returns
    # without waiting — the outcome lands in documents.status, read via the poll.
    orchestrate_task.delay(str(doc.id))

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
