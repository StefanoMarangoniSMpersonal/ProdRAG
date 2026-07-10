"""Celery worker: the async task layer over the M6 ingestion orchestrator.

The whole pipeline was built "job-shaped" (orchestrate takes only an id, opens its own
session, returns a value no caller awaits) precisely so this file could exist without
reshaping anything upstream. The upload endpoint's one-line seam
(`orchestrate_task.delay(str(id))`) hands a document id to Redis; a worker pulls it and
runs the same `orchestrate` coroutine the synchronous BackgroundTasks path used to.

Broker-only (no result backend):
    The client polls `documents.status` in Postgres for the outcome, and orchestrate's
    return value is never awaited — so Celery has nothing to store. Redis carries the
    queue and nothing else; Postgres stays the single source of truth for job state.

Sync task over an async pipeline (the bridge):
    A Celery task is a plain synchronous function, but `orchestrate` is a coroutine. We
    bridge with `asyncio.run(orchestrate(id))`, which spins up a fresh event loop per
    task, runs the pipeline to completion, and tears the loop down.

The async-engine-per-loop trap (why the worker rebinds SessionLocal):
    `app.db.engine` is created once at import with a normal connection pool. Reusing a
    pooled asyncpg connection across the *different* event loops that `asyncio.run`
    creates each task raises "Future attached to a different loop". So on worker startup
    we build a NullPool engine (every session opens and closes its own connection, bound
    to the current loop, never reused) and install it onto orchestrate's module-level
    `SessionLocal` seam — the same swap-point the tests monkeypatch. New connection per
    task is fine at this volume; the pooled per-worker Supabase/Supavisor URL is the
    documented later swap, and it plugs in right here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from celery import Celery
from celery.signals import worker_process_init
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.orchestrate import orchestrate
from app.models import Document

logger = logging.getLogger("app.worker")

settings = get_settings()

# The Celery app. Named `celery` so `celery -A app.worker worker` resolves it without an
# explicit attribute. broker = Redis; result backend left unset (broker-only).
celery = Celery("prodrag", broker=settings.celery_broker_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    timezone="UTC",
    enable_utc=True,
    # Beat runs the reaper on a fixed cadence (embedded via `worker -B` in dev). It
    # sweeps documents stuck in 'processing' — see reap_stuck_documents.
    beat_schedule={
        "reap-stuck-documents": {
            "task": "app.worker.reap_stuck_documents",
            "schedule": float(settings.ingest_reaper_interval_seconds),
        }
    },
)


@worker_process_init.connect
def _install_worker_sessionmaker(**_kwargs: object) -> None:
    """Rebind orchestrate's `SessionLocal` to a NullPool factory, once per worker.

    Fires only inside a real worker (not in eager-mode tests, which keep their own
    monkeypatched SessionLocal). See the module docstring for why NullPool is required
    when driving async SQLAlchemy from `asyncio.run` per task.
    """
    global SessionLocal
    from app.ingest import orchestrate as orch

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # Both the orchestrator (per-job) and the reaper (this module) must use the NullPool
    # factory, so install it on both seams.
    orch.SessionLocal = factory
    SessionLocal = factory
    logger.info("worker: installed NullPool SessionLocal")


@celery.task(name="app.worker.orchestrate_task")
def orchestrate_task(document_id: str) -> None:
    """Run one document through the ingestion pipeline. `document_id` arrives as a
    string (JSON serialization); parse it back to a UUID and drive the async
    orchestrator to completion. Returns nothing — the outcome lives in
    `documents.status`, read via the poll.
    """
    doc_id = uuid.UUID(document_id)
    result = asyncio.run(orchestrate(doc_id))
    logger.info("orchestrate_task done document_id=%s status=%s", doc_id, result.status)


async def _reap() -> int:
    """Recover documents stuck in 'processing' — the gap M6 deferred to the Celery move.

    A worker that dies between the claim (Txn 1) and the results commit (Txn 2) leaves a
    row 'processing' forever. This sweeps rows whose `updated_at` is older than
    `ingest_stuck_after_seconds` (presumed abandoned) and either:
      - re-enqueues them (flip back to 'pending', then `orchestrate_task.delay`), or
      - fails them if they've already hit the `ingest_max_processing_attempts` cap — a
        poison-pill guard so a doc that reliably crashes the worker can't loop forever.

    Safe against a too-eager timeout requeuing a still-alive worker: the requeued job's
    claim bumps `attempt`, and the fence in orchestrate makes the superseded original's
    results commit a no-op. So a false requeue only wastes work, never corrupts. Returns
    the number of stuck rows acted on. Async helper; `reap_stuck_documents` is the task.
    """
    cutoff = func.now() - timedelta(seconds=settings.ingest_stuck_after_seconds)
    requeue: list[uuid.UUID] = []
    async with SessionLocal() as session:
        stuck = (
            await session.execute(
                select(Document.id, Document.attempt).where(
                    Document.status == "processing",
                    Document.updated_at < cutoff,
                )
            )
        ).all()
        for doc_id, attempt in stuck:
            if attempt >= settings.ingest_max_processing_attempts:
                await session.execute(
                    update(Document)
                    .where(Document.id == doc_id, Document.status == "processing")
                    .values(
                        status="failed",
                        error=(f"reaper: exceeded max processing attempts ({attempt})"),
                        updated_at=func.now(),
                    )
                )
                logger.warning(
                    "reaper.failed document_id=%s attempt=%s", doc_id, attempt
                )
            else:
                flipped = await session.execute(
                    update(Document)
                    .where(Document.id == doc_id, Document.status == "processing")
                    .values(status="pending", updated_at=func.now())
                    .returning(Document.id)
                )
                if flipped.scalar_one_or_none() is not None:
                    requeue.append(doc_id)
        await session.commit()

    # Enqueue AFTER the commit so each row is durably 'pending' before a worker can
    # claim it (the claim gates on status='pending').
    for doc_id in requeue:
        orchestrate_task.delay(str(doc_id))
        logger.info("reaper.requeued document_id=%s", doc_id)
    return len(stuck)


@celery.task(name="app.worker.reap_stuck_documents")
def reap_stuck_documents() -> int:
    """Celery Beat entry point for the stuck-'processing' sweep (see `_reap`)."""
    return asyncio.run(_reap())
