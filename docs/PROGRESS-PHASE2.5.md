# Phase 2.5 Progress Tracker (P0–P6)

> Milestone status for **Phase 2.5 — read-path hardening + frontend**. The *spec* (scope,
> seams, forks, teaching plan) is `docs/PHASE2.5-PLAN.md`; per-milestone rationale lives in
> each `docs/learning/P<n>-*.md` note and the `phase2-retrieval` memory. This file is only
> the status table — it does not re-narrate the plan.
>
> Sequencing = **backend-first** (P0→P5 measured against the eval harness), frontend last
> (P6). **Auth is deferred to Phase 3** — every milestone keeps `owner_id` as the seam it
> already is (`DEV_OWNER_ID` today). The per-query audit-log contract (`ask.query` line +
> `query_logs` row) is **frozen**: every backend milestone must preserve it.
>
> _Last updated: 2026-07-23 — P0 complete (uncommitted); next = P1._

## Milestones

Each ships **test-first code + a teaching note** (`docs/learning/P<n>-*.md`); genuine forks
also get an ADR (`docs/adr/`, next serial `0003`).

| Milestone | What | Status |
|-----------|------|--------|
| P0 | Reranker warm-up on startup — FastAPI `lifespan` warms `_get_reranker` in a thread, **gated on `rerank_enabled`** | ✅ **code + test + note complete (uncommitted)** — `app/main.py` lifespan handler (warm via `asyncio.to_thread`, best-effort/swallowed on failure); kills the 24.8 s cold first `/ask` → warm ~2.3 s. No new dep, no config field, no pipeline/schema change; immutable seams untouched (`ASGITransport` skips lifespan). `test_warmup.py` (3, red-first) + `docs/learning/P0-warmup.md`. Suite **147+5**, 0-skip. Live warm-vs-cold check still to run by the architect |
| P1 | SSE streaming on `POST /ask` — `generate_content_stream` + `StreamingResponse`; non-streaming path stays | ⬜ planned — **biggest perceived-latency win.** Fork → ADR `0003` (stream plain text vs stream structured JSON). Must preserve the audit-log contract on the streaming path. Spec: `PHASE2.5-PLAN.md` |
| P2 | Guardrails I/O — validate `GeneratedAnswer.citations` against the actually-retrieved chunk ids | ⬜ planned — naive-first = hand-rolled validator; Guardrails-AI library only if `import`-clean vs the pinned deps. Fork → ADR `0004`. Spec: `PHASE2.5-PLAN.md` |
| P3 | Redis semantic cache — cache before retrieval; skip retrieve+generate on a near-duplicate query | ⬜ planned — new `app/cache.py` within **`redis<6.5`**; needs `retrieve()` to expose the query vector (also useful for P4); event-loop-bound client. Spec: `PHASE2.5-PLAN.md` |
| P4 | Query rewrite / HyDE — transform the query at the top of `retrieve()` | ⬜ planned — RAG-core fork → ADR `0005`; **eval decides** (re-run `eval.run` rerank-ON vs the Q7 baseline). Semantic vs lexical arms take different texts. Spec: `PHASE2.5-PLAN.md` |
| P5 | LangGraph orchestration of the read path — make the linear pipeline an explicit graph | ⬜ planned — ⚠ **immutable-test risk**: graph nodes may relocate seams Phase 2 tests pin → **STOP and ask** (spec revision, architect's call). New dep `langgraph` must prove pin-clean. Spec: `PHASE2.5-PLAN.md` |
| P6 | Chat + upload frontend (Next.js App Router) — consumes the P1 SSE stream + the 202-poll upload | ⬜ planned — glue-tier, move fast; **NO auth** (runs on `DEV_OWNER_ID`). Spec: `PHASE2.5-PLAN.md` |

## Deferred to Phase 3 (out of Phase 2.5)

Supabase Auth end-to-end (JWT/ES256/JWKS + RLS + Supavisor claims), M-enrich (table/image
LLM summaries, still eval-gated), LangSmith/Sentry tracing + cost dashboards, S3 blob store.
Detail in `docs/PHASE2.5-PLAN.md` ("Deferred to Phase 3").
