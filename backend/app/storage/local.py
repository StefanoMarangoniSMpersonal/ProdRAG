"""Local-disk implementation of the `Storage` seam (dev default).

Writes raw ingested files under a configured root directory. Chosen for Phase 1 so
we can build the whole ingestion pipeline end to end without an S3 dependency; the
`Storage` Protocol means swapping in `S3Storage` later touches no caller.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

_SCHEME = "file://"

# Collapse anything that isn't a safe filename char to "_". This strips path
# separators (/ \), "..", drive letters, and control chars — so a hostile or messy
# `filename` can't influence *where* on disk we write. Uniqueness comes from the uuid
# prefix, not the name, so squashing the name is purely cosmetic/safety, never lossy.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(filename: str) -> str:
    # Take only the final path component, then sanitize. `Path(...).name` discards any
    # directory parts a caller smuggled in (e.g. "../../etc/passwd" -> "passwd").
    name = Path(filename).name
    name = _UNSAFE.sub("_", name).strip("._")
    return name or "file"


class LocalDiskStorage:
    """Stores blobs as files under `root`, keyed by `<uuid4>/<safe-filename>`.

    The uuid dir guarantees two uploads named `report.pdf` never collide; keeping the
    original (sanitized) name as the leaf makes the store browsable while debugging.
    """

    def __init__(self, root: Path) -> None:
        # Resolve once to an absolute path so later traversal checks compare like with
        # like regardless of the process's current working directory.
        self._root = root.resolve()

    async def save(self, data: bytes, filename: str) -> str:
        key = f"{uuid4()}/{_safe_name(filename)}"
        dest = self._root / key
        # Offload the blocking filesystem work to a thread so we honour the project's
        # "async everywhere" rule without stalling the event loop.
        await asyncio.to_thread(self._write, dest, data)
        return f"{_SCHEME}{key}"

    async def load(self, uri: str) -> bytes:
        path = self._resolve(uri)
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, uri: str) -> bool:
        path = self._resolve(uri)  # raises on foreign/traversing URI
        return await asyncio.to_thread(path.is_file)

    async def delete(self, uri: str) -> None:
        path = self._resolve(uri)
        await asyncio.to_thread(self._remove, path)

    @asynccontextmanager
    async def open_local(self, uri: str) -> AsyncIterator[Path]:
        # Local disk already holds the blob at a real path, so this is zero-copy:
        # resolve the URI (which also runs the foreign-scheme + traversal guard, raising
        # before we yield) and hand back the on-disk path. There's nothing to spill or
        # clean up on exit — the file lives here until `delete`. A future S3Storage
        # would instead download to a NamedTemporaryFile inside a `try`/`finally` and
        # unlink it after the `yield`; callers never see the difference.
        path = self._resolve(uri)
        yield path

    # --- sync helpers (run inside asyncio.to_thread) ------------------------------

    @staticmethod
    def _write(dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def _remove(self, path: Path) -> None:
        # missing_ok=True makes delete idempotent (per the Storage contract).
        path.unlink(missing_ok=True)
        # Each blob owns a `<uuid4>/` dir, so once the file is gone that dir is empty
        # junk — prune it, but never climb to or past the storage root itself.
        parent = path.parent
        if parent != self._root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def _resolve(self, uri: str) -> Path:
        """Turn a `file://<key>` URI back into an absolute path under `root`, refusing
        anything that isn't ours or that escapes the root (path-traversal guard)."""
        if not uri.startswith(_SCHEME):
            raise ValueError(f"Not a local storage URI: {uri!r}")
        key = uri[len(_SCHEME) :]
        path = (self._root / key).resolve()
        # `is_relative_to` (3.9+) is True only if `path` sits inside `root`. A URI
        # carrying "../" would resolve outside and is rejected here rather than read.
        if not path.is_relative_to(self._root):
            raise ValueError(f"URI escapes storage root: {uri!r}")
        return path
