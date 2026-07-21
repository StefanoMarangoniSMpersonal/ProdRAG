# Session Handoff — resume here next time

_Last updated: 2026-07-21._

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
- **Phase 2 Q7 — cross-encoder rerank (DONE, committed `a67a4a4`).**
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

- **Phase 2 Q8 — grounded, cited generation (DONE, committed `cd258f5`).**
  `app/generate/generate.py`: `generate(query, chunks) → GeneratedAnswer` — the final read-path
  stage, turning ranked `ScoredChunk`s into an answer drawn **only from context**. Three grounding
  levers: a **system instruction** (answer only from context / say "I don't know" / cite chunk ids),
  **temperature 0**, and **structured JSON output** (`response_mime_type="application/json"` +
  `response_schema=GeneratedAnswer`, a Pydantic model reused as both the schema and the parse
  target). **Chunk-level citations** (`citations: list[int]` of `chunk.id`; char-span deferred).
  Empty-context → local refusal, **no billable call**. Model `gemini-3.1-flash-lite` (env
  `GENERATION_MODEL`). The embed + generation clients were unified into a shared
  `app/gemini_client.py` (one process-cached client, honoring the worker's one-loop httpx
  discipline); `embed.py` imports it under its old `_get_client` name → its immutable tests stayed
  green. ADR `docs/adr/0002-generation.md`; teaching note `docs/learning/Q8-generation.md`. **No
  new pip dep** (reuses `google-genai`).
- **Refusal eval — the 10 traps (T1–T10), live-run once.** `eval/refusal.py` runs each trap through
  retrieve → generate; `eval/build_traps.py` emits `eval/traps.jsonl`. Latest live run
  (`eval/results/refusal-20260721T092240_787936Z.json`): **model hallucinated 0/10** — but the
  keyword `is_refusal` metric only scored 8/10, because a **correct grounded negation** ("Thornfield
  has no football program", cited) carries no refusal phrase, and one genuine refusal was phrased
  off the keyword list. **Architect decision (relabel now, judge in Q9):** the metric is honestly
  reported as an `explicit_refusal_rate` **lower bound** with a `non_refusals` **review queue** — it
  is NOT a hallucination count. `is_refusal` and its immutable test are left exactly as a coarse
  explicit-refusal tripwire; robust fabricated-vs-grounded judgment is Q9's LLM-as-judge job. (See
  the `eval-metrics-honest-labeling` memory.)
- **Phase 2 Q9 — RAGAS answer-eval, the LLM-as-judge (DONE, committed `a1228f6`).**
  `ragas==0.4.3`, judge `gemini-3.1-flash-lite`, built as **two separate commands** because the free
  tier is 15 RPM / **500 generation requests per day** — a run must never be wasted:
  **phase A** (`eval/produce.py`) runs the real `retrieve()`+`generate()` once and caches each answer
  **with the exact contexts it saw** → `eval/results/answers-<stamp>.jsonl` (the judge's input *and*
  the audit trail); **phase B** (`eval/ragas_eval.py`) judges that file, so re-scoring costs nothing
  in generation. `eval/throttle.py` = a pure `AsyncRateLimiter` (**sliding window** — matches the
  literal "15 per minute" quota; injectable clock/sleep so its spec runs offline), plus a pre-flight
  `estimate_requests` guard that aborts **before** spending. Three metrics, one per edge of the RAG
  triad: **faithfulness** (contexts↔answer → generation), **answer relevancy** (question↔answer →
  prompt), **context recall** (→ retrieval; needs the reference). **Context precision deliberately
  excluded** — an LLM's guess at the ranking property Q4 already measures with human-approved
  chunk-ids, at ~25% of the budget. **Traps get faithfulness only**: answer relevancy multiplies by
  `int(not all_noncommittal)`, so a *correct* refusal scores exactly 0. Teaching note
  `docs/learning/Q9-ragas.md`.
- **Generation baseline — RAGAS, rerank ON, strictness 3** (500 requests, **zero metric failures**;
  `eval/results/ragas-20260721T131433_136492Z.json` over `answers-20260721T123921_657092Z.jsonl`):

  > **golden (48): faithfulness 0.971 · answer relevancy 0.930 · context recall 0.958**
  > **trap (10): faithfulness 0.597**
  >
  > **Both headline numbers mislead, in opposite directions** — every sub-1.0 row was hand-checked.
  > The 2 zero-recall rows have **correct answers AND correct retrieval**: the judge is right that a
  > headerless table chunk (a bare numeric grid) never names its company, so this is **chunk context
  > loss**, not a retrieval failure — direct evidence for the deferred **M-enrich** stage. And trap
  > 0.597 is **not a 40% hallucination rate**: all 10 trap answers were correct, **zero fabrications**.
  > Faithfulness scores answer *shape* — a bare "I don't know." earns **0.0** for having no claims to
  > support; refusal + grounded explanation 0.5–0.8; grounded negation 1.0. Notably **T5 and T8, the
  > two the Q8 keyword metric wrongly *failed*, both score 1.0 here** — complementary blind spots, so
  > `eval/refusal.py` stays *beside* the judge rather than being replaced by it.
  >
  > **As a regression ruler:** golden faithfulness and recall sit near ceiling (3/48 and 2/48 below
  > 1.0) → they'll only catch large regressions; **answer relevancy (0.930) is the sensitive one**.
  > Scores are **self-graded** (judge == generator model → biased upward) and non-deterministic even
  > at temperature 0: over N=48 a delta under **~0.05** is noise, not signal.

- Offline test suite: **132 pass + 5 deselected (live)**, 0 skip; ruff clean, black clean on `app/`.

## Immediate next step

**Q10 — `POST /ask` (request-shaped) + the per-query structured log.** Closes Phase 2: wire the
finished read path (`retrieve` → rerank → `generate`) behind an HTTP endpoint, and log every query
per the CLAUDE.md contract — user message, retrieved chunk ids, reranked order, final context, the
answer, token/cost usage. Unlike ingestion this one **is** request-shaped (answer in the response,
not 202-and-poll). Ship test-first + `docs/learning/Q10-*.md`. Full breakdown in
`docs/PHASE2-PLAN.md`.

**Two architect decisions parked from Q9** (neither blocks Q10):
1. **Trap faithfulness.** Fixing the 0.597 artifact means either a refusal-specific metric or
   requiring the prompt to *always* explain its refusals. The latter is a **generator prompt
   change** — your call, deliberately not slipped in under an eval task (eval measures; it does
   not fix).
2. **`tests/test_parse.py`'s two commented-out assertions** (`# assert "Falcon-9X" in full_text`,
   2026-07-06) — a silently weakened *immutable* test. Restoring them is a spec decision.

## Open blockers / cautions

- **⚠ DO NOT re-ingest and DO NOT wipe the dev DB.** `eval/golden.jsonl` is keyed to the exact
  chunk-ids in the `infra_pgdata` volume; re-ingest churns `BIGINT IDENTITY` ids and invalidates
  the approved golden set (would force a `build_golden.py` re-run + re-approval). Re-running
  `eval.run` is safe and repeatable. Use `docker compose ... up -d` — **never** `-v`.
- **Reaper unverified live** — the M8 smoke proved the happy path, not the reaper requeue. The
  fence logic is covered by `test_worker.py`, so this is an optional live-confidence check
  (shorten `ingest_stuck_after_seconds`, leave a stale `processing` row, watch Beat requeue +
  `attempt` bump).
- **⚠ Dependency ceiling: `langchain-community<0.4` is load-bearing.** `ragas==0.4.3` imports a
  module langchain-community 0.4.x **deleted** — without the pin, `import ragas` fails outright and
  every Q9 test collapses at collection. Don't "helpfully" unpin it during a dependency refresh.
- **Lint debt (optional cleanup):** `tests/test_parse.py` isn't `black`-clean (two commented-out
  lines). It's an **immutable** test file — left untouched on purpose; worth a separate
  formatting-only commit if ever. Those two commented-out assertions are also a **silently weakened
  spec** — see the parked decision above.
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
- **Refusal eval (Q8, live)** — needs Postgres + `GEMINI_API_KEY`:
  `backend\.venv\Scripts\python.exe -m eval.refusal` runs the 10 traps through retrieve → generate
  and writes a timestamped `eval/results/refusal-*.json`. The headline number is an
  `explicit_refusal_rate` **lower bound** (keyword-based); eyeball the `non_refusals` review queue
  for any actually-fabricated answer. Regenerate the trap set (if `golden_questions.md` changes)
  with `python -m eval.build_traps`.
- **RAGAS answer-eval (Q9, live — two phases, budget-bound).** Needs Postgres + `GEMINI_API_KEY`.
  **Phase A** (58 generation calls + embeds) caches the answers; **phase B** (~500 judge requests,
  ~34 min at 15 RPM) scores them:
  ```powershell
  $env:RERANK_ENABLED="true"    # baseline was recorded rerank-ON; match it or the numbers don't compare
  backend\.venv\Scripts\python.exe -m eval.produce --out eval/results/
  backend\.venv\Scripts\python.exe -m eval.ragas_eval `
      --answers eval/results/answers-<stamp>.jsonl --strictness 3 --max-requests 520
  ```
  **Phase B alone is ~500 of the 500/day generation quota** — one full run per day, so smoke the
  wiring with `--limit 3` first. `--max-requests` **must** be raised past the 450 default at
  strictness 3 or the pre-flight guard aborts. Re-scoring an existing answers file is free of
  generation cost — never re-run phase A just to re-judge. Embedding has its **own separate**
  1000/day quota, so it isn't the binding constraint.
- **Inspect the dev DB:** `docker exec -it prodrag-postgres psql -U prodrag -d prodrag`, or VS Code
  SQLTools; ready queries in `infra/db/explore.sql`.
- **Curl on Windows:** use `curl.exe`, not bare `curl` (aliased to `Invoke-WebRequest`).

## Working rules (full text: CLAUDE.md)

- **Test-first & immutable:** write the failing test first; once written a test is immutable —
  fix the code, never the test. If the spec is wrong, **stop and ask** (architect's call).
- **RAG core = slow down and teach** (name rejected alternatives, define terms). Glue = move fast.
- **Architecture/design decisions are the human's** — don't silently pick one; ask.
