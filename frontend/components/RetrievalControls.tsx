"use client";

import type { AskControls } from "@/lib/types";

// The live read-path control panel that docks above the composer. It lets you flip the
// reranker and the semantic cache and set the relevance floor PER MESSAGE, then watch the
// same question answer differently — the whole point of the feature. It's a controlled
// component: the chat page owns the AskControls state (seeded from GET /ask/config so the
// panel mirrors the running server), and this just renders it and reports changes up.
//
// Two teaching dependencies are encoded in the UI itself:
//   - the floor lives on the cross-encoder logit, so the floor input is DISABLED unless
//     the reranker is on (a floor with no reranker does nothing);
//   - a cache HIT skips retrieval entirely, so a note reminds you to turn the cache off
//     while sweeping the floor, or the cached answer masks the knob's effect.

type Props = {
  value: AskControls;
  onChange: (next: AskControls) => void;
  disabled?: boolean; // while a query is in flight
};

function Toggle({
  label,
  on,
  onClick,
  disabled,
}: {
  label: string;
  on: boolean;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 font-mono text-[0.7rem] uppercase tracking-[0.15em] transition-colors disabled:opacity-40 ${
        on
          ? "border-amber-400/50 bg-amber-400/10 text-amber-300"
          : "border-ink-700 bg-ink-850 text-fog-400 hover:border-ink-600"
      }`}
    >
      <span
        aria-hidden
        className={`h-1.5 w-1.5 rounded-full ${on ? "bg-amber-400" : "bg-fog-600"}`}
      />
      {label}
    </button>
  );
}

export default function RetrievalControls({ value, onChange, disabled }: Props) {
  const set = (patch: Partial<AskControls>) => onChange({ ...value, ...patch });

  return (
    <div className="mb-2 flex flex-wrap items-center gap-2">
      <span className="font-mono text-[0.6rem] uppercase tracking-[0.2em] text-fog-500">
        retrieval
      </span>

      <Toggle
        label="reranker"
        on={value.rerank_enabled}
        onClick={() => set({ rerank_enabled: !value.rerank_enabled })}
        disabled={disabled}
      />
      <Toggle
        label="cache"
        on={value.cache_enabled}
        onClick={() => set({ cache_enabled: !value.cache_enabled })}
        disabled={disabled}
      />

      {/* Floor: only meaningful when the reranker is on (it acts on the reranker logit). */}
      <label
        className={`flex items-center gap-2 rounded-lg border border-ink-700 bg-ink-850 px-3 py-1.5 font-mono text-[0.7rem] uppercase tracking-[0.15em] transition-opacity ${
          value.rerank_enabled ? "text-fog-400" : "opacity-40"
        }`}
      >
        floor
        <input
          type="number"
          step="0.5"
          aria-label="rerank score floor"
          disabled={disabled || !value.rerank_enabled}
          value={value.rerank_score_floor ?? ""}
          placeholder="off"
          onChange={(e) =>
            set({
              rerank_score_floor:
                e.target.value === "" ? null : Number(e.target.value),
            })
          }
          className="w-16 rounded bg-transparent text-right text-fog-100 placeholder:text-fog-600 focus:outline-none disabled:cursor-not-allowed"
        />
      </label>

      {value.cache_enabled && (
        <span className="font-mono text-[0.6rem] normal-case tracking-normal text-fog-500">
          cache on — a hit skips retrieval, so floor/rerank won&rsquo;t change a repeated
          answer
        </span>
      )}
    </div>
  );
}
