"""Q8 refusal eval: do the 10 trap questions get an EXPLICIT refusal (a lower bound)?

`python -m eval.refusal --traps eval/traps.jsonl --out eval/results/`

The trap questions (T1–T10, built from `golden_questions.md` by `build_traps.py`) have no
fabricable answer in the corpus. For each trap this runs the real pipeline — `retrieve()`
then `generate()` — and checks whether the answer contains an explicit "I don't know / not
in the context" phrase (`is_refusal`).

IMPORTANT — what this does and does NOT measure. `is_refusal` is a coarse, keyword-based
EXPLICIT-REFUSAL detector, so `explicit_refusal_rate` is only a **lower bound** on
hallucination resistance, not a measure of it. A non-refusal is NOT necessarily a
hallucination, in two ways this eval's own live run exposed:
  - A correct GROUNDED NEGATION carries no refusal phrase. T5 ("Thornfield's football
    coach?") answered "Thornfield does not have a football program" and cited the chunk that
    says so — the right, non-hallucinated answer, yet it matches no refusal keyword.
  - A genuine refusal can be phrased outside the keyword list (T8 said "does not state the
    verdict"; the list has "not stated", not "does not state").
So `non_refusals` is a **manual/LLM-review queue**, not a hallucination count — the report
surfaces those answers to inspect, and never labels them "hallucinations".

The robust fabricated-vs-grounded judgment (did the model invent a fact not in the corpus?)
belongs to Q9's LLM-as-judge / RAGAS faithfulness, not to string matching. This cheap check
exists to catch obvious regressions and to queue answers for review.

This is a LIVE eval (needs the dev DB + GEMINI_API_KEY) run by hand — the counterpart to Q4's
retrieval `run.py`, for the generation stage. The pieces with logic (`is_refusal`,
`summarize`, `format_table`) are pure and unit-tested offline (`backend/tests/`); the rest is
I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- import bootstrap (must run before importing `app`) ------------------------------
# Mirrors eval/run.py: the eval package is at the repo root; the app lives under backend/.
# Put backend on the path so `import app.*` resolves under `python -m eval.refusal`, and load
# backend/.env so config picks up GEMINI_API_KEY regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
try:  # best-effort; the offline helper test never needs the key
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
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.generate.generate import GeneratedAnswer, generate  # noqa: E402
from app.models import DEV_OWNER_ID  # noqa: E402
from app.retrieve.retrieve import retrieve  # noqa: E402

# Phrases that mark an explicit decline. Refusal is judged on the answer TEXT, not on empty
# citations: a confident-but-uncited answer is a grounding failure, not a good refusal (see
# the immutable spec in test_refusal.py). Matched case-insensitively as substrings.
_REFUSAL_PHRASES: tuple[str, ...] = (
    "don't know",
    "do not know",
    "not know",
    "not mentioned",
    "not mention",
    "does not contain",
    "doesn't contain",
    "not contain",
    "not specified",
    "not stated",
    "not provided",
    "no information",
    "not available",
    "not found",
    "cannot determine",
    "can't determine",
    "cannot answer",
    "unable to answer",
    "isn't in the context",
    "not in the context",
)


def is_refusal(answer: GeneratedAnswer) -> bool:
    """True when `answer` contains an EXPLICIT refusal phrase (a coarse, keyword proxy).

    Keyed to the answer text, not the citation list: an "I don't know / not in the context"
    phrase counts even if the model cited the chunk it checked, while a confident answer with
    no citations does NOT (it's ungrounded). See test_refusal.py for the spec.

    Deliberately a LOWER BOUND, not hallucination detection (see module docstring): it misses
    correct grounded negations and refusals phrased outside the list. The real
    fabricated-vs-grounded judgment is Q9's LLM-as-judge. Logic left unchanged on purpose.
    """
    text = answer.answer.lower()
    return any(phrase in text for phrase in _REFUSAL_PHRASES)


@dataclass(frozen=True, slots=True)
class Trap:
    """One trap question: its id (e.g. 'T1') and text. No relevant chunk ids by design."""

    id: str
    question: str


def load_traps(path: str | Path) -> list[Trap]:
    """Parse a traps.jsonl file (one JSON object per line) into `Trap`s."""
    traps: list[Trap] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        traps.append(Trap(id=obj["id"], question=obj["question"]))
    return traps


def summarize(per_trap: list[dict]) -> dict:
    """Aggregate per-trap results into an HONESTLY-named summary.

    The counts are framed as explicit-refusal detection, never as hallucination detection: a
    `non_refusals` entry is a review queue, not a verdict (see module docstring). There is
    deliberately no "hallucinations" field — asserting that would over-claim what a keyword
    check can know.
    """
    n = len(per_trap)
    refusals = sum(1 for t in per_trap if t["refused"])
    return {
        "n_traps": n,
        "explicit_refusals": refusals,
        "non_refusals": n - refusals,
        "explicit_refusal_rate": (refusals / n) if n else 0.0,
    }


async def evaluate_traps(
    traps: Sequence[Trap], *, owner_id: uuid.UUID = DEV_OWNER_ID
) -> dict:
    """Run each trap through retrieve -> generate and record whether the model refused."""
    per_trap: list[dict] = []
    for trap in traps:
        result = await retrieve(trap.question, owner_id=owner_id)
        answer = await generate(trap.question, result.chunks)
        refused = is_refusal(answer)
        per_trap.append(
            {
                "id": trap.id,
                "question": trap.question,
                "refused": refused,
                "answer": answer.answer,
                "citations": answer.citations,
                "retrieved_chunk_ids": [sc.chunk.id for sc in result.chunks],
            }
        )

    return {"summary": summarize(per_trap), "per_trap": per_trap}


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
    path = out / f"refusal-{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def format_table(report: dict) -> str:
    """A compact human summary for stdout (full detail is in the JSON file)."""
    s = report["summary"]
    lines = [
        f"Trap eval — {s['n_traps']} trap questions",
        f"  explicit refusals    : {s['explicit_refusals']}/{s['n_traps']}",
        f"  explicit_refusal_rate: {s['explicit_refusal_rate']:.3f}  "
        "(LOWER BOUND — keyword-based; not hallucination resistance)",
    ]
    non_refusals = [t for t in report["per_trap"] if not t["refused"]]
    if non_refusals:
        # NOT necessarily hallucinations: a correct grounded negation ("no football
        # program") also lands here. Surface them to INSPECT, don't verdict them.
        lines.append("  not an explicit refusal — INSPECT (may be a correct grounded")
        lines.append("  negation, not a hallucination):")
        for t in non_refusals:
            lines.append(f"    {t['id']}: {t['answer']}  (cited {t['citations']})")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the trap-question refusal eval.")
    parser.add_argument(
        "--traps", default="eval/traps.jsonl", help="path to traps.jsonl"
    )
    parser.add_argument(
        "--out", default="eval/results/", help="directory to write the results JSON"
    )
    args = parser.parse_args(argv)

    traps = load_traps(args.traps)
    report = asyncio.run(evaluate_traps(traps))
    report["metadata"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "generation_model": get_settings().generation_model,
        "traps_path": str(args.traps),
    }

    path = write_report(report, args.out)
    print(format_table(report))
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
