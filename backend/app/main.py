"""FastAPI application entrypoint.

Phase 0 exposes two health endpoints:
  - GET /health     : stateless liveness (is the API process up?)
  - GET /health/db  : proves the DB layer end-to-end (Postgres reachable +
                      pgvector extension installed).

No RAG, auth, or background work yet — this is just the spine.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import router as ask_router
from app.api.documents import router as documents_router
from app.config import get_settings
from app.db import get_session
from app.retrieve import rerank

settings = get_settings()

_startup_log = logging.getLogger("app.startup")

# Give OUR loggers a handler. uvicorn configures only the `uvicorn.*` loggers, so
# without this an `app.*` logger falls back to Python's lastResort handler — which is
# WARNING-level, meaning every logger.info() (including Q10's per-query `ask.query`
# record, the whole point of the milestone) is silently dropped in the real server. It
# was invisible in tests because pytest's caplog attaches its own handler. `force=True`
# so we own the root config regardless of import order; uvicorn's own loggers don't
# propagate to root, so nothing is double-printed.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """P0 — warm the reranker once at startup so the first query isn't cold.

    The cross-encoder (`rerank._get_reranker`, an `@lru_cache` loader) imports torch and
    loads a ~90 MB model on first use — ~24.8 s if that happens on the first request,
    ~2.3 s once warm. We pay that cost here instead, before any request arrives.

    Gated on `rerank_enabled` (read FRESH from `get_settings()`, not the import-time
    `settings`, so it stays monkeypatchable in tests): when rerank is OFF (the default)
    we do NOTHING, preserving the deliberate laziness that keeps torch out of the default
    path and the offline suite. The load runs via `asyncio.to_thread` so the CPU-bound
    import+load never blocks the event loop.

    Warm-up is BEST-EFFORT: any failure (e.g. no network to fetch the weights) is logged
    and swallowed — startup continues and the first live query merely pays the cold cost,
    exactly as before P0. A latency optimisation must never turn into a boot failure.
    """
    if get_settings().rerank_enabled:
        try:
            await asyncio.to_thread(rerank._get_reranker)
            _startup_log.info("reranker warm-up complete (rerank enabled)")
        except Exception:  # best-effort: never let warm-up crash startup
            _startup_log.exception(
                "reranker warm-up failed; first query will load it lazily"
            )
    else:
        _startup_log.info("reranker warm-up skipped (rerank disabled)")

    yield
    # No shutdown work: the lru_cache dies with the process.


app = FastAPI(title="ProdRAG API", version="0.0.1", lifespan=lifespan)

# Allow the Next.js dev server to call us from the browser. Without this the
# frontend fetch would be blocked by the browser's same-origin policy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ingestion HTTP surface (M7): POST /documents (upload) + GET /documents/{id} (poll).
app.include_router(documents_router)

# Query HTTP surface (Q10): POST /ask — request-shaped (the answer comes back inline),
# the deliberate contrast with job-shaped ingestion above.
app.include_router(ask_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check — does not touch any dependency."""
    return {"status": "ok"}


@app.get("/health/db")
async def health_db(session: AsyncSession = Depends(get_session)) -> dict:
    """Readiness check that actually exercises Postgres + pgvector.

    Proves the full API -> DB path: we can open a session, run a query, and the
    `vector` extension we need for embeddings is present.
    """
    version = (await session.execute(text("SELECT version()"))).scalar_one()

    pgvector_version = (
        await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    ).scalar_one_or_none()

    return {
        "status": "ok",
        "postgres_version": version,
        "pgvector_installed": pgvector_version is not None,
        "pgvector_version": pgvector_version,
    }
