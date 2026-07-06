"""M2 — parse: raw file bytes -> a list of typed *elements*, via Unstructured.

This is the first stage of the ingestion pipeline and it does exactly one thing:
hand the raw bytes to Unstructured's `partition`, which detects the format and splits
the document into **typed elements** — objects like `Title`, `NarrativeText`, `Table`,
`ListItem`, each carrying its text plus metadata (page number, coordinates, …).

Where the seam is (parse vs. chunk):
    An *element* is Unstructured's structural unit (one heading, one paragraph, one
    table). It is NOT yet a *chunk* — a chunk is a retrieval-sized piece we embed and
    store. Grouping elements into chunks (Unstructured's `by_title` strategy) is the
    NEXT stage, M3. Both stages operate on Unstructured's native `Element` objects, so
    parse returns them as-is; we only translate into our own `Chunk` ORM model at the
    write stage (M5). Keeping the Unstructured type contained to parse+chunk is the
    "keep the third-party library thin" rule in practice — the DB and retrieval layers
    never see an Unstructured type.

The `strategy` knob (only affects PDFs and images; text formats ignore it):
    - "fast"     : text-extraction only (pdfminer). No system binaries. Great for
                   digital PDFs whose text is already selectable; useless on scans.
    - "hi_res"   : run a layout-detection model + OCR. Best structure/table fidelity,
                   but needs the poppler and tesseract system binaries on PATH.
    - "ocr_only" : OCR every page as an image. For scanned/image-only PDFs.
    - "auto"     : let Unstructured pick per document.

    The real ingestion pipeline defaults to "hi_res" (see
    `Settings.ingest_pdf_strategy`) because table-structure inference — which we always
    request (see below) — only runs under hi_res. This function keeps a permissive
    "auto" default for library callers; the orchestrator passes the strategy explicitly.

Table structure (always on):
    We pass `infer_table_structure=True` on every partition. Under hi_res that runs
    Unstructured's table-transformer model, so a `Table` element carries a real HTML
    grid in `metadata.text_as_html` (preserving cell<->header links) rather than a
    flattened text blob. It is a no-op on the "fast" path and harmless for text formats.

Async note: `partition` is synchronous and, under "hi_res", CPU-heavy (it runs an ML
layout model). We run it inside `asyncio.to_thread` so a parse never blocks the event
loop — the same pattern `LocalDiskStorage` uses for its blocking filesystem calls.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import. `from __future__ import annotations` makes every annotation a
    # string that's never evaluated at runtime, so importing `parse.py` costs nothing —
    # the heavy Unstructured graph (torch, transformers, the OCR/layout models pulled in
    # by `partition.auto`) is imported lazily, only when a parse actually runs. That
    # keeps app startup and test collection instant.
    from unstructured.documents.elements import Element

# The strategies Unstructured accepts. We validate up front so a typo fails fast with
# a clear message instead of surfacing as an obscure error deep inside partition().
_STRATEGIES = frozenset({"auto", "fast", "hi_res", "ocr_only"})

# Exception *class names* Unstructured raises when the poppler/tesseract system
# binaries are missing. We match on the name rather than importing the optional
# modules (pdf2image / pytesseract) — they may not be importable at all.
_MISSING_BINARY_ERRORS = frozenset(
    {"PDFInfoNotInstalledError", "PDFPageCountError", "TesseractNotFoundError"}
)


async def parse_document(
    data: bytes, filename: str, *, strategy: str = "auto"
) -> list[Element]:
    """Partition `data` into typed Unstructured elements.

    `filename` is the human-supplied name; its extension is how Unstructured routes to
    the right partitioner (.pdf -> PDF, .md -> Markdown, …), so it must carry a real
    extension even though the bytes — not the name — are what we parse. `strategy`
    selects the PDF/image reading strategy (see module docstring); it is ignored for
    text formats. Returns the elements in document order.
    """
    if strategy not in _STRATEGIES:
        raise ValueError(
            f"Unknown parse strategy {strategy!r}; "
            f"expected one of {sorted(_STRATEGIES)}."
        )
    return await asyncio.to_thread(_partition_sync, data, filename, strategy)


def _partition_sync(data: bytes, filename: str, strategy: str) -> list[Element]:
    """The blocking Unstructured call, plus friendlier errors for the two failure
    modes the PDF path hits on a fresh machine (missing optional dep / missing system
    binary). Runs inside a worker thread via `asyncio.to_thread`."""
    # Lazy import (see the TYPE_CHECKING note above): the first parse in a process pays
    # the one-time cost of loading Unstructured's partitioner graph; nothing else does.
    from unstructured.partition.auto import partition

    try:
        # Pass the bytes as a file object; `metadata_filename` gives Unstructured the
        # extension it needs to pick a partitioner without touching real disk.
        return partition(
            file=BytesIO(data),
            metadata_filename=filename,
            strategy=strategy,
            # Always request table-structure inference (locked decision). Under hi_res
            # this runs Unstructured's table-transformer model so a detected table keeps
            # its grid as `metadata.text_as_html` (a real <table>…</table>) instead of
            # a single flattened blob that loses every cell<->header link. The flag is a
            # no-op on the "fast" text path, and harmless for text formats (which get
            # text_as_html from their own markup), so it is safe to pass always.
            infer_table_structure=True,
        )
    except ImportError as exc:
        # e.g. hi_res needs `unstructured-inference`, which ships with the [pdf] extra.
        raise RuntimeError(
            f"Parsing {filename!r} (strategy={strategy!r}) needs an optional "
            "Unstructured dependency that isn't installed — for PDFs, install "
            f"'unstructured[pdf]'. Original error: {exc}"
        ) from exc
    except Exception as exc:
        if type(exc).__name__ in _MISSING_BINARY_ERRORS:
            raise RuntimeError(
                f"Parsing {filename!r} (strategy={strategy!r}) needs the poppler "
                "and/or tesseract system binaries, which aren't on PATH. Install them "
                "(Windows: `choco install poppler tesseract`), or use strategy='fast' "
                f"for a digital PDF. Original error: {exc}"
            ) from exc
        raise
