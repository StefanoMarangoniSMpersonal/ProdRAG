# Session Handoff — resume here next time

_Last updated: 2026-07-18._

This file holds only **what's live and what's next**. Per-milestone detail, seam shapes,
rationale, and gotchas live in the code, `docs/PROGRESS.md` (layer % tracker),
`docs/PHASE2-PLAN.md` (approved Q1–Q10), and auto-memory (`phase1-ingestion`,
`phase2-retrieval`, loaded every session). Read this + `CLAUDE.md` (the constitution) to pick up.

## Current goal

**Phase 2 — retrieval pipeline.** Build the query path end to end:
embed query → hybrid (semantic + lexical) → RRF fusion → cross-encoder rerank → generate.
Scope for Phase 2 = a working `POST /ask`; LangGraph, Guardrails, and streaming are Phase 2.5.

## What's done

- **Phase 0** spine (Next.js → FastAPI → Postgres/pgvector → back).
- **Phase 1** ingestion, M0–M8: schema → storage → parse → chunk → embed → write →
  orchestrator → upload endpoint → **async layer** (Celery worker + Redis broker + fenced
  reaper). Proven live end to end (HTTP → worker → `ready`).
- **Phase 2 Q1–Q6:** retrieval indexes (HNSW + `tsv`/GIN) → semantic search → `retrieve()`
  orchestrator → eval harness (hit@k / MRR) → lexical (full-text) → **hybrid RRF fusion**.
  `retrieve()` runs semantic + lexical concurrently and fuses by Reciprocal Rank Fusion.
  Committed & pushed through `716c4a2`.
- **Phase 2 Q7 — cross-encoder rerank (DONE, this session, uncommitted at time of writing).**
  `app/retrieve/rerank.py`: a **local** cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`)
  loaded through the **already-installed** `transformers` (torch is present via `unstructured[pdf]`)
  → **zero new pip deps**, ~90 MB one-time model download. `retrieve()` gained a gated
  pool-widen → rerank → truncate branch: with `rerank_enabled` on, each arm fetches
  `retrieval_candidate_k` (50), RRF fuses the pool, the cross-encoder re-scores it, top-`k` returned
  (its logit becomes `ScoredChunk.score`). **Gated OFF by default** (`RERANK_ENABLED=true` to turn
  on) → rerank-off is byte-identical to Q6, so the 4 immutable Q3 + 7 fuse specs stayed green
  UNTOUCHED (no immutable-test revision this milestone). Provider ADR: `docs/adr/0001-reranker-provider.md`;
  teaching note `docs/learning/Q7-rerank.md`.
- **Latest eval — Q7 rerank ON, 48 Q / 9 docs / 97 chunks**
  (`eval/results/retrieval-20260718T192246_887014Z.json`):

  > **MRR 0.927 · hit@1 0.896 · hit@3 0.938 · hit@5 1.000 · hit@10 1.000**
  > vs the Q6 hybrid baseline (0.895 / 0.812 / 0.979 / 0.979 / 1.000): **+0.032 MRR, +0.084 hit@1**
  > (5 questions promoted to rank 1), hit@5 perfected — **accepted a −0.041 hit@3 regression**
  > (2 questions dropped from top-3: id 22 "NovaBridge rate limit" fell rank 1→4, id 47 a revenue
  > comparison). Architect decision (2026-07-18): a net win on the target lever; keep MiniLM, the
  > model is a config swap behind the `rerank` seam if hit@3 ever matters more.

- Offline test suite: **81 pass + 3 deselected (live)**, 0 skip; ruff + black clean on `app/`.

## Immediate next step

**Q8 — generation (grounded, cited answer).** A NEW Gemini *generation* client (only the
*embedding* client exists today; reuses the `google-genai` SDK → no new dep) in
`app/generate/generate.py`. System instruction: answer **only from provided context**, say "I
don't know" when it's absent, **cite source chunk IDs**. Pydantic model for structured
`{answer, citations[]}`. Module-level seam, mirroring the retrieve stages. Carries an **ADR**
(generation model id; citation granularity chunk-level vs char-span) — **an architecture
decision, ask the architect before coding.** Also folds in the 10 **trap** questions (deferred
from Q4) for a refusal eval. Ship test-first + `docs/learning/Q8-*.md`. Full breakdown: Q8 in
`docs/PHASE2-PLAN.md`.

## Open blockers / cautions

- **⚠ DO NOT re-ingest and DO NOT wipe the dev DB.** `eval/golden.jsonl` is keyed to the exact
  chunk-ids in the `infra_pgdata` volume; re-ingest churns `BIGINT IDENTITY` ids and invalidates
  the approved golden set (would force a `build_golden.py` re-run + re-approval). Re-running
  `eval.run` is safe and repeatable. Use `docker compose ... up -d` — **never** `-v`.
- **Reaper unverified live** — the M8 smoke proved the happy path, not the reaper requeue. The
  fence logic is covered by `test_worker.py`, so this is an optional live-confidence check
  (shorten `ingest_stuck_after_seconds`, leave a stale `processing` row, watch Beat requeue +
  `attempt` bump).
- **Lint debt (optional cleanup):** `tests/test_parse.py` isn't `black`-clean (two commented-out
  lines). It's an **immutable** test file — left untouched on purpose; worth a separate
  formatting-only commit if ever.
- **Credentials:** a PAT was once exposed in chat — **revoke it**; use `gh auth login` or SSH.

## How to run & test (fresh session assumes nothing is running)

```powershell
.\dev.ps1     # setup-if-needed (venv, deps, .env, migrations) + starts everything
.\stop.ps1    # stops Postgres/Redis  (.\stop.ps1 -Wipe drops pgdata — see caution above)
```
`dev.ps1` opens four windows: API (:8000), Celery worker (`--pool=solo`), Celery Beat/reaper
(separate process — embedded `-B` is broken on Windows), frontend (:3000). Verify:
`curl.exe http://localhost:8000/health/db`.

- **Tests:** run from repo root via the project venv explicitly —
  `backend\.venv\Scripts\python.exe -m pytest`. A **SKIP exits 0 → treat skips as a false green.**
  Docker daemon must be up (testcontainers ERROR, never SKIP, if down). Two PDF `hi_res` tests
  need poppler + tesseract (installed). The 3 live tests run with `-m live` + `GEMINI_API_KEY`.
- **Two DBs, don't confuse them:** the suite's testcontainers Postgres is **ephemeral**; the
  **persistent dev DB** is `prodrag-postgres` (docker-compose, `localhost:5432`, all creds
  `prodrag`, volume `infra_pgdata`) — where real uploads land and eval reads.
- **Re-run the eval** (Postgres + `GEMINI_API_KEY` only — no API/worker/Beat needed):
  `docker compose -f infra/docker-compose.yml up -d`, then
  `backend\.venv\Scripts\python.exe -m eval.run --golden eval/golden.jsonl --out eval/results/`.
  That measures the **hybrid (Q6)** pipeline. To measure **with Q7 rerank**, prepend
  `$env:RERANK_ENABLED="true";` (first run downloads the ~90 MB cross-encoder to
  `~/.cache/huggingface`, then ~0.5–2 s/query CPU). Rerank is OFF by default everywhere else.
- **Inspect the dev DB:** `docker exec -it prodrag-postgres psql -U prodrag -d prodrag`, or VS Code
  SQLTools; ready queries in `infra/db/explore.sql`.
- **Curl on Windows:** use `curl.exe`, not bare `curl` (aliased to `Invoke-WebRequest`).

## Working rules (full text: CLAUDE.md)

- **Test-first & immutable:** write the failing test first; once written a test is immutable —
  fix the code, never the test. If the spec is wrong, **stop and ask** (architect's call).
- **RAG core = slow down and teach** (name rejected alternatives, define terms). Glue = move fast.
- **Architecture/design decisions are the human's** — don't silently pick one; ask.
