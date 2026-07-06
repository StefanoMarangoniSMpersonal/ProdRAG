"""M5 — write: chunk *elements* + vectors -> persisted `chunks` rows.

This is the fourth and final stage of the ingestion pipeline's core, and it is the
**translation boundary**. parse (M2) and chunk (M3) both speak Unstructured's native
`Element` type; embed (M4) speaks plain vectors; everything downstream (retrieval,
Phase 2) speaks our own `Chunk` ORM row. `write_chunks` is the single place an
`Element` + its 768-dim vector becomes a `chunks` row in Postgres. Keeping the
Unstructured type contained to parse+chunk+write (never leaking it into the DB or
retrieval layers) is the "keep the third-party library thin" rule in practice.

Two aligned lists, not one (the enrich seam):
    The caller passes `chunks` (the elements) AND `embeddings` (the vectors) as two
    index-aligned lists — never a single "embed the content" call inside here. Today
    the embedded text equals each chunk's content, but the deferred enrich stage will,
    for a table/image chunk, embed an LLM-written *summary* while the raw content is
    still what we store and cite. Storing `content` from the element and `embedding`
    from a separate list means that divergence needs zero change here: M6 resolves the
    `embed_text` seam upstream and hands us finished vectors. A length mismatch between
    the two lists would silently mis-pair each vector with the wrong text — the worst
    corruption possible — so it is a hard, first-line failure.

No commit (M6 owns the transaction):
    write_chunks `flush`es (sends the INSERTs, assigns the bigint PKs, and surfaces any
    constraint violation) but deliberately does NOT commit. The orchestrator (M6) owns
    the transaction boundary so the chunk-write and the `documents.status = 'ready'`
    update commit together — a failure anywhere leaves nothing half-written and the doc
    never falsely marked ready.

Metadata mining (element -> row): `content`/`char_count` from the element's text;
`element_type` from its Python type name (`CompositeElement`, `Table`, …);
`section_title` from the first `Title` among the chunk's `metadata.orig_elements` (the
source elements Unstructured keeps on each chunk); `page_number` from the element
metadata; and a table's `metadata.text_as_html` grid into the row's `metadata` JSONB
(never the embedded text). `token_count` stays NULL — no tokenizer wired yet (deferred).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.models import DEV_OWNER_ID, Chunk

if TYPE_CHECKING:
    # Type-only imports (see parse.py's note): keep `import app.ingest.write` free of
    # the heavy Unstructured graph and a hard SQLAlchemy-async import at module load.
    from sqlalchemy.ext.asyncio import AsyncSession
    from unstructured.documents.elements import Element


def _section_title(element: Element) -> str | None:
    """The chunk's section heading, if any: the text of the first `Title` element among
    the chunk's source elements (`metadata.orig_elements`, which chunk M3 keeps via
    `include_orig_elements=True`). Chunk-level consolidation drops the standalone Title,
    so mining it back out here is the only way to recover it. Matches on the type *name*
    (not an isinstance import) to keep this module Unstructured-free at import time."""
    orig_elements = getattr(element.metadata, "orig_elements", None) or []
    for el in orig_elements:
        if type(el).__name__ == "Title":
            return el.text
    return None


def _chunk_to_row(
    element: Element,
    ordinal: int,
    embedding: list[float],
    document_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> Chunk:
    """Map one chunk element + its vector into a `Chunk` ORM row (pure — no DB touch).
    The mining logic lives here so it can be reasoned about and tested on its own."""
    text = element.text or ""
    metadata = element.metadata
    # A Table chunk carries its grid HTML here (populated by M2 under hi_res); prose
    # chunks don't. Store it in the JSONB catch-all when present, else an empty object
    # (the column is NOT NULL DEFAULT '{}').
    text_as_html = getattr(metadata, "text_as_html", None)

    return Chunk(
        document_id=document_id,
        owner_id=owner_id,
        ordinal=ordinal,
        content=text,
        embedding=embedding,
        char_count=len(text),
        token_count=None,  # deferred — no tokenizer wired yet
        element_type=type(element).__name__,
        section_title=_section_title(element),
        page_number=getattr(metadata, "page_number", None),
        metadata_={"text_as_html": text_as_html} if text_as_html else {},
    )


async def write_chunks(
    session: AsyncSession,
    document_id: uuid.UUID,
    chunks: list[Element],
    embeddings: list[list[float]],
    *,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> list[Chunk]:
    """Persist `chunks` (M3 elements) + their aligned `embeddings` (M4 vectors) as
    `Chunk` rows under `document_id`, in document order (0-based `ordinal`).

    `chunks[i]` is paired with `embeddings[i]`; the two lists MUST be the same length
    (a mismatch raises `ValueError` before any DB work). Adds every row and `flush`es —
    assigning PKs and surfacing constraint errors — but does NOT commit: the caller (M6)
    owns the transaction. Returns the persisted rows (with PKs populated).
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            "write_chunks: chunks and embeddings must be the same length "
            f"({len(chunks)} chunks vs {len(embeddings)} embeddings) — they are paired "
            "by index, so a mismatch would mis-pair vectors with text."
        )

    rows = [
        _chunk_to_row(element, ordinal, embedding, document_id, owner_id)
        for ordinal, (element, embedding) in enumerate(
            zip(chunks, embeddings, strict=True)
        )
    ]
    session.add_all(rows)
    await session.flush()
    return rows
