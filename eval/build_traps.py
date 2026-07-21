"""Regenerate eval/traps.jsonl from the trap table in golden_questions.md.

`python -m eval.build_traps` (writes eval/traps.jsonl)

The trap questions live in `golden_questions.md` under "Bonus: Trap / Absence Questions" as a
3-column table (`| # | Question | Why It's a Trap |`) with ids T1–T10. They are deliberately
NOT in `golden.jsonl` (which is the 48 answerable retrieval questions): a trap has no answer in
the corpus, so it has no relevant chunk id and no place in retrieval metrics. It belongs to the
Q8 generation refusal eval instead (`eval/refusal.py`).

Unlike `build_golden.py`, this needs no DB: a trap carries only {id, question} — there is no
expected answer to map to a chunk. So traps.jsonl is stable (it doesn't churn when chunk ids
change) and this regenerator only re-reads the markdown. Kept separate from build_golden.py so
the immutable retrieval-golden build path stays untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCE = _REPO_ROOT / "eval" / "golden_questions.md"
_OUT = _REPO_ROOT / "eval" / "traps.jsonl"

# A trap row: `| T1 | question text | why it's a trap |`. The id is 'T' + digits; we keep the
# id and the question (2nd cell) and ignore the rationale (3rd cell).
_TRAP_ROW = re.compile(r"^\|\s*(T\d+)\s*\|\s*(.+?)\s*\|\s*.+?\s*\|\s*$")


def parse_traps(markdown: str) -> list[dict]:
    """Pull the T-prefixed trap rows out of the source markdown, in file order."""
    traps: list[dict] = []
    for line in markdown.splitlines():
        m = _TRAP_ROW.match(line.strip())
        if m:
            traps.append({"id": m.group(1), "question": m.group(2)})
    return traps


def main() -> int:
    traps = parse_traps(_SOURCE.read_text(encoding="utf-8"))
    if not traps:
        raise SystemExit(f"No trap rows found in {_SOURCE} — check the table format.")
    lines = [json.dumps(t, ensure_ascii=False) for t in traps]
    _OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(traps)} traps -> {_OUT}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
