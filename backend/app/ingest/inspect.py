"""Dry-run inspector for the parse stage — the code the `/ingest-inspect` skill drives.

Run one document through `parse_document` and print the typed elements it produced, so
chunking/parsing quality can be judged by eye. It is a DRY RUN: it reads a file straight
from a path and writes nothing — no storage, no database. (The wired path that loads
bytes via `get_storage().load()` and writes rows is the M6 orchestrator's job.)

Usage:
    python -m app.ingest.inspect <path> [--strategy fast|hi_res|ocr_only|auto]
                                        [--preview N]

Scope: at M2 this shows *elements*. The chunk-level checks the skill also describes
(mid-table splits, size vs. `max_characters`) become meaningful at M3, once chunking
exists; this inspector grows a chunk view then.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from app.ingest.parse import parse_document

_PREVIEW_DEFAULT = 160


def _preview(text: str, limit: int) -> str:
    """One-line, whitespace-collapsed, truncated preview with a visible boundary.

    ASCII-only on purpose: the Windows console defaults to cp1252, which can't encode
    fancy glyphs (angle brackets, ellipsis) and would crash the print with a
    UnicodeEncodeError. Plain quotes make the element boundaries clear everywhere.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return f'"{collapsed}"'
    return f'"{collapsed[:limit]}..."  (+{len(collapsed) - limit} chars)'


async def _run(path: Path, strategy: str, preview: int) -> int:
    data = path.read_bytes()
    print(f"Parsing {path.name}  ({len(data):,} bytes, strategy={strategy!r})\n")

    elements = await parse_document(data, path.name, strategy=strategy)

    # --- per-element view ---------------------------------------------------------
    for i, el in enumerate(elements):
        page = el.metadata.page_number
        page_str = f"p{page}" if page is not None else "p?"
        # Flag elements that carry a structural HTML rendering (metadata.text_as_html) —
        # in practice Tables under hi_res with infer_table_structure. Seeing [html] here
        # confirms the grid survived parsing rather than being flattened to text.
        html_flag = "[html]" if el.metadata.text_as_html else "      "
        print(
            f"[{i:>3}] {html_flag} {el.category:<16} {page_str:>4} "
            f"{len(el.text):>5}c  {_preview(el.text, preview)}"
        )

    # --- summary ------------------------------------------------------------------
    counts = Counter(el.category for el in elements)
    sizes = sorted(len(el.text) for el in elements)
    pages = [el.metadata.page_number for el in elements]
    max_page = max((p for p in pages if p is not None), default=None)
    empty = sum(1 for el in elements if not el.text.strip())

    print("\n" + "-" * 60)
    print(f"elements : {len(elements)}")
    print(f"by type  : {dict(counts.most_common())}")
    if sizes:
        median = sizes[len(sizes) // 2]
        print(f"char size: min={sizes[0]}  median={median}  max={sizes[-1]}")
    print(f"pages    : {max_page if max_page is not None else 'n/a'}")

    # Garbled-output heuristic: if most elements are empty, parsing (not chunking) is
    # the problem — a scanned PDF read without OCR, or an unsupported layout.
    if elements and empty / len(elements) > 0.5:
        print(
            f"\n!  {empty}/{len(elements)} elements have no text -- this looks like a "
            "PARSING problem, not a chunking one. If it's a scanned/image PDF, try "
            "--strategy hi_res (or ocr_only)."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest.inspect",
        description="Dry-run: parse one document and print its typed elements.",
    )
    parser.add_argument("path", type=Path, help="path to the document to inspect")
    parser.add_argument(
        "--strategy",
        choices=["auto", "fast", "hi_res", "ocr_only"],
        default="auto",
        help="PDF/image read strategy (ignored for text formats); default: auto",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=_PREVIEW_DEFAULT,
        help=f"max preview chars per element (default: {_PREVIEW_DEFAULT})",
    )
    args = parser.parse_args(argv)

    if not args.path.is_file():
        parser.error(f"no such file: {args.path}")

    return asyncio.run(_run(args.path, args.strategy, args.preview))


if __name__ == "__main__":
    sys.exit(main())
