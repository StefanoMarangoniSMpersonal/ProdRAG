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

**P0 warm-up committed `2242256`; P2 guardrails committed `7e76500`; P3 semantic cache DONE
(uncommitted); P1 streaming PARKED.** Per-milestone detail (seams, decisions, gotchas) is in the
`phase2.5-hardening` memory + each `docs/learning/P<n>-*.md` note + `docs/PROGRESS-PHASE2.5.md` —
not re-narrated here. In brief:

- **P3 (semantic cache)** — new fail-open `app/cache.py` (per-owner in-Redis cosine scan; **no new
  dep**, reuses the `celery[redis]` client on Redis DB **/1**). `retrieve()` gained `embed_query()`
  + an optional `query_embedding=` param (share one embed; also feeds P4). `ask.py` gated on
  `cache_enabled` (**default OFF** → cache-off byte-identical to Q10/P2, immutable `test_ask.py`/
  `test_retrieve.py` untouched); a hit skips retrieve+generate and replays the **post-guard**
  answer. **Audit contract accumulated** — `cache_hit` on both sinks (stdout field +
  `query_logs.timings_ms` JSONB; **no migration**), tokens null on a hit. `test_cache.py` (10) +
  `test_ask_cache.py` (3), red-first + `docs/learning/P3-semantic-cache.md`. Suite **173+5**,
  0-skip; **live-proven** (paraphrase hit, retrieve+generate skipped, both sinks correct).

## Immediate next step

**P4 — Query rewrite / HyDE** (P1 streaming stays parked). Transform the query at the top of
`retrieve()` — reuse the new `embed_query` seam; the semantic vs lexical arms may take different
texts. RAG-core fork → **ADR `0005`**, and **eval-gated**: re-run `eval.run` (rerank ON) vs the Q7
baseline (**MRR 0.927 · hit@1 0.896**) to decide if it stays. Full spec in `docs/PHASE2.5-PLAN.md`;
status table in `docs/PROGRESS-PHASE2.5.md`.

## Parked architect decisions (open, not blocking P4)

1. **Trap faithfulness 0.597 artifact** — fix via a refusal-specific metric or an always-explain-
   refusals prompt change (a generator change, your call). Context: `eval-metrics-honest-labeling`
   memory.
2. **`tests/test_parse.py`'s two commented-out `Falcon-9X` asserts** — a silently weakened
   *immutable* test; restoring them is a spec decision (yours).
3. **Cache-key freshness (P3)** — the semantic cache keys only on `(owner_id, query_vector)`, so a
   newly-ingested doc isn't reflected until the 1 h TTL lapses. Fold a corpus/model epoch into the
   key (instant invalidation) vs event-based invalidation vs leave TTL-only — your call, deferred to
   P4/later. Context: `docs/learning/P3-semantic-cache.md` §7 + `phase2.5-hardening` memory.

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
