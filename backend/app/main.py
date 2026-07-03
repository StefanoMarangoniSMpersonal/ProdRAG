"""FastAPI application entrypoint.

Phase 0 exposes two health endpoints:
  - GET /health     : stateless liveness (is the API process up?)
  - GET /health/db  : proves the DB layer end-to-end (Postgres reachable +
                      pgvector extension installed).

No RAG, auth, or background work yet — this is just the spine.
"""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session

settings = get_settings()

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
