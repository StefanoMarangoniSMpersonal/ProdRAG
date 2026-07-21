"""Spec for Q9 phase B: score cached answers with the RAGAS LLM judge.

This is the eval that finally measures the GENERATION stage. Everything before Q9 scored
retrieval (hit@k/MRR against hand-labeled chunk ids) plus one keyword proxy for refusal,
which we already know is only a lower bound — string matching cannot tell a correct
grounded negation from a fabrication.

The rules encoded here are about not lying with the numbers and not wasting the quota:

  - **Traps get faithfulness only.** A trap has no answer in the corpus, so the *right*
    response is a refusal. Answer relevancy explicitly scores a noncommittal answer 0,
    so running it over traps would punish the model precisely for behaving correctly.
    Context recall needs a reference answer, which a trap has none of.
  - **A missing reference means no context recall**, never a 0. Scoring "nothing to
    recall" as a failure would drag the mean down for a bookkeeping reason.
  - **A metric that errors yields None, not 0**, and None is excluded from the mean. A
    failed judge call is missing data; recording it as a zero would silently understate
    the pipeline.
  - **The budget is estimated BEFORE any call is made**, so an over-budget run aborts
    while it still costs nothing rather than dying at 90% having spent the day's quota.

Offline: the tests inject fake metric objects exposing the real `ascore(...)` keyword
surface, so no ragas judge, no Gemini, no key.
"""

from __future__ import annotations

import json

from eval import ragas_eval


class FakeMetric:
    """Mimics a ragas metric: async `ascore(**kwargs)` -> object with `.value`."""

    def __init__(self, value: float | None = 1.0, raises: Exception | None = None):
        self.value = value
        self.raises = raises
        self.calls: list[dict] = []

    async def ascore(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return type("Result", (), {"value": self.value})()


def _golden_row(**over) -> dict:
    row = {
        "kind": "golden",
        "id": 1,
        "question": "who?",
        "reference": "Ada",
        "contexts": ["ctx one", "ctx two"],
        "answer": "Ada did it",
        "citations": [7],
        "retrieved_chunk_ids": [7, 3],
    }
    row.update(over)
    return row


def _trap_row(**over) -> dict:
    return _golden_row(kind="trap", id="T1", reference="", **over)


# --------------------------------------------------------------------------- routing


def test_golden_row_is_scored_on_every_metric() -> None:
    assert set(ragas_eval.metrics_for_row(_golden_row())) == {
        "faithfulness",
        "answer_relevancy",
        "context_recall",
    }


def test_trap_rows_get_faithfulness_only() -> None:
    """Answer relevancy scores a correct refusal 0; recall has no reference to check."""
    assert ragas_eval.metrics_for_row(_trap_row()) == ["faithfulness"]


def test_golden_row_without_a_reference_skips_context_recall() -> None:
    names = ragas_eval.metrics_for_row(_golden_row(reference=""))
    assert "context_recall" not in names
    assert "faithfulness" in names


def test_available_metrics_can_be_narrowed() -> None:
    """`--metrics faithfulness` must really restrict what gets called (and paid for)."""
    names = ragas_eval.metrics_for_row(_golden_row(), available=["faithfulness"])
    assert names == ["faithfulness"]


# ---------------------------------------------------------------------------- budget


def test_budget_estimate_uses_the_real_per_metric_call_counts() -> None:
    """Faithfulness costs 2, context recall 1, relevancy 2*strictness + 1.

    These are the counts ragas actually issues: statement generation + NLI verdicts for
    faithfulness; one classification for recall; and for relevancy, `strictness`
    question generations, one embedding of the user's question, and `strictness`
    embeddings of the generated ones. Guessing would defeat the guard's purpose.

    SPEC REVISION (architect-authorized, 2026-07-21). Relevancy was originally costed at
    `strictness + 2`, on the assumption that embedding the generated questions was one
    batch call. Fixing the gemini-embedding-2 batch-fusion bug (see test_judge.py) made
    that call fan out to one request per question, so the true cost grew to
    `2*strictness + 1`. The old formula undercounted a strictness=3 run over 48
    questions by ~96 requests — a guard that understates spend is worse than none.
    """
    est = ragas_eval.estimate_requests([_golden_row()], strictness=1)
    assert est == 2 + 1 + (2 * 1 + 1)


def test_budget_estimate_accounts_for_strictness() -> None:
    """Each extra strictness round costs TWO requests: one generation, one embedding."""
    lo = ragas_eval.estimate_requests([_golden_row()], strictness=1)
    hi = ragas_eval.estimate_requests([_golden_row()], strictness=3)
    assert hi - lo == 4


def test_budget_estimate_is_cheaper_for_traps() -> None:
    assert ragas_eval.estimate_requests([_trap_row()], strictness=1) == 2


# --------------------------------------------------------------------------- scoring


async def test_each_metric_receives_the_arguments_its_signature_wants() -> None:
    """The three metrics take DIFFERENT kwargs; the adapter must map each correctly."""
    f, r, c = FakeMetric(0.5), FakeMetric(0.6), FakeMetric(0.7)
    metrics = {"faithfulness": f, "answer_relevancy": r, "context_recall": c}

    await ragas_eval.score_rows([_golden_row()], metrics)

    assert f.calls == [
        {
            "user_input": "who?",
            "response": "Ada did it",
            "retrieved_contexts": ["ctx one", "ctx two"],
        }
    ]
    assert r.calls == [{"user_input": "who?", "response": "Ada did it"}]
    assert c.calls == [
        {
            "user_input": "who?",
            "retrieved_contexts": ["ctx one", "ctx two"],
            "reference": "Ada",
        }
    ]


async def test_scores_are_attached_to_the_row() -> None:
    metrics = {"faithfulness": FakeMetric(0.25), "answer_relevancy": FakeMetric(0.75)}

    scored = await ragas_eval.score_rows([_golden_row()], metrics)

    assert scored[0]["scores"] == {"faithfulness": 0.25, "answer_relevancy": 0.75}
    # The row's identity and evidence survive scoring, so a number stays traceable.
    assert scored[0]["id"] == 1
    assert scored[0]["contexts"] == ["ctx one", "ctx two"]


async def test_a_failing_metric_yields_none_and_records_the_error() -> None:
    """Missing data, not a zero — a judge failure must not be read as a bad answer."""
    metrics = {
        "faithfulness": FakeMetric(raises=RuntimeError("429 quota")),
        "answer_relevancy": FakeMetric(1.0),
    }

    scored = await ragas_eval.score_rows([_golden_row()], metrics)

    assert scored[0]["scores"]["faithfulness"] is None
    assert "429 quota" in scored[0]["errors"]["faithfulness"]
    # One metric failing must not abort the others.
    assert scored[0]["scores"]["answer_relevancy"] == 1.0


async def test_trap_rows_never_call_the_relevancy_metric() -> None:
    f, r = FakeMetric(1.0), FakeMetric(0.0)

    await ragas_eval.score_rows(
        [_trap_row()], {"faithfulness": f, "answer_relevancy": r}
    )

    assert len(f.calls) == 1
    assert r.calls == []


async def test_limit_truncates_scoring(tmp_path) -> None:
    rows = [_golden_row(id=i) for i in range(5)]
    f = FakeMetric(1.0)

    scored = await ragas_eval.score_rows(rows, {"faithfulness": f}, limit=2)

    assert [s["id"] for s in scored] == [0, 1]
    assert len(f.calls) == 2


async def test_checkpoint_is_written_as_each_row_completes(tmp_path) -> None:
    """A 20-minute run must not lose everything to a 429 on the last question."""
    ckpt = tmp_path / "ckpt.jsonl"
    rows = [_golden_row(id=1), _golden_row(id=2)]

    await ragas_eval.score_rows(
        rows, {"faithfulness": FakeMetric(1.0)}, checkpoint_path=ckpt
    )

    lines = ckpt.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1


# ------------------------------------------------------------------------ aggregation


def test_aggregate_means_are_split_by_kind() -> None:
    """Golden and trap faithfulness ask different things; one mean would blur them."""
    scored = [
        {"kind": "golden", "scores": {"faithfulness": 1.0}},
        {"kind": "golden", "scores": {"faithfulness": 0.0}},
        {"kind": "trap", "scores": {"faithfulness": 1.0}},
    ]

    summary = ragas_eval.aggregate(scored)

    assert summary["golden"]["faithfulness"]["mean"] == 0.5
    assert summary["golden"]["faithfulness"]["n_scored"] == 2
    assert summary["trap"]["faithfulness"]["mean"] == 1.0


def test_aggregate_excludes_none_from_the_mean() -> None:
    scored = [
        {"kind": "golden", "scores": {"faithfulness": 1.0}},
        {"kind": "golden", "scores": {"faithfulness": None}},
    ]

    summary = ragas_eval.aggregate(scored)

    assert summary["golden"]["faithfulness"]["mean"] == 1.0
    assert summary["golden"]["faithfulness"]["n_scored"] == 1
    assert summary["golden"]["faithfulness"]["n_failed"] == 1


def test_aggregate_handles_a_metric_with_no_successful_scores() -> None:
    scored = [{"kind": "golden", "scores": {"faithfulness": None}}]

    summary = ragas_eval.aggregate(scored)

    assert summary["golden"]["faithfulness"]["mean"] is None
    assert summary["golden"]["faithfulness"]["n_scored"] == 0


# ------------------------------------------------------------------------- reporting


def test_format_table_states_the_caveats_it_would_be_dishonest_to_bury() -> None:
    """The honest-labeling rule: a judge's ratio is not ground truth, and says so."""
    report = {
        "summary": ragas_eval.aggregate(
            [{"kind": "golden", "scores": {"faithfulness": 0.9}}]
        ),
        "metadata": {"judge_model": "gemini-3.1-flash-lite", "self_graded": True},
    }

    out = ragas_eval.format_table(report).lower()

    assert "self-graded" in out
    assert "not ground truth" in out
    assert "non-deterministic" in out


def test_write_report_is_timestamped_and_parseable(tmp_path) -> None:
    report = {"summary": {}, "metadata": {}, "per_row": []}

    path = ragas_eval.write_report(report, tmp_path)

    assert path.name.startswith("ragas-")
    assert path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8")) == report
