"""Q9 phase A — run the real RAG pipeline over the eval questions and CACHE the answers.

`python -m eval.produce --golden eval/golden.jsonl --traps eval/traps.jsonl`

Q9 scores the *generation* stage with an LLM judge (RAGAS). That judge is itself a Gemini
model, so a full run costs ~240 calls against a free-tier quota of 15/minute and 500/day.
Splitting the eval in two — produce here, judge in `eval/ragas_eval.py` — means the
expensive generation pass is paid ONCE and then re-scored for free: adding a metric,
recovering from a 429, or re-running the judge after a prompt bug all read this file
instead of regenerating answers.

The file is also the audit trail. It records the exact context passages the model saw, so
any faithfulness score can be traced back to the passages it was computed against without
re-running (and without hoping retrieval is still deterministic months later).

Row shape (one JSON object per line):
    {"kind": "golden"|"trap", "id": 1, "question": ..., "reference": ...,
     "retrieved_chunk_ids": [...], "contexts": [...], "answer": ..., "citations": [...]}

`kind` is recorded rather than inferred: traps are judged differently in phase B (they get
faithfulness only — a *correct* refusal scores 0 on answer relevancy, which would silently
punish the model for doing the right thing), and phase B shouldn't have to guess that from
an empty `reference`.

Module-level seams: `retrieve` and `generate` are imported as module globals so tests
monkeypatch THIS module's copies and never touch Postgres or Gemini — the same pattern
`eval/run.py` and `app/retrieve/retrieve.py` use.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- import bootstrap (must run before importing `app`) ------------------------------
# Mirrors eval/run.py: the eval package is at the repo root; the app lives under backend/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
try:  # best-effort; offline tests monkeypatch the pipeline and never need the key
    from dotenv import load_dotenv

    load_dotenv(_BACKEND / ".env")
except Exception:  # pragma: no cover - dotenv always present via pydantic-settings
    pass

import argparse  # noqa: E402  (after the path bootstrap, by necessity)
import asyncio  # noqa: E402
import json  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from typing import Any  # noqa: E402

from app.generate.generate import generate  # noqa: E402
from app.models import DEV_OWNER_ID  # noqa: E402
from app.retrieve.retrieve import retrieve  # noqa: E402

from eval.refusal import load_traps  # noqa: E402
from eval.run import load_golden  # noqa: E402
from eval.throttle import AsyncRateLimiter  # noqa: E402


async def produce_answers(
    items: Sequence[Any],
    *,
    kind: str = "golden",
    owner_id: uuid.UUID = DEV_OWNER_ID,
    limit: int | None = None,
    limiter: AsyncRateLimiter | None = None,
) -> list[dict]:
    """Run every item through `retrieve()` then `generate()`; return the answer rows.

    `items` is anything with `.id` and `.question` — `GoldenItem` (which also carries
    `.expected_answer`, the reference used by context recall) or `Trap` (which has no
    reference by design). `limit` truncates the run: the cheap smoke path that validates
    wiring on 3 questions before spending a day's quota on 48.

    `limiter` paces the API calls. Phase A is smaller than the judge but not free: each
    question costs one query embedding plus one generation, and a serial loop at ~2-3s per
    question would issue ~20-30 generation requests a minute against a 15/min limit.
    Optional and off by default so the offline suite (which fakes both calls) never sleeps.
    """
    selected = list(items)[:limit] if limit is not None else list(items)
    rows: list[dict] = []

    for item in selected:
        # One slot per API call, claimed before the call it pays for: retrieve() embeds
        # the query, generate() calls the LLM. Two pools, but pacing both off one limiter
        # errs toward under-spending, which is the safe direction.
        if limiter is not None:
            await limiter.acquire()
        result = await retrieve(item.question, owner_id=owner_id)
        if limiter is not None:
            await limiter.acquire()
        answer = await generate(item.question, result.chunks)
        rows.append(
            {
                "kind": kind,
                "id": item.id,
                "question": item.question,
                # Traps have no reference answer; getattr keeps one code path for both.
                "reference": getattr(item, "expected_answer", ""),
                "retrieved_chunk_ids": [sc.chunk.id for sc in result.chunks],
                "contexts": [sc.chunk.content for sc in result.chunks],
                "answer": answer.answer,
                "citations": answer.citations,
            }
        )

    return rows


def write_answers(rows: Sequence[dict], out_dir: str | Path) -> Path:
    """Write the rows as a timestamped JSONL file under `out_dir`; return its path.

    JSONL (not one big JSON array) so the file can be appended to as a long run
    progresses and a partial file after a crash is still parseable — the same reason
    `golden.jsonl` is line-oriented.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    path = out / f"answers-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def load_answers(path: str | Path) -> list[dict]:
    """Read an answers JSONL back into rows (blank lines skipped)."""
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


async def _run(args: argparse.Namespace) -> tuple[list[dict], AsyncRateLimiter]:
    """Produce golden rows then trap rows in ONE event loop (one client, one pool).

    Golden and trap sets share ONE limiter, so the quota is enforced across the whole run
    rather than reset halfway through it.
    """
    limiter = AsyncRateLimiter(args.rpm)
    rows: list[dict] = []
    if args.golden:
        golden = load_golden(args.golden)
        rows += await produce_answers(
            golden, kind="golden", limit=args.limit, limiter=limiter
        )
    if args.traps:
        traps = load_traps(args.traps)
        rows += await produce_answers(
            traps, kind="trap", limit=args.limit, limiter=limiter
        )
    return rows, limiter


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Q9 phase A: generate answers for the eval questions and cache them."
    )
    parser.add_argument(
        "--golden",
        default="eval/golden.jsonl",
        help="path to golden.jsonl ('' to skip)",
    )
    parser.add_argument(
        "--traps", default="eval/traps.jsonl", help="path to traps.jsonl ('' to skip)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only the first N of EACH set"
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=15,
        help="requests per minute (free-tier generation limit: 15; 0 disables pacing)",
    )
    parser.add_argument(
        "--out", default="eval/results/", help="directory to write the answers JSONL"
    )
    args = parser.parse_args(argv)

    rows, limiter = asyncio.run(_run(args))
    path = write_answers(rows, args.out)

    n_golden = sum(1 for r in rows if r["kind"] == "golden")
    n_traps = len(rows) - n_golden
    print(f"Produced {len(rows)} answers ({n_golden} golden, {n_traps} trap)")
    print(f"Spent {limiter.requests} API requests at {args.rpm} rpm")
    print(f"Wrote {path}")
    print("Next: python -m eval.ragas_eval --answers " + str(path))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
