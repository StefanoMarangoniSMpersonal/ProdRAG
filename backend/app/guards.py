"""P2 — guards: the explicit validation layer around the read path (input + output).

`generate.py` already has THREE *implicit* grounding levers (system instruction,
temperature 0, structured output) — but "please answer only from context" is a
request, not a guarantee. This module is the *explicit* check that runs after the
model has spoken: it treats the citations as claims to verify, not facts to trust.

Two guards, deliberately in one module because they are the same idea from two
sides — a validation boundary around the read path:

  - INPUT guard (`validate_query`): reject a blank or over-long question BEFORE any
    billable work. A blank question can't be answered; an over-long one blows past
    the embedder's input cap. Both are caller errors we can know synchronously, so
    we fail fast and cheap.
  - OUTPUT guard (`check_citations`): the model is shown ONLY the final reranked
    chunks (`generate(query, result.chunks)`), so a citation that isn't one of
    *those* ids is a phantom — invented, or lifted from the wider candidate pool the
    model never actually saw. The guard partitions the citations into valid/phantom.

FRAMEWORK-FREE ON PURPOSE. This module imports no FastAPI: `validate_query` raises
plain `ValueError` subclasses and `check_citations` returns a dataclass. The HTTP
mapping (`ValueError -> HTTPException(400)`, phantom -> WARNING + repaired response)
lives in `app/api/ask.py`. That keeps the guards a pure, unit-testable core — the
same seam discipline the rest of the read path follows — and lets a future non-HTTP
caller (a batch eval, a CLI) reuse them without dragging a web framework in.

Hand-rolled, not the `guardrails-ai` library (ADR 0004): the check is a handful of
lines, and the library would drag heavy transitive constraints into a
`requirements.txt` already threading several load-bearing pins. Adopt it only if a
richer need earns the dependency AND it proves import-clean against those pins.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


class EmptyQueryError(ValueError):
    """The query was blank (empty or whitespace-only) after stripping."""


class QueryTooLongError(ValueError):
    """The query exceeded the configured character cap."""


def validate_query(raw: str, *, max_chars: int) -> str:
    """Normalize and validate an incoming question, or raise.

    Strips surrounding whitespace (the meaningful content is what we validate and
    embed), then enforces two pre-spend invariants:
      - non-blank: an empty question has no answer -> `EmptyQueryError`.
      - within the cap: longer than `max_chars` risks the embedder's ~8,192-token
        input limit and is almost always a paste accident -> `QueryTooLongError`.

    Returns the stripped query on success. Raises `ValueError` subclasses (never
    `HTTPException`) so the module stays framework-free; the caller maps them to 400.
    """
    query = raw.strip()
    if not query:
        raise EmptyQueryError("Query must not be empty.")
    if len(query) > max_chars:
        raise QueryTooLongError(
            f"Query must be at most {max_chars} characters (got {len(query)})."
        )
    return query


@dataclass(frozen=True, slots=True)
class CitationCheck:
    """The output guard's verdict: the model's citations split by whether shown.

    `valid_citations` are the ids that appear in the chunks the model was actually
    given; `phantom_citations` are the ones that don't (fabricated / never-shown).
    Order and duplicates from the model's original list are preserved in each
    partition — the guard *reports* what happened, it doesn't tidy it. Frozen because
    a verdict is a fact about one call, not a mutable buffer.
    """

    valid_citations: list[int]
    phantom_citations: list[int]

    @property
    def has_violation(self) -> bool:
        """True when the model cited at least one chunk it was never shown."""
        return bool(self.phantom_citations)


def check_citations(citations: list[int], shown_ids: Iterable[int]) -> CitationCheck:
    """Partition `citations` by membership in `shown_ids` (the chunks the model saw).

    Pure and order-preserving: walk the model's citations once, routing each id to
    valid or phantom by set membership. `shown_ids` is materialized into a set for
    O(1) lookups. Empty citations (the refusal path) yield two empty lists and
    `has_violation == False`, so a "don't know" answer passes trivially — there is
    nothing to fabricate.
    """
    shown = set(shown_ids)
    valid = [c for c in citations if c in shown]
    phantom = [c for c in citations if c not in shown]
    return CitationCheck(valid_citations=valid, phantom_citations=phantom)
