"""The storage seam — the contract every blob backend must satisfy.

M1 of the ingestion pipeline. This module answers exactly one question: "where do
raw uploaded bytes physically live, and how do I address them later?" — and it
answers it *behind an interface*, so callers can't tell local disk from S3.

Why a `Protocol` and not an abstract base class (ABC): `Protocol` is *structural*
typing — "any object with a `save` and a `load` of these signatures IS a Storage",
with no inheritance required. That means the future `S3Storage` doesn't have to
import and subclass anything from here; it just needs matching methods. It keeps the
concrete backends decoupled from the contract. (An ABC would force `S3Storage(Storage)`,
a nominal parent link we don't need.)

The URI contract (opaque to callers): `save` returns a string like
`file://<key>` where `<key>` is a path *relative to the backend's storage root*, not
an absolute machine path. `load` accepts a URI this same backend produced and returns
the exact bytes. Because the scheme (`file://`, later `s3://`) identifies the backend
and the rest is a relative key, `documents.source_uri` never stores a machine-specific
absolute path — S3 slots in later as a new scheme with no schema migration.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Storage(Protocol):
    """A blob store for raw ingested files. Async so one contract covers both the
    local (thread-offloaded) and future S3 (natively async) implementations."""

    async def save(self, data: bytes, filename: str) -> str:
        """Persist `data` under a fresh, collision-free key derived from `filename`,
        and return its URI. `filename` is the human-supplied name; it is used only for
        readability/extension on disk, never for addressing (the key is unique)."""
        ...

    async def load(self, uri: str) -> bytes:
        """Return the exact bytes previously stored under `uri`. Raises if the URI was
        not produced by this backend or resolves outside the storage root."""
        ...

    async def exists(self, uri: str) -> bool:
        """Whether a blob is currently stored at `uri`. Raises (not returns False) if
        the URI isn't one this backend could have produced — a malformed/foreign URI is
        a caller bug, distinct from a well-formed URI that simply points at nothing."""
        ...

    async def delete(self, uri: str) -> None:
        """Remove the blob at `uri`. Idempotent: deleting an already-absent blob is a
        no-op, not an error (matches S3 delete semantics, so failure-path cleanup in the
        orchestrator can call it blindly). Still raises on a foreign/traversing URI."""
        ...

    def open_local(self, uri: str) -> AbstractAsyncContextManager[Path]:
        """Yield a real local filesystem `Path` for `uri`, for the whole `async with`.

        The parse stage (M2) needs an actual file on disk (poppler/pdfminer read a path,
        and the extension routes the partitioner) — never bytes in memory. `open_local`
        is the seam that gives the orchestrator (M6) that path *without* it ever loading
        the blob into RAM: the local backend yields the blob's own on-disk path (zero
        copy, nothing to clean up); a future S3 backend would stream the object to a
        temp file, yield that, and delete it on exit. Callers stay backend-agnostic.

        Raises (before yielding) on a foreign/traversing URI, exactly like `load`.
        """
        ...
