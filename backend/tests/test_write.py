"""M5 write tests: chunk *elements* + vectors -> persisted `chunks` rows.

This is the *immutable spec* for M5 (CLAUDE.md "test-first & test-immutable"): written
and watched fail (red) before `app/ingest/write.py` existed. Once written it does not
change to accommodate the code — the code bends to it.

What M5 is: the translation boundary. parse (M2) and chunk (M3) speak Unstructured's
native `Element`; retrieval speaks our own `Chunk` ORM row. `write_chunks` maps each
chunk element + its 768-dim vector into a `Chunk` row and persists it.

Test shape (see the plan): the DB tests hit a REAL Postgres via a throwaway
`testcontainers` pgvector container (the `db_session` fixture in conftest.py), so they
prove the actual schema round-trip (pgvector `vector(768)`, JSONB) — not a mock. They
ERROR loudly (never SKIP) if Docker isn't running, the same "a SKIP is a false green"
stance the PDF tests take on missing binaries.

Two deliberate isolations keep this a WRITE spec, not an end-to-end one:
  - **Synthetic vectors** (plain 768-float lists built here) — the write stage doesn't
    care where a vector came from, so we never call the paid Gemini API (M4) to test it.
  - **Hand-built Unstructured elements** — deterministic, no poppler/tesseract, and
    independent of parse/chunk internals. The full parse->chunk->embed->write proof
    belongs to M6's test, not here.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unstructured.documents.elements import (
    CompositeElement,
    Table,
    Title,
)

from app.ingest.write import write_chunks
from app.models import DEV_OWNER_ID, Chunk, Document

DIMS = 768


def _vec(seed: int) -> list[float]:
    """A deterministic, non-trivial 768-float vector. Its exact values don't matter to
    the write stage; distinct seeds just make vectors distinguishable if we inspect."""
    return [float((seed + i) % 5) - 2.0 for i in range(DIMS)]


def _composite_with_title(text: str, title: str, page: int) -> CompositeElement:
    """A grouped text chunk (what by_title emits for prose), carrying its source Title
    in metadata.orig_elements and a page number — the two fields M5 must mine."""
    el = CompositeElement(text=text)
    el.metadata.orig_elements = [Title(text=title)]
    el.metadata.page_number = page
    return el


def _table(text: str, text_as_html: str, page: int) -> Table:
    """An isolated Table chunk carrying its grid as metadata.text_as_html — M5 must land
    that HTML in the row's `metadata` JSONB and tag element_type "Table"."""
    el = Table(text=text)
    el.metadata.text_as_html = text_as_html
    el.metadata.page_number = page
    return el


async def _make_document(session: AsyncSession) -> uuid.UUID:
    """Insert a parent documents row (chunks.document_id is a FK) and return its id."""
    doc = Document(filename="spec.md", source_uri="file://spec-key/spec.md")
    session.add(doc)
    await session.flush()  # DB assigns the uuid PK
    return doc.id


# --- offline: no DB, pure guard ------------------------------------------------------


async def test_write_rejects_misaligned_lengths() -> None:
    # A vector list that doesn't line up with the chunk list would silently mis-pair
    # each vector to the wrong text — the worst possible corruption. It must fail fast,
    # before any DB work, so the guard is the first thing write_chunks does (session is
    # never touched, hence None here).
    chunks = [CompositeElement(text="a"), CompositeElement(text="b")]
    embeddings = [_vec(1)]  # one short

    with pytest.raises(ValueError):
        await write_chunks(None, uuid.uuid4(), chunks, embeddings)


# --- real Postgres (testcontainers): the round-trip spec -----------------------------


async def test_write_persists_rows_with_mined_metadata(
    db_session: AsyncSession,
) -> None:
    document_id = await _make_document(db_session)

    chunks = [
        _composite_with_title(
            "Section One\n\nBody of the first section.", "Section One", 1
        ),
        _composite_with_title(
            "Section Two\n\nBody of the second section.", "Section Two", 2
        ),
    ]
    embeddings = [_vec(1), _vec(2)]

    returned = await write_chunks(db_session, document_id, chunks, embeddings)

    # write_chunks returns one persisted row per input, PKs populated by the flush.
    assert len(returned) == 2
    assert all(r.id is not None for r in returned)

    # Force a real SELECT from the DB (not the identity-map cache) so the assertions
    # below prove what actually persisted through the pgvector/JSONB columns.
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(Chunk)
                .where(Chunk.document_id == document_id)
                .order_by(Chunk.ordinal)
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 2
    # ordinal is contiguous 0-based document position.
    assert [r.ordinal for r in rows] == [0, 1]

    first = rows[0]
    assert first.content == "Section One\n\nBody of the first section."
    assert first.char_count == len(first.content)
    assert first.section_title == "Section One"  # mined from orig_elements Title
    assert first.page_number == 1
    assert first.element_type == "CompositeElement"  # type(element).__name__
    assert first.owner_id == DEV_OWNER_ID
    assert first.document_id == document_id
    assert first.token_count is None  # deferred — no tokenizer yet

    # The embedding survived the pgvector vector(768) column as 768 numbers.
    assert len(list(first.embedding)) == DIMS


async def test_write_lands_table_html_in_metadata_jsonb(
    db_session: AsyncSession,
) -> None:
    document_id = await _make_document(db_session)

    html = "<table><thead><tr><th>Q</th><th>Rev</th></tr></thead></table>"
    chunks = [_table("Q Rev\nQ1 100", html, page=3)]
    embeddings = [_vec(7)]

    await write_chunks(db_session, document_id, chunks, embeddings)

    db_session.expire_all()
    row = (
        await db_session.execute(select(Chunk).where(Chunk.document_id == document_id))
    ).scalar_one()

    assert row.element_type == "Table"
    assert row.page_number == 3
    # The grid HTML rode into the JSONB catch-all (never the embedded text).
    assert row.metadata_["text_as_html"] == html


async def test_write_defaults_metadata_and_nullable_fields(
    db_session: AsyncSession,
) -> None:
    # A plain chunk with no Title in orig_elements and no page number: section_title and
    # page_number are NULL, and metadata is an empty object (not null) — the JSONB
    # column default shape.
    document_id = await _make_document(db_session)

    chunks = [CompositeElement(text="orphan text with no heading")]
    embeddings = [_vec(3)]

    await write_chunks(db_session, document_id, chunks, embeddings)

    db_session.expire_all()
    row = (
        await db_session.execute(select(Chunk).where(Chunk.document_id == document_id))
    ).scalar_one()

    assert row.section_title is None
    assert row.page_number is None
    assert row.metadata_ == {}
