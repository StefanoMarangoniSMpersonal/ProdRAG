"""M1 storage-seam tests: prove the save/load round-trip, uniqueness, and the
path-traversal guard. Each test uses pytest's `tmp_path` fixture as the storage root,
so nothing ever touches the real backend/storage/ directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.local import LocalDiskStorage


async def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    data = b"hello world"

    uri = await storage.save(data, "notes.txt")

    # Contract: a backend-qualified, relative URI (not an absolute machine path).
    assert uri.startswith("file://")
    assert ":" not in uri[len("file://") :]  # no drive-letter / absolute path leaked
    # The bytes come back exactly.
    assert await storage.load(uri) == data
    # And a real file exists on disk, under the tmp root, keeping the original name.
    assert (tmp_path / uri[len("file://") :]).read_bytes() == data
    assert uri.endswith("/notes.txt")


async def test_same_filename_no_collision(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)

    uri_a = await storage.save(b"first", "report.pdf")
    uri_b = await storage.save(b"second", "report.pdf")

    # Same human name, different keys (uuid prefix), and neither overwrites the other.
    assert uri_a != uri_b
    assert await storage.load(uri_a) == b"first"
    assert await storage.load(uri_b) == b"second"


async def test_load_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    # Plant a file outside the storage root that a traversal URI would try to reach.
    secret = tmp_path.parent / "secret.txt"
    secret.write_bytes(b"top secret")

    with pytest.raises(ValueError):
        await storage.load("file://../secret.txt")


async def test_load_rejects_foreign_scheme(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    with pytest.raises(ValueError):
        await storage.load("s3://bucket/key")


async def test_exists_reflects_presence(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    uri = await storage.save(b"data", "a.txt")

    assert await storage.exists(uri) is True
    # Well-formed URI pointing at nothing -> False (not an error).
    assert await storage.exists("file://deadbeef/gone.txt") is False
    # Foreign/traversing URI -> raises, same as load.
    with pytest.raises(ValueError):
        await storage.exists("s3://bucket/key")


async def test_delete_is_idempotent_and_removes_file(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    uri = await storage.save(b"data", "a.txt")

    await storage.delete(uri)
    assert await storage.exists(uri) is False
    # Deleting again is a no-op, not an error.
    await storage.delete(uri)
    # The uuid dir was pruned, so nothing is left under the root.
    assert not any(tmp_path.iterdir())


# --- open_local: hand M2 a real filesystem path (used by the M6 orchestrator) --------


async def test_open_local_yields_a_real_readable_path(tmp_path: Path) -> None:
    # M2 parse wants a PATH, not bytes. `open_local` is the seam M6 uses: for local disk
    # it yields the blob's actual on-disk path (zero copy). The bytes at that path must
    # be exactly what was saved, and the file must still exist inside the context.
    storage = LocalDiskStorage(root=tmp_path)
    data = b"# heading\n\nbody text"
    uri = await storage.save(data, "doc.md")

    async with storage.open_local(uri) as path:
        assert isinstance(path, Path)
        assert path.is_file()
        assert path.read_bytes() == data
        # The extension is preserved so the parser can route by format.
        assert path.suffix == ".md"
        # It's the real stored file under the root, not a temp copy (local backend).
        assert path.is_relative_to(tmp_path)


async def test_open_local_rejects_foreign_scheme(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    with pytest.raises(ValueError):
        async with storage.open_local("s3://bucket/key"):
            pass


async def test_open_local_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalDiskStorage(root=tmp_path)
    secret = tmp_path.parent / "secret.txt"
    secret.write_bytes(b"top secret")
    with pytest.raises(ValueError):
        async with storage.open_local("file://../secret.txt"):
            pass
