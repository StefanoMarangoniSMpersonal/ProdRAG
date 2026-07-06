"""M3 — chunk: typed *elements* -> retrieval-sized *chunks*, via Unstructured by_title.

This is the second stage of the ingestion pipeline. It takes the list of typed
elements M2 (`parse_document`) produced and groups them into **chunks** — the pieces
we will embed (M4) and store (M5). One chunk is the unit the whole retrieval side is
built around.

Where the seam is (element vs. chunk):
    An *element* is one structural unit Unstructured detected — a heading, a paragraph,
    a table. A *chunk* is a retrieval-sized grouping of those elements. `chunk_by_title`
    walks the elements in order and:
      - starts a new chunk whenever it hits a Title (section heading),
      - merges consecutive runt sections shorter than `combine_text_under_n_chars` (a
        remedy for lines mis-detected as Titles — the main over-chunking knob),
      - caps every chunk at `max_characters` (a hard max; oversized elements are split),
      - keeps Tables isolated in their own chunk (`isolate_table=True`, the default).
    Output is still Unstructured `Element` objects (`CompositeElement` for grouped text,
    `Table`/`TableChunk` for tables) — so parse and chunk both speak Unstructured's
    native type, and we only translate into our own `Chunk` ORM model at the write stage
    (M5). Keeping the third-party type contained to parse+chunk is the "library stays
    thin" rule.

Table structure survives chunking (the M3 requirement):
    A `Table` element carries its grid as `metadata.text_as_html` (M2 populates this
    under hi_res via `infer_table_structure=True`). Because tables are isolated into
    their own chunk and `max_characters` caps `text_as_html` too, the HTML rides through
    onto the table chunk unchanged (a table only larger than the cap would be split into
    `TableChunk`s, each still HTML). M5 stores that HTML in `chunks.metadata` (JSONB);
    it never becomes the embedded text directly.

`include_orig_elements=True` (Unstructured default, kept): each chunk keeps its source
elements in `metadata.orig_elements`, so M5 can mine per-element metadata (section
title, page number) that chunk-level consolidation would otherwise drop.

Async note: `chunk_by_title` is pure in-memory Python (no I/O, no ML) — fast today. We
still run it via `asyncio.to_thread` and expose an `await`-able `chunk_document`,
matching `parse_document`. That is deliberate interface-shaping, not a performance need:
a future heavier chunking strategy (`by_similarity` runs an embedding model to find
semantic boundaries) drops in behind the exact same `await chunk_document(...)` seam
without reshaping the M6 orchestrator's call site — the project's "build with scaling in
mind" rule applied to the interface.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import (see parse.py): `from __future__ import annotations` turns every
    # annotation into a never-evaluated string, so `import app.ingest.chunk` stays
    # instant and Unstructured is imported lazily, only when a chunk actually runs.
    from unstructured.documents.elements import Element

# Pipeline defaults for the two by_title knobs. The M6 orchestrator passes the values
# from Settings (env-overridable); these module constants are the convenience default
# for library callers (e.g. /ingest-inspect). The immutable M3 test passes its cap
# explicitly, so it never depends on these.
DEFAULT_MAX_CHARACTERS = 1500
DEFAULT_COMBINE_TEXT_UNDER_N_CHARS = 500


async def chunk_document(
    elements: list[Element],
    *,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
    combine_text_under_n_chars: int = DEFAULT_COMBINE_TEXT_UNDER_N_CHARS,
) -> list[Element]:
    """Group `elements` into retrieval-sized chunks with Unstructured's by_title.

    `max_characters` is the hard cap per chunk (applies to a table's `text_as_html`
    too); `combine_text_under_n_chars` merges runt sections below that length. Returns
    the chunks in document order as Unstructured `Element` objects (`CompositeElement` /
    `Table`).
    """
    return await asyncio.to_thread(
        _chunk_sync, elements, max_characters, combine_text_under_n_chars
    )


def _chunk_sync(
    elements: list[Element], max_characters: int, combine_text_under_n_chars: int
) -> list[Element]:
    """The blocking `chunk_by_title` call, run inside a worker thread via
    `asyncio.to_thread`. `chunk_by_title` raises `ValueError` itself if
    `combine_text_under_n_chars > max_characters`, so we don't re-validate that."""
    # Lazy import (see the TYPE_CHECKING note above): the chunking module is light, but
    # we import inside the call to mirror parse.py and hold `import chunk` free.
    from unstructured.chunking.title import chunk_by_title

    return chunk_by_title(
        elements,
        max_characters=max_characters,
        combine_text_under_n_chars=combine_text_under_n_chars,
        # include_orig_elements=True and isolate_table=True are Unstructured defaults
        # and exactly what we want (see module docstring); passed by omission.
    )
