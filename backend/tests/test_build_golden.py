"""Immutable spec for the Q4 golden-set generator's pure logic (eval/build_golden.py).

Test-first (CLAUDE.md "test-first & test-immutable"): the 5-column golden_questions.md
parser and the source-scoped matcher are specified here before build_golden.py is
updated. Only the DB shell (`_load_chunks` / `build`) stays live-only.

Pinned behaviors:
  - `parse_source` reads the 5-column table (# | Q | Source File | Category | Answer),
    drops the Category cell, and skips the header, separator, and 3-column trap table.
  - `match_chunks` scopes the substring match to the answer's OWN Source File, so an
    answer string that also appears in an unrelated document is not matched there.
"""

from __future__ import annotations

from pathlib import Path

from eval.build_golden import DBChunk, match_chunks, parse_source

_TABLE = """# Golden Question Set

**Category key:**
- `SINGLE_FACT` — one fact

| # | Question | Source File | Category | Expected Answer |
|---|---|---|---|---|
| 1 | Who is the CEO? | rag_test_document | SINGLE_FACT | Marta Silveira |
| 2 | Population? | corpus_government_policy | NUMERICAL | 6.8 million |

## Bonus: Trap Questions

| # | Question | Why It's a Trap |
|---|---|---|
| T1 | Who is the CTO? | No CTO is listed |
"""


def test_parse_source_reads_five_column_table(tmp_path: Path) -> None:
    path = tmp_path / "golden_questions.md"
    path.write_text(_TABLE, encoding="utf-8")

    qs = parse_source(path)

    assert len(qs) == 2  # header, separator, and trap table all skipped
    assert qs[0].id == 1
    assert qs[0].question == "Who is the CEO?"
    assert qs[0].source_file == "rag_test_document"
    assert qs[0].expected_answer == "Marta Silveira"
    assert qs[1].id == 2
    assert qs[1].source_file == "corpus_government_policy"
    assert qs[1].expected_answer == "6.8 million"


def test_parse_source_skips_trap_table(tmp_path: Path) -> None:
    path = tmp_path / "golden_questions.md"
    path.write_text(_TABLE, encoding="utf-8")
    ids = {q.id for q in parse_source(path)}
    assert ids == {1, 2}  # T1 (non-numeric id, 3 columns) never parsed


def test_match_chunks_scopes_to_source_file() -> None:
    # 'Marta Silveira' occurs in BOTH docs, but the answer's source is
    # rag_test_document, so only chunk 10 counts — chunk 20 is out of scope.
    chunks = [
        DBChunk(
            id=10, content="The CEO is Marta Silveira.", filename="rag_test_document.md"
        ),
        DBChunk(
            id=20,
            content="Marta Silveira lectured here.",
            filename="corpus_education.md",
        ),
    ]
    assert match_chunks("Marta Silveira", "rag_test_document", chunks) == [10]


def test_match_chunks_normalizes_money_and_commas() -> None:
    chunks = [
        DBChunk(
            id=1, content="priced at 9200 per unit.", filename="rag_test_document.md"
        )
    ]
    assert match_chunks("$9,200 per unit", "rag_test_document", chunks) == [1]


def test_match_chunks_returns_empty_when_source_absent() -> None:
    chunks = [DBChunk(id=1, content="Marta Silveira", filename="rag_test_document.md")]
    assert match_chunks("Marta Silveira", "ghost_doc", chunks) == []
