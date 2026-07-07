"""M6 — orchestrate: run one document through the whole ingestion pipeline.

This is the conductor. M1–M5 each do one thing and were tested in isolation; M6 is
the first code that runs them *together* against a real document, and the first thing
that actually persists retrievable `chunks` rows end to end:

    load path (M1 open_local) -> parse (M2) -> chunk (M3) -> embed (M4) -> write (M5)

and, around that chain, it owns the two things no single stage can:

  1. **The `documents.status` lifecycle.** A row starts `pending` (inserted by whoever
     kicked the job — the future M7 upload endpoint). M6 walks it `pending ->
     processing` (work started, observable to a poller), then `-> ready` (chunks
     committed) or `-> failed` (with the reason in `documents.error`).
  2. **The transaction boundary.** M5's `write_chunks` deliberately only *flushes*; M6
     is the caller that commits, so the chunk rows and `status='ready'` land in ONE
     commit — a failure anywhere leaves nothing half-written, no doc falsely ready.

Job-shaped (the guardrail that makes "Celery later = no rewrite"):
    `orchestrate(document_id)` takes only an id, opens its own session (no caller hands
    it one), returns an `IngestResult` a synchronous caller does NOT wait on (the client
    polls `status` instead), and never raises for an *expected* processing failure — it
    records it. Turning this into a background task later is one line: `orchestrate(id)`
    -> `orchestrate.delay(id)`; the endpoint and client contract do not change.

Why the collaborators are module-level names (SessionLocal, get_storage, embed_texts):
    They're imported here as globals rather than reached through their packages at each
    call site so a test can monkeypatch this module's copy (point SessionLocal at a test
    container, swap embed_texts for a fake) — the seam M4's tests use on its client. It
    costs nothing in production and makes the whole pipeline drivable offline.

Three short transactions, never one held across the slow work:
    The claim (Txn 1) commits on its own so `processing` is visible *and* so we don't
    hold a DB connection open across the tens of seconds a hi_res parse + embed can take
    (that would tie up a pooled connection the rest of the system needs). The heavy
    stages then run with NO transaction open. Only when vectors are in hand do we open
    the results transaction (Txn 2) to write chunks + flip to ready atomically. On
    failure a THIRD, fresh transaction records `failed` — it must be its own transaction
    because the results one is already poisoned and rolled back, and the failure record
    has to survive that rollback.

Deferred (known, not solved here):
    A hard crash *between* Txn 1 and Txn 2 (process killed mid-parse) leaves the row
    stuck in `processing` forever — there's no reaper yet. That belongs with the Celery
    move (a visibility-timeout / requeue), out of scope for the naive-first pipeline.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.chunk import chunk_document
from app.ingest.embed import as_retrieval_document, embed_texts
from app.ingest.parse import parse_document
from app.ingest.write import write_chunks
from app.models import Document
from app.storage import get_storage

if TYPE_CHECKING:
    from unstructured.documents.elements import Element

logger = logging.getLogger("app.ingest.orchestrate")


@dataclass(frozen=True)
class IngestResult:
    """What one ingestion job produced — for the job's own logging/metrics and its test.

    A synchronous caller doesn't block on this (the client polls `documents.status`);
    it's returned so a worker can log the outcome and so eval can accumulate
    ingestion-side metrics (the "eval is a substrate" rule — M6 must not run silently).
    `timings_ms` maps each stage name to its wall-clock milliseconds.
    """

    document_id: uuid.UUID
    status: str  # "ready" | "failed"
    chunk_count: int
    timings_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None


def _ms(since: float) -> float:
    """Milliseconds elapsed since a `perf_counter()` reading, rounded for logs."""
    return round((perf_counter() - since) * 1000, 1)


async def orchestrate(document_id: uuid.UUID) -> IngestResult:
    """Ingest the `pending` document identified by `document_id`, end to end.

    Reads the row's `source_uri`, runs parse -> chunk -> embed -> write, and drives
    `documents.status` to `ready` (chunks committed) or `failed` (reason in `error`).
    Returns an `IngestResult`; an *expected* processing failure is recorded and
    returned, not raised. Raises `LookupError` only if no row exists for `document_id`
    (a lost-row / caller bug — there is nothing to mark failed).
    """
    settings = get_settings()
    storage = get_storage()
    timings: dict[str, float] = {}
    started = perf_counter()

    async with SessionLocal() as session:
        # --- Txn 1: claim the document --------------------------------------------
        # Load the row, capture where its bytes live, and flip pending -> processing.
        # Commit immediately: this makes progress observable to a poller and frees the
        # connection so the slow parse/embed below never holds a transaction open.
        doc = await session.get(Document, document_id)
        if doc is None:
            raise LookupError(f"orchestrate: no document with id {document_id}")
        source_uri = doc.source_uri
        doc.status = "processing"
        # Clear any error from a previous failed attempt (retry-friendly).
        doc.error = None
        await session.commit()

        try:
            # --- Slow work: NO transaction held -----------------------------------
            # open_local hands parse a real on-disk path (local: zero copy; S3 later:
            # a temp spill) without ever loading the blob into memory here.
            t = perf_counter()
            async with storage.open_local(source_uri) as path:
                elements = await parse_document(
                    path, strategy=settings.ingest_pdf_strategy
                )
            timings["parse_ms"] = _ms(t)

            t = perf_counter()
            chunks: list[Element] = await chunk_document(
                elements,
                max_characters=settings.ingest_chunk_max_characters,
                combine_text_under_n_chars=(
                    settings.ingest_chunk_combine_text_under_n_chars
                ),
            )
            timings["chunk_ms"] = _ms(t)

            # The `embed_text` seam: today we embed each chunk's own content, wrapped
            # in the RETRIEVAL_DOCUMENT role (title=None for now). A future enrich stage
            # would substitute a summary string here for table/image chunks — M4/M5
            # unchanged, because they only ever see final text / finished vectors.
            embed_inputs = [as_retrieval_document(c.text or "") for c in chunks]
            t = perf_counter()
            embeddings = await embed_texts(embed_inputs)
            timings["embed_ms"] = _ms(t)

            # --- Txn 2: results, atomic -------------------------------------------
            # write_chunks flushes (assigns PKs, surfaces constraint errors) but does
            # not commit; setting status here and committing lands chunks + 'ready'
            # together, so a mid-write failure can never leave a half-ready document.
            t = perf_counter()
            rows = await write_chunks(session, document_id, chunks, embeddings)
            doc.status = "ready"
            await session.commit()
            timings["write_ms"] = _ms(t)

        except Exception as exc:  # noqa: BLE001
            # Any stage failure funnels here. The results transaction is poisoned; roll
            # it back, then record the failure in a FRESH transaction so 'failed' + the
            # reason survive that rollback. The blob is intentionally left in storage
            # (retry/debug) — its lifecycle belongs to the row's creator, not this job.
            await session.rollback()
            failed = await session.get(Document, document_id)
            if failed is not None:
                failed.status = "failed"
                failed.error = str(exc)
                await session.commit()
            timings["total_ms"] = _ms(started)
            logger.exception(
                "ingest.failed document_id=%s error=%s timings_ms=%s",
                document_id,
                exc,
                timings,
            )
            return IngestResult(
                document_id=document_id,
                status="failed",
                chunk_count=0,
                timings_ms=timings,
                error=str(exc),
            )

    timings["total_ms"] = _ms(started)
    if not rows:
        # A document that yields zero chunks still commits as 'ready' (naive-first), but
        # it's retrieval-invisible — flag it so a silent empty ingest is noticeable.
        logger.warning("ingest.empty document_id=%s produced 0 chunks", document_id)
    logger.info(
        "ingest.complete document_id=%s chunks=%d timings_ms=%s",
        document_id,
        len(rows),
        timings,
    )
    return IngestResult(
        document_id=document_id,
        status="ready",
        chunk_count=len(rows),
        timings_ms=timings,
    )
