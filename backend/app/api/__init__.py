"""HTTP API routers.

The FastAPI app (`app/main.py`) stays a thin assembly point: each area of the API lives
in its own router module here and is mounted via `app.include_router(...)`. M7 adds the
first one (`documents` — upload + status poll); `ask`/`list` will follow in Phase 2.
"""
