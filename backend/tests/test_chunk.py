"""M3 chunk tests: typed elements -> retrieval-sized chunks (Unstructured by_title).

This is the *immutable spec* for M3 (CLAUDE.md "test-first & test-immutable"): it was
written and watched fail (red) before `app/ingest/chunk.py` existed. Once written it
does not change to accommodate the code — the code bends to it.

Two things are pinned here:
  1. `by_title` actually groups elements into fewer, size-capped chunks (the markdown
     fixture — text-only, so no system binaries; can't SKIP into a false green).
  2. The isolated `Table` chunk KEEPS its `metadata.text_as_html` (the M3 requirement:
     the grid M2 preserved must survive chunking, not just parsing). Proving that needs
     a real table under `hi_res`, so `test_chunk_pdf_table_keeps_text_as_html` REQUIRES
     `poppler` + `tesseract` on PATH and ERRORs loudly (never SKIPs) if they're missing
     — the same deliberate trade the M2 PDF test makes.

The hard cap asserted below is the literal 1500 (the M3 architect decision), passed
explicitly to `chunk_document` so the spec never depends on a config value that could
drift.
"""

from __future__ import annotations

from pathlib import Path

from app.ingest.chunk import chunk_document
from app.ingest.parse import parse_document

FIXTURES = Path(__file__).parent / "fixtures"

MAX_CHARACTERS = 1500
COMBINE_UNDER = 500


async def test_chunk_by_title_groups_and_caps() -> None:
    # Parse the markdown corpus into elements, then chunk them. by_title should merge
    # the many small elements into fewer, section-shaped chunks -- none over the cap.
    data = (FIXTURES / "rag_test_document.md").read_bytes()
    elements = await parse_document(data, "rag_test_document.md")

    chunks = await chunk_document(
        elements,
        max_characters=MAX_CHARACTERS,
        combine_text_under_n_chars=COMBINE_UNDER,
    )

    # Chunking produced something, and it grouped -- fewer chunks than source elements
    # (a pass-through would return one chunk per element).
    assert len(chunks) > 0
    assert len(chunks) < len(elements)

    # The hard cap holds: no chunk's text exceeds max_characters. This is the guarantee
    # that keeps every chunk under the embedding model's input limit downstream.
    for ch in chunks:
        assert len(ch.text) <= MAX_CHARACTERS

    # A stable fact from the fixture survives chunking (guards a chunker that returns
    # chunks but drops/garbles their text).
    full_text = "\n".join(ch.text for ch in chunks)
    assert "Aurelia Robotics" in full_text


async def test_chunk_pdf_table_keeps_text_as_html() -> None:
    # The M3 requirement: after chunking, the isolated Table chunk must still carry the
    # HTML grid (metadata.text_as_html) that preserves cell<->header links. A chunker
    # that dropped it here would undo everything M2's hi_res table path bought us.
    data = (FIXTURES / "quarterly_report.pdf").read_bytes()
    elements = await parse_document(data, "quarterly_report.pdf", strategy="hi_res")

    chunks = await chunk_document(
        elements,
        max_characters=MAX_CHARACTERS,
        combine_text_under_n_chars=COMBINE_UNDER,
    )

    # Find the chunk(s) carrying a structural HTML rendering -- the isolated table.
    html_chunks = [ch for ch in chunks if ch.metadata.text_as_html]
    assert html_chunks, "expected a chunk carrying metadata.text_as_html (the table)"

    text_as_html = html_chunks[0].metadata.text_as_html
    assert text_as_html is not None
    assert "<table" in text_as_html

    # The hard cap still holds across the real document.
    for ch in chunks:
        assert len(ch.text) <= MAX_CHARACTERS
