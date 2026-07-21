"""Opt-in live smoke: the REAL RAGAS judge scores one row against the real Gemini API.

Everything else in the Q9 suite runs against fake metrics, which proves our routing,
aggregation and budget logic but says nothing about whether the ragas<->google-genai
wiring actually works. This test is the one that would catch "the judge never ran": a
provider signature change, a bad model id, an auth failure.

Deliberately the CHEAPEST run that proves it: one row, faithfulness only, which is 2
Gemini requests out of a 500/day quota. It needs no Postgres — the contexts are inline —
so it isolates the judge from the retrieval stack.

Opt-in via the `live` marker (deselected by default; run with `-m live` and
GEMINI_API_KEY set). Never skipped: a skip reads as a false green.

Correctness, not just liveness: the answer contradicts its context, so a working judge
must score it BELOW a perfectly grounded one. Asserting a relative ordering rather than
an absolute threshold keeps the test meaningful without making it flaky — the judge is
non-deterministic, so pinning "must be < 0.3" would be pinning noise.
"""

from __future__ import annotations

import pytest
from eval.judge import build_judge
from eval.ragas_eval import ALL_METRICS, _build_metrics, aggregate, score_rows

pytestmark = pytest.mark.live

_CONTEXTS = [
    "Aurelia Robotics was founded in 2011 in Lisbon. Its CEO is Marta Silveira.",
]


def _row(answer: str, row_id: int) -> dict:
    return {
        "kind": "golden",
        "id": row_id,
        "question": "Who is the CEO of Aurelia Robotics?",
        "reference": "Marta Silveira",
        "contexts": _CONTEXTS,
        "answer": answer,
        "citations": [1],
        "retrieved_chunk_ids": [1],
    }


async def test_live_judge_separates_a_grounded_answer_from_a_fabricated_one() -> None:
    judge = build_judge(rpm=15)
    metrics = _build_metrics(judge, ["faithfulness"], strictness=1)

    grounded = _row("The CEO of Aurelia Robotics is Marta Silveira.", 1)
    fabricated = _row(
        "The CEO of Aurelia Robotics is Henrik Vogel, appointed in 2019.", 2
    )

    scored = await score_rows([grounded, fabricated], metrics)

    # The judge actually ran: no metric errored out.
    assert "errors" not in scored[0], scored[0].get("errors")
    assert "errors" not in scored[1], scored[1].get("errors")

    grounded_score = scored[0]["scores"]["faithfulness"]
    fabricated_score = scored[1]["scores"]["faithfulness"]
    assert grounded_score is not None and fabricated_score is not None
    # The whole point of claim-level entailment: an unsupported claim scores lower.
    assert grounded_score > fabricated_score

    # And the aggregation path works on real results, not just canned floats.
    summary = aggregate(scored)
    assert summary["golden"]["faithfulness"]["n_scored"] == 2

    # The limiter counted the real traffic (2 calls per row for faithfulness).
    assert judge.limiter.requests == 4
    assert set(metrics) <= set(ALL_METRICS)
