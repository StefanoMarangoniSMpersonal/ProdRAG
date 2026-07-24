"""Q10 — the query entry point: ask a question, get a grounded, cited answer.

The HTTP shell over the finished read path. Everything hard already exists: `retrieve()`
embeds the query, searches semantically + lexically, fuses by RRF and (when enabled)
reranks; `generate()` turns those chunks into an answer constrained to them. This
module only has to wire the two, shape the response, and — the genuinely new part —
make sure the query leaves a record.

REQUEST-shaped, on purpose (the contrast with ingestion):
    `POST /documents` returns 202 and you poll, because a hi_res parse + embed runs for
    tens of seconds; holding the connection open that long invites proxy timeouts. A
    query is the opposite: ~1-3 s, and the caller has nothing to do until the answer
    arrives, so deferring it would only add a poll for no benefit. Same system, two
    shapes, each chosen by the work's duration — that is the whole lesson. (Streaming
    the answer token-by-token is a real improvement on this and is deliberately Phase
    2.5; it changes the transport, not this pipeline.)

THE PER-QUERY LOG (the milestone's second deliverable):
    CLAUDE.md: "Log every RAG query: user message, retrieved chunk IDs, reranked order,
    final context, the answer, and token/cost usage. This log is the raw material for
    evaluation." Two sinks, because they answer different questions:
      - a structured stdout line -> the live tail; "what just happened on that request?"
      - a `query_logs` row (004) -> the durable, queryable history; "which queries ever
        retrieved chunk 42?", "what did we spend last week?" — SQL questions a log
        stream can't answer.
    Both carry BOTH rankings (`retrieved_chunk_ids` = the pre-rerank candidate pool,
    `reranked_chunk_ids` = what the model was actually shown), which is what makes the
    retrieve-wide -> rerank-narrow funnel debuggable after the fact.

    The row write is BEST-EFFORT: it is wrapped so that a logging failure can never turn
    an answer the user already paid for into a 500. The audit is important; it is not
    more important than the product.

Why `retrieve` and `generate` are module-level names:
    Imported here as globals so a test can monkeypatch THIS module's copy and drive the
    endpoint with no Gemini, no torch, no embeddings — the same seam `documents.py` uses
    for `orchestrate_task`/`get_storage`. Note the handler passes no session to
    `retrieve()`: it owns its own (one per concurrent arm). The `Depends(get_session)`
    here exists solely for the audit row.
"""

from __future__ import annotations

import json
import logging
import uuid
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache
from app.config import get_settings
from app.db import get_session
from app.generate.generate import generate
from app.guards import (
    EmptyQueryError,
    QueryTooLongError,
    check_citations,
    validate_query,
)
from app.models import DEV_OWNER_ID, QueryLog
from app.retrieve.retrieve import embed_query, retrieve

router = APIRouter(tags=["ask"])
logger = logging.getLogger("app.api.ask")


class AskRequest(BaseModel):
    """The question, plus optional per-request read-path overrides.

    The knobs let the chat UI experiment live (flip the reranker/cache, move the floor)
    without touching process config — the architect's "per-request, not global state"
    decision. Each is OPTIONAL and means "use the server default" when absent:
      - `rerank_enabled` / `cache_enabled`: `None` (omitted) → the `Settings` value.
      - `rerank_score_floor`: since `None` is a MEANINGFUL value here (floor disabled),
        "use the default" is expressed by OMITTING the field; the endpoint reads
        `model_fields_set` to tell an omitted floor from an explicit `null`.
    Phase 3: gate these behind the owner / a debug flag once auth lands — today they
    are an unauthenticated, single-tenant experiment surface.
    """

    query: str = Field(description="The user's question.")
    rerank_enabled: bool | None = Field(
        default=None,
        description="Override the cross-encoder rerank stage for this query.",
    )
    cache_enabled: bool | None = Field(
        default=None, description="Override the semantic answer cache for this query."
    )
    rerank_score_floor: float | None = Field(
        default=None,
        description="Override the rerank relevance floor (null disables it). Honoured "
        "only when present in the payload.",
    )


class AppliedSettings(BaseModel):
    """The read-path settings that actually governed this answer (UI legibility echo).

    So each message can show WHAT produced it — reranked or not, the floor in force,
    cache on/off, and whether this specific answer came from the cache. On a cache HIT
    the rerank/floor fields report the effective config even though retrieval didn't run
    (the hit short-circuited it) — `cache_hit` is how the UI distinguishes the two.
    """

    rerank_enabled: bool
    rerank_score_floor: float | None
    cache_enabled: bool
    cache_hit: bool


class AskConfig(BaseModel):
    """Read-path defaults the chat control panel initialises from (GET /ask/config)."""

    rerank_enabled: bool
    cache_enabled: bool
    rerank_score_floor: float | None


class Source(BaseModel):
    """One shown passage — the text behind a citation badge.

    `citations` returns chunk ids; a bare `[13]` badge with no passage behind it is
    useless to a reader, so `AskResponse.sources` carries the actual text of every chunk
    the model was shown (a superset of `citations`), letting the UI render each passage
    and highlight the ones the answer drew on. `id` joins back to `citations` / the id
    lists; `score` is the chunk's fused/rerank score (its rank evidence). `filename` is
    deferred — `retrieve()` doesn't load the `Document` join today (a v1 follow-up).
    """

    id: int
    content: str
    score: float


class AskResponse(BaseModel):
    """The answer plus the evidence trail that produced it.

    `citations` are chunk ids the answer drew on (ADR 0002's chunk-level granularity).
    Both id lists are returned, not just the final one: a caller (and the UI) can see
    what retrieval surfaced AND what survived reranking. `sources` (P6) carries the text
    of the shown chunks so the UI can back each citation with its passage — response-only
    (the frozen audit contract is unchanged). `query_id` correlates this response with
    its log line and its `query_logs` row.
    """

    query_id: uuid.UUID
    answer: str
    citations: list[int]
    retrieved_chunk_ids: list[int]
    reranked_chunk_ids: list[int]
    sources: list[Source]
    timings_ms: dict[str, float]
    # The effective read-path settings for this answer (P-UI): lets the chat show what
    # produced each message so a knob's effect is legible per query.
    applied: AppliedSettings


@router.post("/ask", response_model=AskResponse)
async def ask(
    request: AskRequest,
    session: AsyncSession = Depends(get_session),
) -> AskResponse:
    """Answer `query` from the indexed corpus, and record the whole query."""
    settings = get_settings()

    # INPUT guard (P2). A blank or over-long question is a caller error we can know
    # synchronously — reject it before retrieval embeds anything or generation spends a
    # billable call. (Same stance as the empty-upload 400 in documents.py: validate what
    # we can, now.) validate_query is framework-free and raises plain ValueError
    # subclasses; we map both to a 400 here so guards.py stays FastAPI-free.
    try:
        query = validate_query(request.query, max_chars=settings.max_query_chars)
    except (EmptyQueryError, QueryTooLongError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    started = perf_counter()

    # EFFECTIVE read-path settings for THIS request. Each knob is the request override
    # when given, else the Settings default. `rerank_enabled` / `cache_enabled` use
    # `None` to mean "not overridden"; `rerank_score_floor`'s `None` is meaningful
    # (floor disabled), so "not overridden" is detected via `model_fields_set` (was the
    # field in the payload?). These effective values drive the cache gate, the call into
    # retrieve(), and the `applied` echo — resolved once, here, so all three agree.
    rerank_on = (
        settings.rerank_enabled
        if request.rerank_enabled is None
        else request.rerank_enabled
    )
    cache_on = (
        settings.cache_enabled
        if request.cache_enabled is None
        else request.cache_enabled
    )
    floor = (
        request.rerank_score_floor
        if "rerank_score_floor" in request.model_fields_set
        else settings.rerank_score_floor
    )
    applied = AppliedSettings(
        rerank_enabled=rerank_on,
        rerank_score_floor=floor,
        cache_enabled=cache_on,
        cache_hit=False,  # flipped to True on the hit path below
    )

    # SEMANTIC CACHE (P3), gated. When enabled, embed the query ONCE here and reuse the
    # vector for both the cache lookup and — on a miss — retrieval, so a miss never pays
    # Gemini for the same embed twice. On a HIT we return the stored answer without
    # retrieving or generating. Cache-off: this whole block is skipped, query_embedding
    # stays None, retrieve() embeds internally, and ask() is byte-identical to Q10/P2.
    query_embedding: list[float] | None = None
    if cache_on:
        t = perf_counter()
        query_embedding = await embed_query(query)
        embed_ms = round((perf_counter() - t) * 1000, 1)

        t = perf_counter()
        hit = await cache.cache_get(query_embedding, DEV_OWNER_ID)
        cache_lookup_ms = round((perf_counter() - t) * 1000, 1)

        if hit is not None:
            query_id = uuid.uuid4()
            timings = {
                "embed_ms": embed_ms,
                "cache_lookup_ms": cache_lookup_ms,
                "total_ms": round((perf_counter() - started) * 1000, 1),
            }
            # The audit contract still fires on a hit — same two sinks — but token
            # counts are null (no generation ran) and cache_hit=true, so hit-rate and
            # cost-saved are computable from the log. The stored citations are already
            # the P2-repaired (post-guard) list, so a hit inherits the guarantee the
            # miss that created it earned. The row write stays best-effort as below.
            logger.info(
                "ask.query %s",
                json.dumps(
                    {
                        "query_id": str(query_id),
                        "query": query,
                        "answer": hit.answer,
                        "citations": hit.citations,
                        "valid_citations": hit.valid_citations,
                        "phantom_citations": hit.phantom_citations,
                        "retrieved_chunk_ids": hit.retrieved_chunk_ids,
                        "reranked_chunk_ids": hit.final_chunk_ids,
                        "context_chars": hit.context_chars,
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                        "cache_hit": True,
                        "timings_ms": timings,
                    }
                ),
            )
            try:
                session.add(
                    QueryLog(
                        id=query_id,
                        owner_id=DEV_OWNER_ID,
                        query=query,
                        answer=hit.answer,
                        citations=hit.citations,
                        retrieved_chunk_ids=hit.retrieved_chunk_ids,
                        final_chunk_ids=hit.final_chunk_ids,
                        context_chars=hit.context_chars,
                        prompt_tokens=None,
                        completion_tokens=None,
                        total_tokens=None,
                        generation_model=hit.generation_model,
                        # cache_hit rides in the JSONB (no migration) alongside timings.
                        timings_ms={**timings, "cache_hit": True},
                    )
                )
                await session.commit()
            except (
                Exception
            ):  # noqa: BLE001 — never fail the answer over an audit write
                logger.exception("ask.query_log_write_failed query_id=%s", query_id)

            return AskResponse(
                query_id=query_id,
                answer=hit.answer,
                citations=hit.valid_citations,
                retrieved_chunk_ids=hit.retrieved_chunk_ids,
                reranked_chunk_ids=hit.final_chunk_ids,
                # Replay the stored passages (plain dicts) back into Source models, so a
                # hit reproduces exactly the response a fresh miss would. A pre-P6 cache
                # entry has no `sources` (defaults to []) — degrades to id-only, never
                # errors.
                sources=[Source(**s) for s in hit.sources],
                timings_ms=timings,
                # Same effective settings, but this answer DID come from the cache.
                applied=applied.model_copy(update={"cache_hit": True}),
            )

    # MISS (or cache disabled). query_embedding is the shared vector on a miss, or None
    # when the cache is off (retrieve() then embeds internally, exactly as in Q10). The
    # effective rerank/floor overrides ride into retrieve() here; on a hit above they
    # never applied (retrieval was skipped), which is why `applied.cache_hit` exists.
    t = perf_counter()
    result = await retrieve(
        query,
        query_embedding=query_embedding,
        rerank_enabled=rerank_on,
        rerank_score_floor=floor,
    )
    retrieve_ms = round((perf_counter() - t) * 1000, 1)

    t = perf_counter()
    answer = await generate(query, result.chunks)
    generate_ms = round((perf_counter() - t) * 1000, 1)

    # Minted here (not by the DB) so the query has an id even if the audit write below
    # fails: it is the correlation id in the response AND in the log line.
    query_id = uuid.uuid4()
    final_ids = [sc.chunk.id for sc in result.chunks]
    context_chars = sum(len(sc.chunk.content) for sc in result.chunks)
    # The shown passages, response-only (P6): the chunks the model actually saw, in
    # shown order, so the UI can render every passage and highlight the cited ones. Free
    # — `result.chunks` is already in memory. Kept as plain dicts here too so the same
    # value serializes into the cache (asdict-friendly) without a second shape.
    source_dicts = [
        {"id": sc.chunk.id, "content": sc.chunk.content, "score": sc.score}
        for sc in result.chunks
    ]

    # OUTPUT guard (P2). The model was shown ONLY `final_ids`, so any citation
    # outside that set is a phantom (invented, or lifted from the wider candidate
    # pool it never saw). The partition is pure and always computed, so the audit
    # log records the violation regardless of the kill-switch.
    # `citation_guard_enabled` (default on) controls the two *actions*: flag it (a
    # WARNING) and repair the client response (drop the phantom ids). Turned off, the
    # model's raw citations flow straight through — a way to measure raw grounding in
    # eval — while the log still shows what happened. Repair-and-flag, not
    # fail-closed: the answer was already generated and billed, so we never turn a
    # phantom citation into a 500 (the same best-effort stance as the audit write).
    check = check_citations(answer.citations, final_ids)
    guard_on = settings.citation_guard_enabled
    if guard_on and check.has_violation:
        logger.warning(
            "ask.citation_violation query_id=%s phantom=%s shown=%s",
            query_id,
            check.phantom_citations,
            final_ids,
        )
    client_citations = check.valid_citations if guard_on else answer.citations

    timings = {
        "retrieve_ms": retrieve_ms,
        "generate_ms": generate_ms,
        "total_ms": round((perf_counter() - started) * 1000, 1),
    }
    usage = answer.usage

    # One greppable line per query. The stage breakdown from retrieval rides along under
    # its own key so the flat top-level timings stay stable as stages are added.
    log_record = {
        "query_id": str(query_id),
        "query": query,
        "answer": answer.answer,
        # RAW model citations (what the model did) stay under the frozen key; the P2
        # partition rides alongside as accumulated fields.
        "citations": answer.citations,
        "valid_citations": check.valid_citations,
        "phantom_citations": check.phantom_citations,
        "retrieved_chunk_ids": result.candidate_chunk_ids,
        "reranked_chunk_ids": final_ids,
        "context_chars": context_chars,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        # Did the lexical arm contribute to this query? (A cache HIT never reaches here,
        # so this is always a real true/false, never NULL.)
        "lexical_matched": result.lexical_matched,
        "timings_ms": {**timings, "retrieval": result.timings_ms},
    }
    # With the cache on (effective), tag the miss so hit-rate is computable from the log
    # stream. With the cache off the key is omitted, keeping the line byte-identical to
    # Q10/P2. `cache_on` is the effective flag (request override or Settings default).
    if cache_on:
        log_record["cache_hit"] = False
    logger.info("ask.query %s", json.dumps(log_record))

    row_timings = {**timings, **result.timings_ms}
    if cache_on:
        row_timings["cache_hit"] = False

    # The durable half. Best-effort: an audit failure must not cost the user an answer
    # that has already been generated (and billed), so it is logged and swallowed.
    try:
        session.add(
            QueryLog(
                id=query_id,
                owner_id=DEV_OWNER_ID,
                query=query,
                answer=answer.answer,
                citations=answer.citations,
                retrieved_chunk_ids=result.candidate_chunk_ids,
                final_chunk_ids=final_ids,
                context_chars=context_chars,
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
                generation_model=get_settings().generation_model,
                lexical_matched=result.lexical_matched,
                timings_ms=row_timings,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — deliberately broad: never fail the answer
        logger.exception("ask.query_log_write_failed query_id=%s", query_id)

    # Populate the cache with the POST-GUARD answer so the next paraphrase can skip this
    # whole pipeline. Only on a real miss (query_embedding is the vector we looked up
    # with); cache-off leaves it None and this is skipped. cache_set is fail-open — a
    # write failure is logged inside cache.py and never reaches here.
    if cache_on and query_embedding is not None:
        await cache.cache_set(
            query_embedding,
            DEV_OWNER_ID,
            cache.CachedAnswer(
                answer=answer.answer,
                citations=answer.citations,
                valid_citations=check.valid_citations,
                phantom_citations=check.phantom_citations,
                retrieved_chunk_ids=result.candidate_chunk_ids,
                final_chunk_ids=final_ids,
                context_chars=context_chars,
                generation_model=get_settings().generation_model,
                sources=source_dicts,
            ),
        )

    return AskResponse(
        query_id=query_id,
        answer=answer.answer,
        # The one client-visible P2 change: the repaired list (phantom ids dropped
        # when the guard is on; a no-op when there's no phantom or the guard is off).
        # The RAW list is preserved above in both audit sinks, so nothing is hidden.
        citations=client_citations,
        retrieved_chunk_ids=result.candidate_chunk_ids,
        reranked_chunk_ids=final_ids,
        sources=[Source(**s) for s in source_dicts],
        timings_ms=timings,
        applied=applied,  # cache_hit stays False — this answer was freshly generated.
    )


@router.get("/ask/config", response_model=AskConfig)
async def ask_config() -> AskConfig:
    """The read-path defaults the chat control panel initialises from.

    Read-only: it reflects the current `Settings` so the UI panel opens showing what the
    server would do BEFORE any per-request override (the architect's "panel mirrors the
    running env" decision). Phase 3: gate/authorize this with `/ask` once auth lands.
    """
    settings = get_settings()
    return AskConfig(
        rerank_enabled=settings.rerank_enabled,
        cache_enabled=settings.cache_enabled,
        rerank_score_floor=settings.rerank_score_floor,
    )
