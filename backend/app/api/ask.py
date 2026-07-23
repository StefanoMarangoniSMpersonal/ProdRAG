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
    """The question. One field today; a `k` override or filters would land here."""

    query: str = Field(description="The user's question.")


class AskResponse(BaseModel):
    """The answer plus the evidence trail that produced it.

    `citations` are chunk ids the answer drew on (ADR 0002's chunk-level granularity).
    Both id lists are returned, not just the final one: a caller (and the UI) can see
    what retrieval surfaced AND what survived reranking. `query_id` correlates this
    response with its log line and its `query_logs` row.
    """

    query_id: uuid.UUID
    answer: str
    citations: list[int]
    retrieved_chunk_ids: list[int]
    reranked_chunk_ids: list[int]
    timings_ms: dict[str, float]


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

    # SEMANTIC CACHE (P3), gated. When enabled, embed the query ONCE here and reuse the
    # vector for both the cache lookup and — on a miss — retrieval, so a miss never pays
    # Gemini for the same embed twice. On a HIT we return the stored answer without
    # retrieving or generating. Cache-off: this whole block is skipped, query_embedding
    # stays None, retrieve() embeds internally, and ask() is byte-identical to Q10/P2.
    query_embedding: list[float] | None = None
    if settings.cache_enabled:
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
                timings_ms=timings,
            )

    # MISS (or cache disabled). query_embedding is the shared vector on a miss, or None
    # when the cache is off (retrieve() then embeds internally, exactly as in Q10).
    t = perf_counter()
    result = await retrieve(query, query_embedding=query_embedding)
    retrieve_ms = round((perf_counter() - t) * 1000, 1)

    t = perf_counter()
    answer = await generate(query, result.chunks)
    generate_ms = round((perf_counter() - t) * 1000, 1)

    # Minted here (not by the DB) so the query has an id even if the audit write below
    # fails: it is the correlation id in the response AND in the log line.
    query_id = uuid.uuid4()
    final_ids = [sc.chunk.id for sc in result.chunks]
    context_chars = sum(len(sc.chunk.content) for sc in result.chunks)

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
        "timings_ms": {**timings, "retrieval": result.timings_ms},
    }
    # With the cache on, tag the miss so hit-rate is computable from the log stream.
    # With the cache off the key is omitted, keeping the line byte-identical to Q10/P2.
    if settings.cache_enabled:
        log_record["cache_hit"] = False
    logger.info("ask.query %s", json.dumps(log_record))

    row_timings = {**timings, **result.timings_ms}
    if settings.cache_enabled:
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
    if settings.cache_enabled and query_embedding is not None:
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
        timings_ms=timings,
    )
