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

from app.config import get_settings
from app.db import get_session
from app.generate.generate import generate
from app.models import DEV_OWNER_ID, QueryLog
from app.retrieve.retrieve import retrieve

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
    query = request.query.strip()
    if not query:
        # A blank question is a caller error we can know synchronously — reject it
        # before retrieval embeds anything or generation spends a billable call. (Same
        # stance as the empty-upload 400 in documents.py: validate what we can, now.)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query must not be empty.",
        )

    started = perf_counter()

    t = perf_counter()
    result = await retrieve(query)
    retrieve_ms = round((perf_counter() - t) * 1000, 1)

    t = perf_counter()
    answer = await generate(query, result.chunks)
    generate_ms = round((perf_counter() - t) * 1000, 1)

    # Minted here (not by the DB) so the query has an id even if the audit write below
    # fails: it is the correlation id in the response AND in the log line.
    query_id = uuid.uuid4()
    final_ids = [sc.chunk.id for sc in result.chunks]
    context_chars = sum(len(sc.chunk.content) for sc in result.chunks)
    timings = {
        "retrieve_ms": retrieve_ms,
        "generate_ms": generate_ms,
        "total_ms": round((perf_counter() - started) * 1000, 1),
    }
    usage = answer.usage

    # One greppable line per query. The stage breakdown from retrieval rides along under
    # its own key so the flat top-level timings stay stable as stages are added.
    logger.info(
        "ask.query %s",
        json.dumps(
            {
                "query_id": str(query_id),
                "query": query,
                "answer": answer.answer,
                "citations": answer.citations,
                "retrieved_chunk_ids": result.candidate_chunk_ids,
                "reranked_chunk_ids": final_ids,
                "context_chars": context_chars,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "timings_ms": {**timings, "retrieval": result.timings_ms},
            }
        ),
    )

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
                timings_ms={**timings, **result.timings_ms},
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — deliberately broad: never fail the answer
        logger.exception("ask.query_log_write_failed query_id=%s", query_id)

    return AskResponse(
        query_id=query_id,
        answer=answer.answer,
        citations=answer.citations,
        retrieved_chunk_ids=result.candidate_chunk_ids,
        reranked_chunk_ids=final_ids,
        timings_ms=timings,
    )
