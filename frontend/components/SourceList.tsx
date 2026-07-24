"use client";

import type { Source } from "@/lib/types";

// The evidence panel under an assistant answer. It renders every passage the model was
// SHOWN (rerank order, best-first) and marks the ones the model said it actually USED.
//
// A note on the citation model, because it drives this whole component's shape:
// `generate.py` returns prose in `answer` and a SEPARATE `citations: number[]` of the
// chunk ids it relied on — there are NO inline `[n]` markers in the answer text to hang
// badges off of, and the raw ids (e.g. 147) are meaningless to a reader. So instead of
// faking inline anchors, we give each shown source a stable 1-based DISPLAY number and
// surface the cited/uncited split here: cited passages are the model's evidence; the
// rest are context it saw but didn't lean on (useful for judging grounding).

type Props = {
  sources: Source[];
  citations: number[];
};

export default function SourceList({ sources, citations }: Props) {
  if (sources.length === 0) return null;

  const cited = new Set(citations);
  const citedCount = sources.filter((s) => cited.has(s.id)).length;

  return (
    <section className="mt-4 border-t border-ink-800 pt-3">
      <h3 className="mb-2 flex items-center gap-2 font-mono text-[0.65rem] uppercase tracking-[0.2em] text-fog-500">
        Sources
        <span className="text-fog-500/70">
          · {sources.length} shown · {citedCount} cited
        </span>
      </h3>

      <ol className="space-y-1.5">
        {sources.map((source, i) => {
          const isCited = cited.has(source.id);
          const num = i + 1;
          return (
            <li key={source.id}>
              <details className="group rounded-md border border-ink-800 bg-ink-900/60 open:bg-ink-850">
                <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-2 text-sm">
                  {/* Display-number badge: filled amber when cited, hollow when not. */}
                  <span
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded font-mono text-[0.7rem] ${
                      isCited
                        ? "bg-amber-400 text-ink-950"
                        : "border border-ink-600 text-fog-500"
                    }`}
                  >
                    {num}
                  </span>

                  {isCited ? (
                    <span className="shrink-0 font-mono text-[0.6rem] uppercase tracking-[0.15em] text-amber-400">
                      cited
                    </span>
                  ) : (
                    <span className="shrink-0 font-mono text-[0.6rem] uppercase tracking-[0.15em] text-fog-500">
                      seen
                    </span>
                  )}

                  {/* One-line preview; the caret rotates when the row is open. */}
                  <span className="min-w-0 flex-1 truncate text-fog-400">
                    {source.content}
                  </span>

                  <span
                    className="shrink-0 font-mono text-[0.65rem] text-fog-500"
                    title="fused / rerank score"
                  >
                    {source.score.toFixed(3)}
                  </span>
                  <svg
                    className="h-3.5 w-3.5 shrink-0 text-fog-500 transition-transform group-open:rotate-90"
                    viewBox="0 0 12 12"
                    fill="none"
                    aria-hidden
                  >
                    <path
                      d="M4.5 2.5 8 6l-3.5 3.5"
                      stroke="currentColor"
                      strokeWidth="1.4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </summary>

                <div className="border-t border-ink-800 px-3 py-2.5">
                  <div className="mb-1.5 font-mono text-[0.6rem] uppercase tracking-[0.15em] text-fog-500">
                    chunk {source.id}
                  </div>
                  <p className="whitespace-pre-wrap text-sm leading-relaxed text-fog-300">
                    {source.content}
                  </p>
                </div>
              </details>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
