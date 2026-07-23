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
> _Last updated: 2026-07-23 — P0 committed `2242256`; P2 committed `7e76500`; P1 parked; P3 complete (uncommitted); next = P4._

## Milestones

Each ships **test-first code + a teaching note** (`docs/learning/P<n>-*.md`); genuine forks
also get an ADR (`docs/adr/`, next serial `0003`).

| Milestone | What | Status |
|-----------|------|--------|
| P0 | Reranker warm-up on startup — FastAPI `lifespan` warms `_get_reranker` in a thread, **gated on `rerank_enabled`** | ✅ **committed `2242256`** — `app/main.py` lifespan handler (warm via `asyncio.to_thread`, best-effort/swallowed on failure); kills the 24.8 s cold first `/ask` → warm ~2.3 s. No new dep, no config field, no pipeline/schema change; immutable seams untouched (`ASGITransport` skips lifespan). `test_warmup.py` (3, red-first) + `docs/learning/P0-warmup.md`. Suite **147+5**, 0-skip. Live warm-vs-cold check still to run by the architect |
| P1 | SSE streaming on `POST /ask` — `generate_content_stream` + `StreamingResponse`; non-streaming path stays | ⏸ **PARKED** (architect, 2026-07-23) — perceived-latency UX polish, not blocking core function; fine trading latency for correctness. Unparks cleanly (non-streaming `/ask` untouched). Fork → ADR `0003`. Spec: `PHASE2.5-PLAN.md` |
| P2 | Guardrails I/O — validate `GeneratedAnswer.citations` against the actually-shown chunk ids | ✅ **complete + committed** — `app/guards.py` (framework-free `validate_query` input guard [blank + `max_query_chars` cap, pre-spend 400] + `check_citations` output guard → `CitationCheck` valid/phantom partition against `final_ids`, NOT the wider candidate pool). `ask.py` wired: phantom → `ask.citation_violation` WARNING + repaired `AskResponse.citations`; **audit contract accumulated** (stdout line keeps RAW `citations`, adds `valid_citations`/`phantom_citations`; `query_logs` keeps RAW, no migration). Repair-and-flag not fail-closed (200, never discard a billed answer); kill-switch `citation_guard_enabled`. `test_guardrails.py` (13, red-first) + `docs/learning/P2-guardrails.md` + ADR `0004`. Immutable `test_ask.py` untouched (repair no-op on `[13]⊆{13,11}`). Suite **160+5**, 0-skip. No eval re-run (retrieval + answer text unchanged) |
| P3 | Redis semantic cache — cache before retrieval; skip retrieve+generate on a near-duplicate query | ✅ **complete (uncommitted)** — `app/cache.py` (fail-open async semantic cache: per-owner `cache:{owner_id}` key → JSON array of `{vector, ts, payload}`, cosine scan ≥ `cache_similarity_threshold`, TTL + `max_entries` eviction; `_get_redis` lru-cache seam, reuses `celery[redis]` client **no new dep**, on **Redis DB /1**). `retrieve()` gained `embed_query()` + optional `query_embedding=` (share ONE embed, no double-embed on a miss; additive → immutable Q3 tests untouched). `ask.py` gated on `cache_enabled` (**default OFF** → cache-off byte-identical to Q10/P2, immutable `test_ask.py` green): hit skips retrieve+generate, returns post-guard answer; **audit contract accumulated** — `cache_hit` on both sinks (stdout field + `query_logs.timings_ms` JSONB, no migration), tokens null on a hit. `test_cache.py` (10, unit/offline) + `test_ask_cache.py` (3, endpoint), red-first + `docs/learning/P3-semantic-cache.md`. Suite **173+5**, 0-skip. **Live-proven**: paraphrase hit replayed a cached answer in ~0 pipeline ms (retrieve+generate skipped), audit `cache_hit` true/false correct on both sinks |
| P4 | Query rewrite / HyDE — transform the query at the top of `retrieve()` | ⬜ planned — RAG-core fork → ADR `0005`; **eval decides** (re-run `eval.run` rerank-ON vs the Q7 baseline). Semantic vs lexical arms take different texts. Spec: `PHASE2.5-PLAN.md` |
| P5 | LangGraph orchestration of the read path — make the linear pipeline an explicit graph | ⬜ planned — ⚠ **immutable-test risk**: graph nodes may relocate seams Phase 2 tests pin → **STOP and ask** (spec revision, architect's call). New dep `langgraph` must prove pin-clean. Spec: `PHASE2.5-PLAN.md` |
| P6 | Chat + upload frontend (Next.js App Router) — consumes the P1 SSE stream + the 202-poll upload | ⬜ planned — glue-tier, move fast; **NO auth** (runs on `DEV_OWNER_ID`). Spec: `PHASE2.5-PLAN.md` |

## Deferred to Phase 3 (out of Phase 2.5)

Supabase Auth end-to-end (JWT/ES256/JWKS + RLS + Supavisor claims), M-enrich (table/image
LLM summaries, still eval-gated), LangSmith/Sentry tracing + cost dashboards, S3 blob store.
Detail in `docs/PHASE2.5-PLAN.md` ("Deferred to Phase 3").

## Deferred — future guardrails phase (not Phase 3, its own milestone)

**Input-guard expansion: prompt-injection detection + PII screening.** P2 shipped only the cheap,
exact input guards (blank + length cap). Adversarial/probabilistic guards want their own threat
model, eval set, and failure policy (block / sanitize / flag) — a milestone, not a rider — and
land in the same `app/guards.py`. Rationale + revisit triggers: **ADR `0004`**.
