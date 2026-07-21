"""FastAPI application entrypoint.

Phase 0 exposes two health endpoints:
  - GET /health     : stateless liveness (is the API process up?)
  - GET /health/db  : proves the DB layer end-to-end (Postgres reachable +
                      pgvector extension installed).

No RAG, auth, or background work yet — this is just the spine.
"""

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import router as ask_router
from app.api.documents import router as documents_router
from app.config import get_settings
from app.db import get_session

settings = get_settings()

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

app = FastAPI(title="ProdRAG API", version="0.0.1")

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
