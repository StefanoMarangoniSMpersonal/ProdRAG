"""Q5 lexical-search spec: `search_lexical` returns the chunks whose TEXT matches.

This is the *immutable spec* for Q5 (CLAUDE.md "test-first & test-immutable"): written
and watched fail (red) before `app/retrieve/lexical.py` existed. Once written it does
not change to accommodate the code — the code bends to it.

What Q5 is: naive lexical (full-text) search over the `tsvector` column + GIN index Q1
switched on. Given a query *string*, parse it into a `tsquery` with
`websearch_to_tsquery('english', …)` (Google-style user input), match it against
`chunks.tsv` (`@@`), and return the matching chunks ranked by `ts_rank_cd` (cover
density), each wrapped in a `ScoredChunk`. The `score` is the `ts_rank_cd` value:
higher = more relevant — the SAME direction as semantic's cosine similarity, so the two
lists fuse uniformly in Q6.

The defining contrast with semantic search (why the specs differ): semantic returns the
`k` *nearest* rows of the WHOLE table — every row is a candidate, distance just orders
them. Lexical returns ONLY rows that actually MATCH the query terms; a chunk sharing no
lexeme with the query never appears, even when `k` has room —
`test_..._excludes_non_matches` pins exactly that.

These assertions run against a REAL Postgres (the throwaway `pgvector/pgvector:pg16`
testcontainer) because `to_tsvector`/`websearch_to_tsquery`/`ts_rank_cd` and the
generated `tsv` column are Postgres features SQLite can't stand in for. They use the
committing `session_factory` fixture (TRUNCATE teardown) so seeded rows are visible the
way an independent retrieval query would be, and ERROR loudly (never SKIP) if Docker is
down — the "a SKIP is a false green" stance.

Seeding sets only `content`; `chunks.tsv` is a GENERATED column
(`to_tsvector('english', content)`), so Postgres computes the searchable text itself —
the test never writes `tsv` directly, matching production (M5 write only ever set
`content`). `embedding` is a throwaway zero-vector purely to satisfy the NOT NULL
column; lexical search never reads it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DEV_OWNER_ID, Chunk, Document
from app.retrieve.lexical import search_lexical

DIMS = 768

# Lexical search never touches the vector; the zero-vector just satisfies NOT NULL.
_DUMMY_VEC = [0.0] * DIMS

# A second owner, distinct from DEV_OWNER_ID, for the visibility-filter test.
OTHER_OWNER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


async def _seed_document(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Insert a parent `documents` row (chunks.document_id is a NOT NULL FK); commit."""
    async with factory() as session:
        doc = Document(filename="q5.md", source_uri="file://q5-key/q5.md")
        session.add(doc)
        await session.flush()
        doc_id = doc.id
        await session.commit()
    return doc_id


async def _seed_chunk(
    factory: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
    *,
    ordinal: int,
    content: str,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> None:
    """Insert one committed `chunks` row; `tsv` is computed by the DB from `content`."""
    async with factory() as session:
        session.add(
            Chunk(
                document_id=document_id,
                owner_id=owner_id,
                ordinal=ordinal,
                content=content,
                embedding=_DUMMY_VEC,
                char_count=len(content),
            )
        )
        await session.commit()


# --- exact token match: finds the rare token, EXCLUDES chunks that don't match ---


async def test_search_lexical_finds_exact_token_and_excludes_non_matches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # A rare made-up token semantic search would rank poorly (no learned meaning), but
    # lexical nails because the exact word is present.
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=0,
        content="The Quenlorix protocol governs sync.",
    )
    # Two chunks that share NO lexeme with the query. Lexical must omit them entirely,
    # even though k=10 leaves plenty of room (the semantic-vs-lexical divide).
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=1,
        content="Completely unrelated banana content.",
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=2,
        content="Another passage about weather maps.",
    )

    async with session_factory() as session:
        results = await search_lexical(session, "quenlorix", k=10)

    assert [r.chunk.content for r in results] == [
        "The Quenlorix protocol governs sync."
    ]


# --- ranking: ts_rank_cd puts denser matches first, score higher = more relevant ---


async def test_search_lexical_ranks_denser_match_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # Same query term, different densities. `ts_rank_cd` rewards more/closer hits, so
    # the 3x chunk must outrank the 1x chunk. This pins BOTH the order and the score
    # DIRECTION (higher = more relevant): a regression that flipped the sort or negated
    # the score goes red here.
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=0,
        content="orbital orbital orbital dynamics",
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=1,
        content="orbital mechanics and a lot of other unrelated filler words here",
    )

    async with session_factory() as session:
        results = await search_lexical(session, "orbital", k=10)

    assert [r.chunk.content for r in results] == [
        "orbital orbital orbital dynamics",
        "orbital mechanics and a lot of other unrelated filler words here",
    ]
    assert results[0].score > results[1].score
    assert results[1].score > 0.0


# --- k truncates (keeping the top-ranked matches) -------------------------------------


async def test_search_lexical_k_truncates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # Three matching chunks with strictly decreasing term density (3x, 2x, 1x) so the
    # ranking order is deterministic; k=2 must drop the weakest (1x) match.
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="zephyrine zephyrine zephyrine"
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=1, content="zephyrine zephyrine only"
    )
    await _seed_chunk(session_factory, doc_id, ordinal=2, content="zephyrine once here")

    async with session_factory() as session:
        results = await search_lexical(session, "zephyrine", k=2)

    assert len(results) == 2
    assert [r.chunk.content for r in results] == [
        "zephyrine zephyrine zephyrine",
        "zephyrine zephyrine only",
    ]


# --- Google-style parsing: `or` is an OR operator, not a stopword AND ---


async def test_search_lexical_parses_websearch_or_operator(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # Query "orbital or quenlorix": `websearch_to_tsquery` reads this as
    # (orbital | quenlorix) and matches a chunk containing EITHER term. plainto_tsquery
    # would drop "or" as a stopword and AND the rest -> a chunk with only one term would
    # NOT match. Asserting BOTH single-term chunks come back pins the parser choice to
    # websearch_to_tsquery.
    await _seed_chunk(
        session_factory, doc_id, ordinal=0, content="orbital dynamics of the station"
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=1, content="the quenlorix handshake"
    )
    await _seed_chunk(
        session_factory, doc_id, ordinal=2, content="unrelated content about gardening"
    )

    async with session_factory() as session:
        results = await search_lexical(session, "orbital or quenlorix", k=10)

    assert {r.chunk.content for r in results} == {
        "orbital dynamics of the station",
        "the quenlorix handshake",
    }


# --- owner filter: another owner's matching chunk is invisible ------------------------


async def test_search_lexical_filters_by_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc_id = await _seed_document(session_factory)

    # The other owner's chunk is the STRONGEST lexical match (term appears 3x), so if
    # the owner filter were missing it would rank first. It must not appear at all.
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=0,
        content="marniset marniset marniset",
        owner_id=OTHER_OWNER_ID,
    )
    await _seed_chunk(
        session_factory,
        doc_id,
        ordinal=1,
        content="marniset appears once",
        owner_id=DEV_OWNER_ID,
    )

    async with session_factory() as session:
        results = await search_lexical(session, "marniset", k=10)

    assert [r.chunk.content for r in results] == ["marniset appears once"]
