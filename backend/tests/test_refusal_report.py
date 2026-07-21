"""Spec for the trap-eval REPORTING layer: it must be honestly labeled.

The live run showed the model got 10/10 traps right (0 hallucinations), yet the old
report called the 2 non-refusals "hallucinations" — wrong. `is_refusal` is only a
keyword-based EXPLICIT-REFUSAL detector (a lower bound on hallucination resistance): a
non-refusal is NOT necessarily a hallucination (e.g. a correct grounded negation like
"there is no football program" carries no refusal phrase). So the report must (a) count
non-refusals without claiming they are hallucinations, and (b) say out loud that the
rate is a lower bound to inspect. Robust fabricated-vs-grounded judgment is Q9's job.

These are offline specs for the pure `summarize`/`format_table` helpers — no DB, no API.
"""

from __future__ import annotations

from eval.refusal import format_table, summarize


def _per_trap(refused_flags: list[bool]) -> list[dict]:
    return [
        {
            "id": f"T{i+1}",
            "question": f"q{i+1}",
            "refused": flag,
            "answer": "some answer",
            "citations": [1],
            "retrieved_chunk_ids": [1],
        }
        for i, flag in enumerate(refused_flags)
    ]


def test_summarize_counts_with_honest_field_names() -> None:
    s = summarize(_per_trap([True, True, True, False]))
    assert s["n_traps"] == 4
    assert s["explicit_refusals"] == 3
    assert s["non_refusals"] == 1
    assert s["explicit_refusal_rate"] == 0.75
    # A non-refusal must NOT be asserted to be a hallucination anywhere in the schema.
    assert "hallucination" not in " ".join(s.keys()).lower()


def test_summarize_empty_is_safe() -> None:
    s = summarize([])
    assert s["n_traps"] == 0
    assert s["explicit_refusal_rate"] == 0.0


def test_format_table_flags_non_refusals_for_inspection_not_as_hallucinations() -> None:
    report = {
        "summary": summarize(_per_trap([True, False])),
        "per_trap": _per_trap([True, False]),
    }
    out = format_table(report).lower()
    # The rate is honestly framed as a lower bound to inspect, not a verdict.
    assert "lower bound" in out
    assert "inspect" in out
    # The old misleading label must be gone.
    assert "hallucinated (answered" not in out
