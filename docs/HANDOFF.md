# Session Handoff — resume here next time

_Last updated: 2026-07-24._

**Only what's live and what's next.** Per-milestone detail, seams, rationale and gotchas live in
`CLAUDE.md`, `docs/PROGRESS.md` (layer tracker) + `docs/PROGRESS-PHASE2.5.md` (P0–P6 status),
`docs/PHASE2-PLAN.md` + `docs/PHASE2.5-PLAN.md` (approved milestone breakdowns), and auto-memory
(`phase1-ingestion`, `phase2-retrieval`, `phase2.5-hardening`, loaded every session). Don't re-narrate them here —
point to them. Read this + `CLAUDE.md` to pick up.

## Current goal

**Phase 2.5 — read-path hardening + frontend.** Phase 2 (retrieval, Q1–Q10) is **COMPLETE**,
committed `fdd9b85` (unpushed). What the finished pipeline does and every Q1–Q10 decision: see the
`phase2-retrieval` memory. The Phase 2.5 milestone plan (P0–P6, backend-first, **auth deferred to
Phase 3**) is **`docs/PHASE2.5-PLAN.md`** (spec); live status is **`docs/PROGRESS-PHASE2.5.md`**.

**P0 warm-up committed `2242256`; P2 guardrails committed `7e76500`; P3 semantic cache committed
`c3ad6bc`; P4 HyDE committed — eval gate RAN 2026-07-24, no gain, ships gated OFF; P5 LangGraph
refactor CODE DONE (uncommitted); P1 streaming PARKED.**
Per-milestone detail (seams, decisions, gotchas) is in the `phase2.5-hardening` memory + each
`docs/learning/P<n>-*.md` note + `docs/PROGRESS-PHASE2.5.md` — not re-narrated here. In brief:

- **P4 (HyDE query transform)** — new `app/retrieve/hyde.py` `generate_hypothetical(query)` (shared
  google-genai client, plain text out, **no new dep**). `retrieve()` gained a gated branch at the
  query→vector step: on `hyde_enabled` (**default OFF** → byte-identical to Q7) it generates a
  hypothetical answer passage and embeds it in the **DOCUMENT role** (`as_retrieval_document`) for
  the **semantic arm only**; the lexical arm keeps the raw query. **Fail-open** to raw-query embed
  on any error; records a `hyde_ms` timing. **N=1**, temp 0, dedicated `hyde_model`; 4 `hyde_*`
  Settings knobs. `test_hyde.py` (4) + `test_retrieve_hyde.py` (5), red-first +
  `docs/learning/P4-hyde.md` + **ADR `0005`**. Immutable tests untouched (gate OFF = no-op;
  `hyde_ms` safe vs the `>=` superset timings assert). Suite **182+5**, 0-skip.

## Immediate next step

**Commit P5**, then **P6 — chat + upload frontend** (Next.js App Router; glue-tier, move fast; **NO
auth**, runs on `DEV_OWNER_ID`). P1 streaming stays parked. Spec: `docs/PHASE2.5-PLAN.md`.

_P5 (LangGraph) is CODE DONE, uncommitted._ `retrieve()`'s internals are now a compiled **LangGraph**
graph (`app/retrieve/retrieve.py`: `_graph()` + module-level `_*_node` coroutines over a `GraphState`
TypedDict; **fan-out embed→(semantic ‖ lexical)→fan-in fuse** replaces `asyncio.gather`, each arm
keeps its own `SessionLocal()`; a **reducer** merges both arms' `timings`; the rerank gate is a
**conditional edge**). **Scope = `retrieve()` only** (ADR `0006`) → `ask.py` + the frozen audit
contract untouched. **Byte-identical output**, no new config, no migration. New dep
**`langgraph==1.2.9` pinned** (torch-free on the app import path; coexists with the load-bearing
pins). Immutable tests green **unedited** (the byte-identical proof); `test_read_graph.py` (9) added.
Suite **191+5**, 0-skip. **No eval re-run** needed. Only the live parity `curl` on `/ask` is left for
the architect to eyeball. Details: `docs/learning/P5-langgraph.md` + ADR `0006`.

_P4 (HyDE) is committed and done._ The eval gate ran 2026-07-24 (rerank ON): baseline and HyDE ON
came back **identical to 16 decimals** (MRR 0.9267 / hit@1 0.8958 / hit@3 0.9375 / hit@5,10 1.000) —
HyDE only churned ranks 6–10, and the **cross-encoder reranker masks HyDE** (both target the same
rank-2 near-miss). So **HyDE ships gated OFF** per ADR `0005`; the code stays as a studied,
reversible lever. Optional/deferred on quota: the isolation A/B with **rerank OFF**, which would
measure HyDE's raw retrieval effect before the reranker erases it.

## Parked architect decisions (open, not blocking P5)

1. **Trap faithfulness 0.597 artifact** — fix via a refusal-specific metric or an always-explain-
   refusals prompt change (a generator change, your call). Context: `eval-metrics-honest-labeling`
   memory.
2. **`tests/test_parse.py`'s two commented-out `Falcon-9X` asserts** — a silently weakened
   *immutable* test; restoring them is a spec decision (yours).
3. **Cache-key freshness (P3)** — the semantic cache keys only on `(owner_id, query_vector)`, so a
   newly-ingested doc isn't reflected until the 1 h TTL lapses. Fold a corpus/model epoch into the
   key (instant invalidation) vs event-based invalidation vs leave TTL-only — your call,
   **re-parked to the END of Phase 2.5** (2026-07-24: theory-over-impl for now, single-user).
   Context: `docs/learning/P3-semantic-cache.md` §7 + `phase2.5-hardening` memory.

## Open blockers / cautions

- **⚠ DO NOT re-ingest or wipe the dev DB** — `eval/golden.jsonl` is keyed to the exact chunk-ids
  in the `infra_pgdata` volume. `docker compose ... up -d`, **never** `-v` / `.\stop.ps1 -Wipe`.
- **⚠ `langchain-community<0.4` pin is load-bearing** (ragas 0.4.3 imports a module 0.4.x deleted).
  Also **`redis<6.5`** (Celery caps it). Don't unpin either during a dependency refresh.
- **Credentials:** a PAT was once exposed in chat — revoke it; use `gh auth login` or SSH.
- **Reaper requeue path** is unit-tested only (optional live-confidence check) — see the
  `phase1-ingestion` memory.

## How to run & test

- **Stack:** `.\dev.ps1` (API :8000, worker, Beat, frontend :3000) · `.\stop.ps1` (never `-Wipe`).
  Verify: `curl.exe http://localhost:8000/health/db`.
- **Tests:** `backend\.venv\Scripts\python.exe -m pytest` from repo root. **A SKIP exits 0 → false
  green** (`testing-skips-are-not-passes` memory). Docker daemon must be up (testcontainers).
- **Ask / eval / RAGAS commands + quotas:** in `docs/PHASE2-PLAN.md` (Verification) and the
  `eval-run` skill. Rerank is OFF unless `RERANK_ENABLED=true` and the **semantic cache** OFF unless
  `CACHE_ENABLED=true` (Redis DB /1) — both set before starting the API. Windows env gotchas (venv
  path, `curl.exe`, psql `NOTICE`): `phase1-ingestion` memory.

## Working rules (full text: CLAUDE.md)

Test-first & immutable (fix the code, never the test; if the spec is wrong, **stop and ask**) ·
RAG core = slow down and teach · glue = move fast · **architecture decisions are the human's**.
