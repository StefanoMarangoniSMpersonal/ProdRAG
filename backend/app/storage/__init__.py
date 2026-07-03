"""Storage package — the blob-store seam and its accessor.

Downstream modules (M2 parse, M6 orchestrator) should import `get_storage()` and the
`Storage` type only, never the concrete `LocalDiskStorage`. That keeps every caller
backend-agnostic: switching to S3 becomes a one-line change here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.storage.base import Storage
from app.storage.local import LocalDiskStorage

__all__ = ["Storage", "LocalDiskStorage", "get_storage"]


@lru_cache
def get_storage() -> Storage:
    """Return the process-wide storage backend, chosen by config.

    Cached (same pattern as `get_settings()`) so the root path is resolved once. Today
    it always returns local disk; when S3 lands, this is where the selection happens.
    """
    settings = get_settings()
    return LocalDiskStorage(root=Path(settings.storage_dir))
