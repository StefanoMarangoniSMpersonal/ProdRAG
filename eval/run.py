"""Q4 retrieval-eval harness: run the golden questions through `retrieve()`, score them.

`python -m eval.run --golden eval/golden.jsonl --out eval/results/` — the entrypoint the
`eval-run` skill already expects. For each golden question it calls the REAL `retrieve()`
(Q3), turns the ranked chunks into ids, and hands those to the pure metrics
(`eval/metrics.py`) to get hit@k and reciprocal rank; it aggregates to hit-rate@k + MRR,
prints a table, and writes a timestamped results JSON under `--out` so later runs (Q5
lexical, Q6 fusion, Q7 rerank) have a baseline to diff against.

Naive-first, matching the pipeline: this measures the SEMANTIC-only baseline, because
that is all `retrieve()` does today. The harness never changes as later stages slot in
behind `retrieve()` — it only ever sees "a query in, ranked ids out".

Two seams make it drivable offline in tests:
  - `retrieve` is a module global (imported once here), so a test monkeypatches THIS
    module's copy with a fake that returns canned rankings — no Postgres, no Gemini. Same
    seam pattern as `app/retrieve/retrieve.py`.
  - the `sys.path` + `.env` bootstrap below lets `python -m eval.run` run from the repo
    root yet still import the app and load `backend/.env`'s `GEMINI_API_KEY` (the live
    embed needs it); unit runs monkeypatch `retrieve` and never touch the key.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- import bootstrap (must run before importing `app`) ------------------------------
# The eval package sits at the repo root; the app lives under backend/. Put backend on
# the path so `import app.*` resolves when this is run as `python -m eval.run` from the
# repo root, and load backend/.env so config picks up GEMINI_API_KEY regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
try:  # best-effort; offline/unit runs monkeypatch retrieve and never need the key
    from dotenv import load_dotenv

    load_dotenv(_BACKEND / ".env")
except Exception:  # pragma: no cover - dotenv always present via pydantic-settings
    pass

import argparse  # noqa: E402  (after the path bootstrap, by necessity)
import asyncio  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import DEV_OWNER_ID, Chunk, Document  # noqa: E402
from app.retrieve.retrieve import retrieve  # noqa: E402

from eval.metrics import hit_at_k, hit_rate_at_k, mrr, reciprocal_rank  # noqa: E402

DEFAULT_KS = (1, 3, 5, 10)


@dataclass(frozen=True, slots=True)
class GoldenItem:
    """One golden question: its text, the relevant chunk ids, and the reference answer.

    `relevant_chunk_ids` is the ground truth for retrieval metrics (built by
    `build_golden.py`). `expected_answer` is carried through untouched for RAGAS at Q9;
    Q4 does not read it.
    """

    id: int
    question: str
    relevant_chunk_ids: set[int]
    expected_answer: str = ""


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The scored result of one harness run: per-question rows + aggregate summary."""

    per_query: list[dict]
    summary: dict
    metadata: dict = field(default_factory=dict)


def load_golden(path: str | Path) -> list[GoldenItem]:
    """Parse a golden.jsonl file (one JSON object per line) into `GoldenItem`s.

    `relevant_chunk_ids` becomes a set (metrics do membership tests, order-free). Blank
    lines are skipped so the file can be hand-edited without breaking.
    """
    items: list[GoldenItem] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        items.append(
            GoldenItem(
                id=obj["id"],
                question=obj["question"],
                relevant_chunk_ids=set(obj.get("relevant_chunk_ids", [])),
                expected_answer=obj.get("expected_answer", ""),
            )
        )
    return items


async def evaluate_golden(
    golden: Sequence[GoldenItem],
    *,
    ks: Sequence[int] = DEFAULT_KS,
    owner_id: uuid.UUID = DEV_OWNER_ID,
) -> EvalReport:
    """Run every golden question through `retrieve()` and score it.

    Retrieves ONCE per question at the largest `k` and slices for the smaller cutoffs —
    hit@1/3/5 are all answered by the single top-`max(ks)` ranking, and reciprocal rank
    needs the full order anyway, so N calls per question would be waste.
    """
    ks = sorted(set(ks))
    max_k = max(ks)
    per_query: list[dict] = []

    for item in golden:
        result = await retrieve(item.question, k=max_k, owner_id=owner_id)
        ranked_ids = [sc.chunk.id for sc in result.chunks]
        rr = reciprocal_rank(ranked_ids, item.relevant_chunk_ids)
        hits = {k: hit_at_k(ranked_ids, item.relevant_chunk_ids, k) for k in ks}
        per_query.append(
            {
                "id": item.id,
                "question": item.question,
                "relevant_chunk_ids": sorted(item.relevant_chunk_ids),
                "retrieved_chunk_ids": ranked_ids,
                "reciprocal_rank": rr,
                "hits": {str(k): hits[k] for k in ks},
            }
        )

    summary: dict = {
        "n_questions": len(per_query),
        "mrr": mrr([q["reciprocal_rank"] for q in per_query]),
    }
    for k in ks:
        summary[f"hit_rate@{k}"] = hit_rate_at_k([q["hits"][str(k)] for q in per_query])

    metadata = {
        "ks": list(ks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "owner_id": str(owner_id),
    }
    return EvalReport(per_query=per_query, summary=summary, metadata=metadata)


def write_report(report: EvalReport, out_dir: str | Path) -> Path:
    """Write the report as a timestamped JSON file under `out_dir`; return its path.

    Microsecond timestamp so back-to-back runs never clobber each other — each result
    file is kept so future runs have a baseline to diff against (the `eval-run` skill
    relies on the directory accumulating).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    path = out / f"retrieval-{stamp}.json"
    payload = {
        "summary": report.summary,
        "metadata": report.metadata,
        "per_query": report.per_query,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def format_table(report: EvalReport) -> str:
    """A compact human summary for stdout (the full detail lives in the JSON file)."""
    s = report.summary
    lines = [
        f"Retrieval eval — {s['n_questions']} questions",
        f"  MRR       : {s['mrr']:.3f}",
    ]
    for k in report.metadata.get("ks", DEFAULT_KS):
        key = f"hit_rate@{k}"
        if key in s:
            lines.append(f"  hit@{k:<3}  : {s[key]:.3f}")
    return "\n".join(lines)


async def _corpus_counts(owner_id: uuid.UUID) -> tuple[int, int]:
    """(documents, chunks) visible to `owner_id` — recorded so a baseline says how big a
    haystack it was measured over (13 chunks is a smoke test, not a trustworthy N)."""
    async with SessionLocal() as session:
        n_docs = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.owner_id == owner_id)
        )
        n_chunks = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.owner_id == owner_id)
        )
    return int(n_docs or 0), int(n_chunks or 0)


def _git_sha() -> str | None:
    """Short git SHA of the working tree, or None if unavailable — stamps the baseline to
    the exact pipeline code it measured."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # pragma: no cover - git may be absent in odd envs
        return None


async def _run_live(
    golden: Sequence[GoldenItem], ks: Sequence[int], owner_id: uuid.UUID
) -> EvalReport:
    """Evaluate + enrich metadata with live corpus counts (one event loop for both)."""
    report = await evaluate_golden(golden, ks=ks, owner_id=owner_id)
    try:
        n_docs, n_chunks = await _corpus_counts(owner_id)
        report.metadata["corpus_docs"] = n_docs
        report.metadata["corpus_chunks"] = n_chunks
    except Exception as exc:  # pragma: no cover - counts are best-effort context
        report.metadata["corpus_counts_error"] = str(exc)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the retrieval eval golden set.")
    parser.add_argument(
        "--golden", default="eval/golden.jsonl", help="path to golden.jsonl"
    )
    parser.add_argument(
        "--out", default="eval/results/", help="directory to write the results JSON"
    )
    args = parser.parse_args(argv)

    golden = load_golden(args.golden)
    ks = list(DEFAULT_KS)
    report = asyncio.run(_run_live(golden, ks, DEV_OWNER_ID))
    report.metadata["git_sha"] = _git_sha()
    report.metadata["embedding_model"] = get_settings().embedding_model
    report.metadata["golden_path"] = str(args.golden)

    path = write_report(report, args.out)
    print(format_table(report))
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
