"""Q9 phase B — score cached answers with the RAGAS LLM-as-judge.

`python -m eval.ragas_eval --answers eval/results/answers-<stamp>.jsonl`

The first eval that measures the GENERATION stage. Q4-Q7 scored retrieval (hit@k / MRR
against hand-labeled chunk ids); Q8 added only `eval/refusal.py`'s keyword check, which we
deliberately relabelled a LOWER BOUND because string matching cannot distinguish a correct
grounded negation ("Thornfield has no football program") from a fabrication.

RAGAS replaces the string match with claim-level entailment: an LLM decomposes the answer
into atomic statements and returns a binary "is this supported by the context?" verdict per
statement; the score is supported/total. That is a real fabricated-vs-grounded judgment.

Three metrics, each blaming a DIFFERENT stage — which is the point of using more than one:

    faithfulness      answer vs context   -> blames GENERATION (grounding)
    answer_relevancy  answer vs question  -> blames the PROMPT / answer completeness
    context_recall    context vs reference-> blames RETRIEVAL (embedding/chunking/k)

Context *precision* is deliberately absent: it is an LLM's guess at the ranking property we
already measure directly with human-approved chunk-id labels (MRR/hit@k), and it would cost
about a quarter of the call budget to duplicate a better number.

What the numbers are NOT: ground truth. The judge is an LLM (by default the SAME model that
wrote the answers, so it is self-grading and biased upward), it is non-deterministic even at
temperature 0, and with N=48 a difference under ~0.05 is noise. `format_table` prints all
three caveats rather than burying them — the same honest-labeling rule that forced the
refusal metric's rename.

Scoring rules that keep the means honest:
  - traps get faithfulness ONLY (relevancy scores a correct refusal 0; recall needs a
    reference a trap doesn't have),
  - a missing reference skips context recall rather than scoring it 0,
  - a metric that raises yields None and is excluded from the mean — a failed judge call
    is missing data, not a bad answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- import bootstrap (must run before importing `app`) ------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
try:  # best-effort; offline tests inject fake metrics and never need the key
    from dotenv import load_dotenv

    load_dotenv(_BACKEND / ".env")
except Exception:  # pragma: no cover - dotenv always present via pydantic-settings
    pass

import argparse  # noqa: E402  (after the path bootstrap, by necessity)
import asyncio  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from typing import Any  # noqa: E402

from eval.produce import load_answers  # noqa: E402

ALL_METRICS = ("faithfulness", "answer_relevancy", "context_recall")

# Metrics a trap row may be scored on. A trap has no answer in the corpus, so the correct
# response is a refusal: answer relevancy explicitly scores a noncommittal answer 0 (it
# would punish the model for being right), and context recall has no reference to check.
TRAP_METRICS = ("faithfulness",)

# Gemini requests each metric issues per row, measured from the ragas source rather than
# guessed — the budget guard is only as good as these numbers.
#   faithfulness    : 1 statement-generation call + 1 NLI-verdict call
#   context_recall  : 1 classification call
#   answer_relevancy: `strictness` question-generation calls, + 1 embedding of the user's
#                     question, + `strictness` embeddings of the generated questions.
#                     That last term is `strictness`, not 1, because ThrottledEmbeddings
#                     fans a batch out into one request per text to dodge the
#                     gemini-embedding-2 batch-fusion bug (see eval/judge.py). So
#                     relevancy costs 2*strictness + 1, and each extra round costs TWO
#                     requests, not one.
_COST_FAITHFULNESS = 2
_COST_CONTEXT_RECALL = 1
_COST_RELEVANCY_QUESTION_EMBEDDING = 1


def metrics_for_row(row: dict, available: Sequence[str] = ALL_METRICS) -> list[str]:
    """Which metrics may legitimately be computed for this row.

    Routing lives here, once, so the scoring loop, the budget estimate and the tests all
    agree on what a run will actually do.
    """
    allowed = TRAP_METRICS if row.get("kind") == "trap" else ALL_METRICS
    names = [m for m in available if m in allowed]
    # Nothing to recall without a reference answer — skip it rather than score a 0.
    if not row.get("reference"):
        names = [m for m in names if m != "context_recall"]
    return names


def estimate_requests(
    rows: Sequence[dict],
    *,
    strictness: int = 1,
    available: Sequence[str] = ALL_METRICS,
) -> int:
    """Total API requests a run over `rows` would issue, computed BEFORE spending any.

    The guard exists because the quota is 500 requests/day: an over-budget run must fail
    while it is still free, not die at 90% having burned the day.
    """
    total = 0
    for row in rows:
        for name in metrics_for_row(row, available):
            if name == "faithfulness":
                total += _COST_FAITHFULNESS
            elif name == "context_recall":
                total += _COST_CONTEXT_RECALL
            elif name == "answer_relevancy":
                # strictness generations + strictness question embeddings + 1
                total += 2 * strictness + _COST_RELEVANCY_QUESTION_EMBEDDING
    return total


# Each metric takes a different keyword surface; this maps a row onto the right one.
# Keeping the three signatures in one table (rather than branching inside the loop) makes
# it obvious what evidence each metric is actually given.
_ARGS = {
    "faithfulness": lambda row: {
        "user_input": row["question"],
        "response": row["answer"],
        "retrieved_contexts": row["contexts"],
    },
    "answer_relevancy": lambda row: {
        "user_input": row["question"],
        "response": row["answer"],
    },
    "context_recall": lambda row: {
        "user_input": row["question"],
        "retrieved_contexts": row["contexts"],
        "reference": row["reference"],
    },
}


async def score_rows(
    rows: Sequence[dict],
    metrics: dict[str, Any],
    *,
    limit: int | None = None,
    checkpoint_path: str | Path | None = None,
) -> list[dict]:
    """Score each row with every metric that applies to it; return the enriched rows.

    Runs strictly SERIALLY. That is not an oversight: the limiter paces to 15 requests per
    minute, so concurrency would buy nothing but a thundering herd against the same quota,
    and a serial loop makes the checkpoint a truthful record of progress.

    `checkpoint_path` gets one JSON line per completed row as it completes, so a 429 or a
    Ctrl-C 40 minutes in still leaves everything scored so far on disk.
    """
    selected = list(rows)[:limit] if limit is not None else list(rows)
    scored: list[dict] = []
    ckpt = Path(checkpoint_path) if checkpoint_path is not None else None
    if ckpt is not None:
        ckpt.parent.mkdir(parents=True, exist_ok=True)

    for row in selected:
        out = dict(row)
        scores: dict[str, float | None] = {}
        errors: dict[str, str] = {}

        for name in metrics_for_row(row, list(metrics)):
            try:
                result = await metrics[name].ascore(**_ARGS[name](row))
                scores[name] = float(result.value)
            except Exception as exc:
                # None, never 0: a judge failure is missing data. Recording it as a zero
                # would silently understate the pipeline being measured.
                scores[name] = None
                errors[name] = f"{type(exc).__name__}: {exc}"

        out["scores"] = scores
        if errors:
            out["errors"] = errors
        scored.append(out)

        if ckpt is not None:
            with ckpt.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(out) + "\n")

    return scored


def aggregate(scored: Sequence[dict]) -> dict:
    """Mean per metric, split by row kind, with failures counted separately.

    Golden and trap faithfulness answer different questions ("is the answer grounded?" vs
    "did it refuse to invent one?"), so pooling them into one mean would blur two results
    into a number that means neither.
    """
    summary: dict[str, dict] = {}
    for row in scored:
        kind = row.get("kind", "golden")
        bucket = summary.setdefault(kind, {"n_rows": 0})
        bucket["n_rows"] += 1
        for name, value in row.get("scores", {}).items():
            stats = bucket.setdefault(
                name, {"total": 0.0, "n_scored": 0, "n_failed": 0}
            )
            if value is None:
                stats["n_failed"] += 1
            else:
                stats["total"] += value
                stats["n_scored"] += 1

    for bucket in summary.values():
        for name, stats in bucket.items():
            if name == "n_rows":
                continue
            n = stats["n_scored"]
            stats["mean"] = (stats["total"] / n) if n else None
            del stats["total"]
    return summary


def _git_sha() -> str | None:
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


def write_report(report: dict, out_dir: str | Path) -> Path:
    """Write the report as a timestamped JSON under `out_dir`; return its path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    path = out / f"ragas-{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def format_table(report: dict) -> str:
    """A compact human summary — including the caveats, not despite them.

    An LLM judge's score reads like a measurement and isn't one. Printing "faithfulness
    0.94" alone invites treating it as fact, so the caveats sit next to the number where
    they cannot be missed (the same rule that renamed the refusal rate a lower bound).
    """
    summary = report.get("summary", {})
    meta = report.get("metadata", {})
    lines = ["RAGAS eval (LLM-as-judge)"]

    for kind in sorted(summary):
        bucket = summary[kind]
        lines.append(f"  {kind} — {bucket.get('n_rows', 0)} rows")
        for name in ALL_METRICS:
            stats = bucket.get(name)
            if not stats:
                continue
            mean = stats["mean"]
            shown = "n/a" if mean is None else f"{mean:.3f}"
            line = f"    {name:<17}: {shown}  (n={stats['n_scored']}"
            if stats["n_failed"]:
                line += f", {stats['n_failed']} failed"
            lines.append(line + ")")

    lines += [
        "",
        "  How to read these numbers:",
        f"    - judge model: {meta.get('judge_model', 'unknown')}"
        + (
            "  [SELF-GRADED: same model wrote the answers]"
            if meta.get("self_graded")
            else ""
        ),
        "    - an LLM judge's claim-level ratio is NOT ground truth; it is one model's",
        "      opinion about entailment, and it is biased upward when self-graded.",
        "    - scores are non-deterministic even at temperature 0; over ~48 questions a",
        "      difference under ~0.05 between runs is noise, not a regression.",
    ]
    return "\n".join(lines)


def _build_metrics(judge: Any, names: Sequence[str], strictness: int) -> dict[str, Any]:
    """Construct the requested ragas metrics against the throttled judge.

    Imported lazily so the module (and its offline tests) never pay for ragas' heavy
    import chain unless a real judged run happens.
    """
    from ragas.metrics.collections import AnswerRelevancy, ContextRecall, Faithfulness

    built: dict[str, Any] = {}
    if "faithfulness" in names:
        built["faithfulness"] = Faithfulness(llm=judge.llm)
    if "answer_relevancy" in names:
        built["answer_relevancy"] = AnswerRelevancy(
            llm=judge.llm, embeddings=judge.embeddings, strictness=strictness
        )
    if "context_recall" in names:
        built["context_recall"] = ContextRecall(llm=judge.llm)
    return built


async def _run(args: argparse.Namespace, rows: list[dict]) -> dict:
    from eval.judge import build_judge

    judge = build_judge(rpm=args.rpm)
    metrics = _build_metrics(judge, args.metrics, args.strictness)

    ckpt = Path(args.out) / "ragas-checkpoint.jsonl"
    scored = await score_rows(rows, metrics, limit=args.limit, checkpoint_path=ckpt)

    import ragas

    return {
        "summary": aggregate(scored),
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": _git_sha(),
            "judge_model": judge.model,
            "judge_embedding_model": judge.embedding_model,
            "self_graded": judge.model == _generation_model(),
            "metrics": list(metrics),
            "strictness": args.strictness,
            "ragas_version": ragas.__version__,
            "answers_path": str(args.answers),
            "rpm": args.rpm,
            "requests_spent": judge.limiter.requests,
            "checkpoint_path": str(ckpt),
        },
        "per_row": scored,
    }


def _generation_model() -> str:
    from app.config import get_settings

    return get_settings().generation_model


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Q9 phase B: score cached answers with the RAGAS LLM judge."
    )
    parser.add_argument("--answers", required=True, help="path to an answers JSONL")
    parser.add_argument(
        "--limit", type=int, default=None, help="only score the first N rows"
    )
    parser.add_argument(
        "--rpm", type=int, default=15, help="requests per minute (free tier: 15)"
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=450,
        help="abort before starting if the run would exceed this (daily cap: 500)",
    )
    parser.add_argument(
        "--strictness",
        type=int,
        default=1,
        help="answer-relevancy rounds; ragas' default is 3 (less noise, 3x the cost)",
    )
    parser.add_argument(
        "--metrics",
        nargs="+",
        default=list(ALL_METRICS),
        choices=list(ALL_METRICS),
        help="which metrics to run",
    )
    parser.add_argument("--out", default="eval/results/", help="output directory")
    args = parser.parse_args(argv)

    rows = load_answers(args.answers)
    if args.limit is not None:
        rows = rows[: args.limit]

    # Pre-flight: refuse to start a run that cannot finish within the quota. Failing here
    # costs nothing; failing at row 40 costs the day.
    estimate = estimate_requests(
        rows, strictness=args.strictness, available=args.metrics
    )
    print(f"{len(rows)} rows x {args.metrics} ~= {estimate} requests", end="")
    print(f" (~{estimate / max(args.rpm, 1):.0f} min at {args.rpm} rpm)")
    if estimate > args.max_requests:
        print(
            f"ABORT: estimated {estimate} requests exceeds --max-requests "
            f"{args.max_requests}. Narrow --metrics, lower --strictness, or use --limit."
        )
        return 1

    report = asyncio.run(_run(args, rows))
    path = write_report(report, args.out)
    print(format_table(report))
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
