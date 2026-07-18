"""Q5 — lexical retrieval: query STRING -> the chunks whose TEXT matches, ranked.

The second retrieval signal, standing beside Q2's semantic search. Where semantic search
matches on *meaning* (vector proximity), lexical search matches on the actual *words*:
Postgres full-text search over the `tsvector` column + GIN index Q1 switched on. It's
the half that catches what dense vectors are worst at — proper nouns, acronyms, code
identifiers, product names, IDs, rare tokens the embedding model never learned a good
direction for. Q6 fuses the two with Reciprocal Rank Fusion; this stage produces one of
its two input lists.

The shape deliberately mirrors `search_semantic` (same seams, same session-passed-in
contract, `list[ScoredChunk]` return, same "higher = more relevant" score direction) so
the two are interchangeable inputs to fusion. The one structural difference is what
"no match" means (see below).

Full-text search vocabulary (the moving parts):
    - `tsvector` — the SEARCHABLE form of a document: its text lexed into normalized
      *lexemes* (lowercased, stop-words dropped, stemmed: "governs"/"governing" ->
      "govern") with positions. `chunks.tsv` is a GENERATED column
      (`to_tsvector('english', content)`), so Postgres maintains it from `content`
      automatically — ingest never writes it.
    - `tsquery` — the SEARCHABLE form of a QUERY: lexemes combined with boolean/phrase
      operators (`&` and, `|` or, `!` not, `<->` followed-by).
    - `@@` — the match operator: `tsv @@ tsquery` is true when the document satisfies
      the query. The GIN index on `tsv` is what makes this fast.

Why `websearch_to_tsquery` (not `plainto_tsquery` / `to_tsquery`):
    A user types a search box query, not a tsquery. Three parsers turn text into a
    `tsquery`, differing in how much syntax they honor:
      - `to_tsquery` — expects RAW tsquery syntax (`orbital & dynamics`); RAISES on
        plain prose. Too brittle for user input.
      - `plainto_tsquery` — treats the whole string as words AND-ed together; ignores
        all operators (quotes, `or`, `-`).
      - `websearch_to_tsquery` — Google-style: honors `"quoted phrases"` (-> `<->`),
        `or` (-> `|`), leading `-` (-> `!`), and never raises on arbitrary input.
    We take `websearch_to_tsquery` because the caller is a human search box; it degrades
    gracefully and gives users the operators they already expect.

Ranking with `ts_rank_cd` (cover density), score direction:
    A match is boolean; ranking orders the matches. `ts_rank_cd` ("cover density")
    scores by how many query lexemes hit and how TIGHTLY they cluster — a chunk where
    the query terms sit close together (a small "cover") outranks one where they're
    scattered. We chose `_cd` over plain `ts_rank` because proximity is a real relevance
    signal for multi-word queries; for one term they behave alike (frequency-based).
    Neither is true Okapi BM25 — Postgres FTS ranking is "BM25-ish" (no proper IDF /
    length normalization). Real BM25 needs an extension (ParadeDB / `pg_search`);
    deferred. The `ts_rank_cd` value IS the `ScoredChunk.score`, and higher = more
    relevant — the same direction as semantic's cosine similarity, so fusion (Q6) reads
    both lists uniformly. We `SELECT` the rank expression once and reuse the same
    expression object for the `ORDER BY`, so the sort and the reported score can never
    disagree (same discipline `semantic.py` uses for its distance expression).

The defining contrast with semantic search — MATCH vs NEAREST:
    `search_semantic` returns the k *nearest* rows of the whole table: every row is a
    candidate and distance only orders them. `search_lexical` returns ONLY rows that
    actually MATCH the `tsquery` (the `@@` predicate). A chunk sharing no lexeme with
    the query never appears even when `k` has room — it may return fewer than `k`, or 0.
    That's not a bug to paper over; it's exactly why lexical complements semantic (which
    always returns *something*, relevant or not).

Owner filter, module-level seam: identical to semantic — every query is scoped by
`owner_id` (visibility / future-RLS boundary), and the `session` is passed in because
Q3's `retrieve()` orchestrator owns `SessionLocal`. No `ef_search` knob here: that tunes
the HNSW *vector* index; lexical rides the GIN index instead.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models import DEV_OWNER_ID, Chunk
from app.retrieve.types import ScoredChunk

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def search_lexical(
    session: AsyncSession,
    query_text: str,
    *,
    k: int,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> list[ScoredChunk]:
    """Return the `k` best-ranked chunks matching `query_text`, owner-filtered.

    Parses `query_text` with `websearch_to_tsquery('english', …)`, matches it against
    `chunks.tsv` (`@@`), ranks the matches by `ts_rank_cd` (cover density), and returns
    them best-first as `ScoredChunk`s whose `score` is the rank value (higher = more
    relevant). Unlike semantic search this returns ONLY actual matches, so it may return
    fewer than `k` (or none). `owner_id` restricts the candidate set (visibility seam).
    """
    # Parse the user's search-box text into a tsquery ONCE, then reuse the same
    # expression object for the match predicate, the rank, and (transitively) the
    # ORDER BY — so all three see identical query lexemes.
    tsquery = func.websearch_to_tsquery("english", query_text)

    # One rank expression, reused for the score column AND the ordering, so they can
    # never diverge (the "compute-once" discipline from semantic.py's distance expr).
    rank = func.ts_rank_cd(Chunk.tsv, tsquery)

    rows = await session.execute(
        select(Chunk, rank.label("rank"))
        .where(Chunk.owner_id == owner_id)  # visibility seam / future RLS boundary
        .where(Chunk.tsv.op("@@")(tsquery))  # MATCH: only chunks sharing a lexeme
        .order_by(rank.desc())  # descending: highest relevance first
        .limit(k)
    )

    return [ScoredChunk(chunk=chunk, score=float(rank)) for chunk, rank in rows]
