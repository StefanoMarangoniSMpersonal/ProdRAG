"""Shared result vocabulary for the retrieve package.

`ScoredChunk` is the type every retrieval stage returns: semantic (Q2), lexical (Q5),
and rerank (Q7) all hand back `list[ScoredChunk]`. It lives here — a neutral module
none of those stages "owns" — so there is exactly ONE canonical definition they import,
and no import cycle (a `ScoredChunk` defined in `semantic.py` would be a *different
class* from one in `lexical.py`, and fusion combining both lists would mix them).

Why a wrapper at all: relevance is a property of a chunk *with respect to a specific
query*, not of the chunk itself — so it can't live on the `Chunk` ORM row. A retrieval
stage produces two things at once, the chunk and how relevant it was, and `ScoredChunk`
pairs them.

Score convention (shared across all stages): **higher = more relevant.** Semantic search
stores cosine *similarity* (`1 - cosine_distance`), lexical stores `ts_rank`, rerank
stores the reranker's score — all in the same direction, so fusion/rerank code reads
uniformly and never has to remember which stage means "bigger is better" vs "smaller".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import Chunk


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A retrieved `Chunk` paired with its relevance score for one query.

    Frozen + slots: an immutable little struct that stages pass along and read (the same
    shape as `IngestResult` in `app/ingest/orchestrate.py`). `score` follows the
    package-wide "higher = more relevant" convention (see module docstring).
    """

    chunk: Chunk
    score: float


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """What one retrieve call produced — the whole read side's output, in one object.

    Returned by `retrieve()` (Q3) and consumed by everything above it (the `/ask`
    endpoint in Q10, the eval harness in Q4, the `explain-retrieval` skill). It lives
    here beside `ScoredChunk` — the shared result-vocabulary module — so there is one
    canonical definition and no import cycle, the same reasoning `ScoredChunk` records.

    `chunks` is the ranked result (best first). `timings_ms` maps each stage name
    (`embed_ms`, `search_ms`, `total_ms`; more as lexical/rerank land) to its wall-clock
    milliseconds — retrieval must never run silently (the "eval is a substrate" rule,
    same reason `IngestResult` carries timings). `query` echoes the original question so
    a result identifies what it answered without the caller having to thread it back.
    """

    query: str
    chunks: list[ScoredChunk]
    timings_ms: dict[str, float] = field(default_factory=dict)
