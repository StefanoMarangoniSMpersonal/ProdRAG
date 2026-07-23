# Session Handoff — resume here next time

_Last updated: 2026-07-23._

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

**P0 (warm-up) committed `2242256`; P2 (guardrails) DONE — just committed. P1 (streaming) PARKED.**
P0: `app/main.py` `lifespan` warms `_get_reranker` (gated on `rerank_enabled`, best-effort). P2:
new framework-free `app/guards.py` — `validate_query` (blank + `max_query_chars` length cap → 400
pre-spend) and `check_citations` (partitions the model's citations into valid/phantom against
`final_ids`, the chunks actually shown). `ask.py` wired: phantom → `ask.citation_violation` WARNING
+ a repaired `AskResponse.citations` (repair-and-flag, HTTP 200 — never discard a billed answer);
the **frozen audit contract is accumulated, not weakened** (stdout line + `query_logs` row keep the
RAW model citations; the stdout line gains `valid_citations`/`phantom_citations`; no migration).
Kill-switch `citation_guard_enabled` (default on). `test_guardrails.py` (13, red-first) + ADR `0004`
+ `docs/learning/P2-guardrails.md`. Immutable `test_ask.py` untouched (repair is a no-op on the
existing `[13] ⊆ {13,11}` case). Suite **160+5**, 0-skip. Future input-guard expansion
(prompt-injection / PII) is deferred to its own phase (ADR `0004` + PROGRESS deferred list).

## Immediate next step

**P3 — Redis semantic cache** (P1 streaming is parked — perceived-latency polish, not blocking).
New `app/cache.py` within the **`redis<6.5`** pin: cache before retrieval, skip retrieve+generate
on a near-duplicate query. Needs `retrieve()` to expose the query vector (also useful for P4);
event-loop-bound client. Test-first. Full spec in `docs/PHASE2.5-PLAN.md`; status table in
`docs/PROGRESS-PHASE2.5.md`.

## Parked architect decisions (open, not blocking P3)

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
