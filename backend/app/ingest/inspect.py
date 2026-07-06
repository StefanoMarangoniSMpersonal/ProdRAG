"""Dry-run inspector for the parse stage — the code the `/ingest-inspect` skill drives.

Run one document through `parse_document` and print the typed elements it produced, so
chunking/parsing quality can be judged by eye. It is a DRY RUN: it reads a file straight
from a path and writes nothing — no storage, no database. (The wired path that loads
bytes via `get_storage().load()` and writes rows is the M6 orchestrator's job.)

Usage:
    python -m app.ingest.inspect <path> [--strategy fast|hi_res|ocr_only|auto]
                                        [--preview N]
                                        [--max-chars N] [--combine N]

Scope: it shows two views of one document — the M2 *elements* (what the partitioner
detected) and the M3 *chunks* (how `by_title` grouped them, the retrieval unit). The
`--max-chars` / `--combine` flags re-run chunking at different sizes so you can tune
`max_characters` by eye without editing config; they default to the pipeline settings.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from app.config import get_settings
from app.ingest.chunk import chunk_document
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


async def _run(
    path: Path, strategy: str, preview: int, max_chars: int, combine: int
) -> int:
    size = path.stat().st_size
    print(f"Parsing {path.name}  ({size:,} bytes, strategy={strategy!r})\n")

    elements = await parse_document(path, strategy=strategy)

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

    # --- M3: chunk view -----------------------------------------------------------
    # Group the same elements into chunks (Unstructured by_title) and print them. This
    # is the retrieval unit — what M4 embeds and M5 stores — so eyeballing it is how
    # chunk size gets tuned. Re-run with --max-chars / --combine to compare splits.
    print("\n" + "=" * 60)
    print(f"chunking (by_title: max_chars={max_chars}, combine_under={combine})\n")
    chunks = await chunk_document(
        elements, max_characters=max_chars, combine_text_under_n_chars=combine
    )
    for i, ch in enumerate(chunks):
        page = ch.metadata.page_number
        page_str = f"p{page}" if page is not None else "p?"
        # [html] marks a chunk carrying metadata.text_as_html — in practice the isolated
        # Table chunk. Seeing it here confirms the grid survived chunking, not just M2.
        html_flag = "[html]" if ch.metadata.text_as_html else "      "
        # Flag chunks over the hard cap. by_title splits oversized elements, so this
        # should stay empty; a "!" means a chunk slipped through above max_characters.
        over = "!" if len(ch.text) > max_chars else " "
        print(
            f"[{i:>3}]{over}{html_flag} {ch.category:<16} {page_str:>4} "
            f"{len(ch.text):>5}c  {_preview(ch.text, preview)}"
        )

    # --- chunk summary ------------------------------------------------------------
    chunk_counts = Counter(ch.category for ch in chunks)
    chunk_sizes = sorted(len(ch.text) for ch in chunks)
    over_cap = sum(1 for s in chunk_sizes if s > max_chars)

    print("\n" + "-" * 60)
    print(f"chunks   : {len(chunks)}  (from {len(elements)} elements)")
    print(f"by type  : {dict(chunk_counts.most_common())}")
    if chunk_sizes:
        median = chunk_sizes[len(chunk_sizes) // 2]
        print(
            f"char size: min={chunk_sizes[0]}  median={median}  max={chunk_sizes[-1]}"
        )
    if over_cap:
        print(f"\n!  {over_cap} chunk(s) exceed max_characters={max_chars}.")
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
    # Chunking knobs default to the pipeline settings (config.py); override to tune.
    settings = get_settings()
    parser.add_argument(
        "--max-chars",
        type=int,
        default=settings.ingest_chunk_max_characters,
        help=f"chunk hard cap (default: {settings.ingest_chunk_max_characters})",
    )
    parser.add_argument(
        "--combine",
        type=int,
        default=settings.ingest_chunk_combine_text_under_n_chars,
        help=(
            "combine sections under N chars "
            f"(default: {settings.ingest_chunk_combine_text_under_n_chars})"
        ),
    )
    args = parser.parse_args(argv)

    if not args.path.is_file():
        parser.error(f"no such file: {args.path}")

    return asyncio.run(
        _run(args.path, args.strategy, args.preview, args.max_chars, args.combine)
    )


if __name__ == "__main__":
    sys.exit(main())
