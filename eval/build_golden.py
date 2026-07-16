"""Regenerate `eval/golden.jsonl` by mapping each expected answer to its chunk id(s).

Why this is a script and not a hand-written file: chunk ids are DB-generated
`BIGINT IDENTITY` values that CHANGE on every re-ingest (a re-ingest is delete-then-insert,
so Postgres hands out fresh numbers). A `golden.jsonl` with hand-typed ids would silently
go wrong the next time the corpus is loaded and every metric would be computed against
garbage with no error. So the id mapping is a *derived* artifact: re-run this after any
re-ingest and the ids refresh.

  python -m eval.build_golden            # writes eval/golden.jsonl + prints a review report

What it does: parses the 5-column table in `eval/golden_questions.md`
(`| # | Question | Source File | Category | Expected Answer |`), reads every chunk in the
dev store (owner = DEV_OWNER_ID), and for each question finds the chunk(s) whose `content`
contains the expected answer (normalized, case-insensitive substring) **within that
question's own Source File** — the scope restriction is what stops an answer string that
also appears in an unrelated document from being matched there (cross-document false
positive). It writes a DRAFT golden.jsonl and prints a report flagging:
  - source missing -> the named Source File has no ingested chunks (ingest it, or the
                      filename isn't `<source>.md`).
  - 0 matches      -> the answer is paraphrased in the corpus; add the id(s) by hand.
  - >1 match       -> confirm every matched chunk is genuinely relevant (or trim).

The output is a DRAFT for human review, not an oracle — a substring match is a strong
hint, not proof of relevance. `run.py` consumes only the reviewed file. Trap questions
(the separate 3-column table) are intentionally ignored: no answer in the corpus -> no
relevant id; they belong to Q8 generation eval, not Q4 retrieval metrics. The Category
column is parsed past but not stored (deferred).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same import bootstrap as run.py: backend on the path + backend/.env loaded, so this
# runs as `python -m eval.build_golden` from the repo root and reaches the dev DB.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
try:
    from dotenv import load_dotenv

    load_dotenv(_BACKEND / ".env")
except Exception:  # pragma: no cover
    pass

import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import DEV_OWNER_ID, Chunk, Document  # noqa: E402

GOLDEN_SOURCE = _REPO_ROOT / "eval" / "golden_questions.md"
GOLDEN_OUT = _REPO_ROOT / "eval" / "golden.jsonl"


@dataclass(frozen=True)
class SourceQ:
    """One answerable row of golden_questions.md (the Category cell is dropped)."""

    id: int
    question: str
    source_file: str
    expected_answer: str


@dataclass(frozen=True)
class DBChunk:
    """A chunk flattened for matching: its id, content, and owning document filename.

    Keeping this separate from the `Chunk` ORM row lets `match_chunks` be a pure,
    DB-free function that the tests can drive with hand-built rows.
    """

    id: int
    content: str
    filename: str


def parse_source(path: Path) -> list[SourceQ]:
    """Pull the numbered Q&A rows out of golden_questions.md's 5-column markdown table.

    A data row is a table row with exactly five cells whose first cell is an integer.
    That single rule skips the header (`| # | Question | ... |`), the `|---|` separator,
    the Category-key bullets, and the 3-column trap table (`| T1 | ... | ... |` — three
    cells, non-numeric id) automatically. The Category cell (index 3) is parsed past but
    not carried."""
    out: list[SourceQ] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 5 or not cells[0].isdigit():
            continue
        out.append(
            SourceQ(
                id=int(cells[0]),
                question=cells[1],
                source_file=cells[2],
                expected_answer=cells[4],
            )
        )
    return out


def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace so '$9,200 per unit' can match a
    chunk that reads 'priced at 9200 per unit.' — a loose hint, refined by human review.
    """
    text = text.lower()
    text = text.replace("$", "").replace(",", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def match_chunks(expected: str, source_file: str, chunks: list[DBChunk]) -> list[int]:
    """Ids of chunks FROM `source_file` whose normalized content contains the answer.

    The `source_file == filename` gate is the scope restriction: a question's answer is
    only matched against chunks of its own document (`<source_file>.md`), so a string that
    also occurs in an unrelated doc is not spuriously credited there."""
    needle = _normalize(expected)
    if not needle:
        return []
    target = f"{source_file}.md"
    return [
        c.id for c in chunks if c.filename == target and needle in _normalize(c.content)
    ]


async def _load_chunks(owner_id: uuid.UUID) -> list[DBChunk]:
    """Every chunk visible to `owner_id`, flattened with its document filename."""
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Chunk.id, Chunk.content, Document.filename)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.owner_id == owner_id)
            .order_by(Chunk.id)
        )
        return [DBChunk(id=r[0], content=r[1], filename=r[2]) for r in rows]


async def build(owner_id: uuid.UUID = DEV_OWNER_ID) -> int:
    questions = parse_source(GOLDEN_SOURCE)
    chunks = await _load_chunks(owner_id)
    ingested = {c.filename for c in chunks}
    print(f"Parsed {len(questions)} questions; loaded {len(chunks)} chunks.\n")

    flagged: list[str] = []
    lines: list[str] = []
    for q in questions:
        ids = match_chunks(q.expected_answer, q.source_file, chunks)
        if f"{q.source_file}.md" not in ingested:
            flagged.append(
                f"  [source missing] Q{q.id}: {q.source_file!r} has no ingested chunks "
                f"-> ingest it (expected filename {q.source_file}.md)"
            )
        elif len(ids) == 0:
            flagged.append(
                f"  [0 matches] Q{q.id}: {q.question!r} -> add id(s) by hand"
            )
        elif len(ids) > 1:
            flagged.append(
                f"  [{len(ids)} matches] Q{q.id}: {q.question!r} -> confirm {ids}"
            )
        record = {
            "id": q.id,
            "question": q.question,
            "relevant_chunk_ids": ids,
            "expected_answer": q.expected_answer,
        }
        lines.append(json.dumps(record, ensure_ascii=False))

    GOLDEN_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote DRAFT {GOLDEN_OUT} ({len(lines)} rows).")
    if flagged:
        print("\nReview these rows by hand before trusting the file:")
        print("\n".join(flagged))
    else:
        print("\nEvery question matched exactly one chunk — still eyeball the file.")
    return len(flagged)


if __name__ == "__main__":  # pragma: no cover
    # Always exit 0: flagged rows are a review prompt, not a failure — this is a
    # human-in-the-loop dev tool, and a real corpus will always flag some rows.
    asyncio.run(build())
