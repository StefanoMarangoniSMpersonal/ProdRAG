"""Q8 refusal-detection spec: does a generated answer count as a clean refusal?

The refusal eval (`eval/refusal.py`) runs the 10 trap questions — questions whose answer
is NOT in the corpus — through retrieve -> generate, and a trap "passes" when the model
REFUSES instead of hallucinating. Deciding refusal-vs-hallucination from a
`GeneratedAnswer` is the one piece of that eval with real logic, so it's a pure,
unit-testable helper (`is_refusal`) — the live retrieve/generate wiring needs a
DB + API key and runs manually.

`is_refusal` keys off the answer TEXT (an explicit "I don't know / not in the context"
phrase), not merely empty citations: a confident but uncited answer ("Blue.") is a
grounding failure, not a good refusal, so it must NOT count as one. This is the
immutable spec for that decision.
"""

from __future__ import annotations

from eval.refusal import is_refusal

from app.generate.generate import GeneratedAnswer


def test_explicit_dont_know_is_refusal() -> None:
    # The canonical good refusal: the model declines and cites nothing.
    assert is_refusal(GeneratedAnswer(answer="I don't know.", citations=[]))


def test_context_absence_phrasing_is_refusal_even_with_citation() -> None:
    # A model may point at the chunk it checked while still declining — that's a
    # refusal (it refused to fabricate), so a citation must not disqualify it.
    ans = GeneratedAnswer(
        answer="The provided context does not mention a CTO.", citations=[5]
    )
    assert is_refusal(ans)


def test_confident_answer_with_citation_is_not_refusal() -> None:
    # A trap answered with a concrete fact + citation is exactly the hallucination the
    # eval is built to catch — not a refusal.
    assert not is_refusal(GeneratedAnswer(answer="The CTO is Jane Doe.", citations=[5]))


def test_confident_uncited_answer_is_not_refusal() -> None:
    # Empty citations alone must NOT count as a refusal: a bare "Blue." with no citation
    # is an ungrounded answer, not a decline. Refusal is signalled by the words, not the
    # empty list.
    assert not is_refusal(GeneratedAnswer(answer="Blue.", citations=[]))


def test_no_information_variants_are_refusals() -> None:
    # Common ways Gemini declines under our system instruction — all must register as
    # refusals.
    for text in [
        "There is no information about this in the provided context.",
        "The context does not contain the answer to this question.",
        "This is not specified in the documents.",
        "I cannot determine this from the given context.",
    ]:
        assert is_refusal(GeneratedAnswer(answer=text, citations=[])), text
