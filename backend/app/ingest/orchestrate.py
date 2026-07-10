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

from sqlalchemy import func, update

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
    # "ready" | "failed" | "superseded" (a newer claim fenced this run out — it wrote
    # nothing) | "skipped" (the document wasn't 'pending' when claimed — a redundant
    # delivery). Only "ready"/"failed" are ever written to documents.status; the other
    # two are in-memory signals for the worker's log, never a row state.
    status: str
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
        # Load the row (to capture where its bytes live, and to fail loudly on a truly
        # missing id), then claim it with a SINGLE atomic UPDATE: pending -> processing
        # AND attempt = attempt + 1, returning the new token. The increment is done in
        # SQL (not read-then-write on the ORM object) so two workers racing to claim the
        # same row get DISTINCT tokens — the DB serializes the increments. That token,
        # `my_attempt`, is this run's fencing key: only the holder of the current
        # attempt may later write results, so a reaper can requeue a presumed-dead
        # worker without the superseded run corrupting the winner. Commit at once so
        # `processing` is observable to a poller and no transaction is held across the
        # slow work below.
        doc = await session.get(Document, document_id)
        if doc is None:
            raise LookupError(f"orchestrate: no document with id {document_id}")
        source_uri = doc.source_uri
        claim = await session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == "pending")
            .values(
                status="processing",
                attempt=Document.attempt + 1,
                error=None,  # clear any error from a previous failed attempt
                updated_at=func.now(),
            )
            .returning(Document.attempt)
        )
        my_attempt = claim.scalar_one_or_none()
        await session.commit()
        if my_attempt is None:
            # The row existed but wasn't 'pending' (already processing/ready/failed) — a
            # redundant delivery (e.g. a reaper requeue that raced a finishing run). Not
            # ours to run; step aside without touching it.
            logger.info(
                "ingest.skipped document_id=%s (not pending at claim)", document_id
            )
            timings["total_ms"] = _ms(started)
            return IngestResult(
                document_id=document_id,
                status="skipped",
                chunk_count=0,
                timings_ms=timings,
            )

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

            # --- Txn 2: results, atomic + FENCED ----------------------------------
            # The fence goes FIRST: flip to 'ready' only if attempt still equals the
            # token we claimed with. If a reaper requeued this doc and a newer worker
            # re-claimed it, attempt has moved past us -> 0 rows -> we're superseded:
            # roll back and write NOTHING (before write_chunks, so the loser can never
            # delete or overwrite the winner's rows). If we still hold the token, we own
            # the row lock this UPDATE took; write_chunks then flushes
            # chunks and we commit — chunks + 'ready' land together, so a mid-write
            # failure can never leave a half-ready document.
            t = perf_counter()
            won = await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.attempt == my_attempt)
                .values(status="ready", updated_at=func.now())
                .returning(Document.id)
            )
            if won.scalar_one_or_none() is None:
                await session.rollback()
                timings["total_ms"] = _ms(started)
                logger.info(
                    "ingest.superseded document_id=%s attempt=%s (results)",
                    document_id,
                    my_attempt,
                )
                return IngestResult(
                    document_id=document_id,
                    status="superseded",
                    chunk_count=0,
                    timings_ms=timings,
                )
            rows = await write_chunks(session, document_id, chunks, embeddings)
            await session.commit()
            timings["write_ms"] = _ms(t)

        except Exception as exc:  # noqa: BLE001
            # Any stage failure funnels here. The results transaction is poisoned; roll
            # it back, then record the failure in a FRESH transaction — FENCED on the
            # same token, so a superseded run that also errored won't clobber the
            # winner's 'ready' with a failure. Blob left in storage (retry/debug) —
            # its lifecycle belongs to the row's creator, not this job.
            await session.rollback()
            recorded = await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.attempt == my_attempt)
                .values(status="failed", error=str(exc), updated_at=func.now())
            )
            await session.commit()
            timings["total_ms"] = _ms(started)
            if recorded.rowcount == 0:
                # Superseded before we recorded the failure — not ours to report.
                logger.info(
                    "ingest.superseded document_id=%s attempt=%s (on failure)",
                    document_id,
                    my_attempt,
                )
                return IngestResult(
                    document_id=document_id,
                    status="superseded",
                    chunk_count=0,
                    timings_ms=timings,
                )
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
