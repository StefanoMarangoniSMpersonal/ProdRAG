# Session Handoff — resume here next time

_Last updated: 2026-07-23._

**Only what's live and what's next.** Per-milestone detail, seams, rationale and gotchas live in
`CLAUDE.md`, `docs/PROGRESS.md` (layer tracker) + `docs/PROGRESS-PHASE2.5.md` (P0–P6 status),
`docs/PHASE2-PLAN.md` + `docs/PHASE2.5-PLAN.md` (approved milestone breakdowns), and auto-memory
(`phase1-ingestion`, `phase2-retrieval`, loaded every session). Don't re-narrate them here —
point to them. Read this + `CLAUDE.md` to pick up.

## Current goal

**Phase 2.5 — read-path hardening + frontend.** Phase 2 (retrieval, Q1–Q10) is **COMPLETE**,
committed `fdd9b85` (unpushed). What the finished pipeline does and every Q1–Q10 decision: see the
`phase2-retrieval` memory. The Phase 2.5 milestone plan (P0–P6, backend-first, **auth deferred to
Phase 3**) is **`docs/PHASE2.5-PLAN.md`** (spec); live status is **`docs/PROGRESS-PHASE2.5.md`**.

**P0 (reranker warm-up) is DONE — uncommitted.** `app/main.py` gained a `lifespan` handler that
warms `_get_reranker` via `asyncio.to_thread`, gated on `rerank_enabled`, best-effort (a warm-up
failure is logged and swallowed). `test_warmup.py` (3) + `docs/learning/P0-warmup.md`. Suite
**147+5**, 0-skip. Still uncommitted alongside the Phase 2.5 docs (plan, this handoff, both progress
files) — none of the P0 / doc work has been committed yet.

## Immediate next step

**P1 — SSE streaming on `POST /ask`.** Add a streaming variant of `generate()`
(`generate_content_stream`) and a `StreamingResponse` branch in `app/api/ask.py`; the non-streaming
path stays. **First decision = ADR `0003`** (stream plain text vs stream structured JSON — structured
output fights token streaming). Must keep the frozen audit-log contract on the streaming path.
Test-first. Full spec in `docs/PHASE2.5-PLAN.md`; status table in `docs/PROGRESS-PHASE2.5.md`.

## Parked architect decisions (open, not blocking P0)

1. **Trap faithfulness 0.597 artifact** — fix via a refusal-specific metric or an always-explain-
   refusals prompt change (a generator change, your call). Context: `eval-metrics-honest-labeling`
   memory.
2. **`tests/test_parse.py`'s two commented-out `Falcon-9X` asserts** — a silently weakened
   *immutable* test; restoring them is a spec decision (yours).

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
  `eval-run` skill. Rerank is OFF unless `RERANK_ENABLED=true` before starting the API. Windows
  env gotchas (venv path, `curl.exe`, psql `NOTICE`): `phase1-ingestion` memory.

## Working rules (full text: CLAUDE.md)

Test-first & immutable (fix the code, never the test; if the spec is wrong, **stop and ask**) ·
RAG core = slow down and teach · glue = move fast · **architecture decisions are the human's**.
