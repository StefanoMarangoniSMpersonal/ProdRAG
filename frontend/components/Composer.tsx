"use client";

import { useRef, useState } from "react";

// The question input. A textarea (not an <input>) so multi-line questions are natural;
// Enter submits, Shift+Enter inserts a newline — the convention users expect from chat.
// It's a controlled-ish component but owns its own draft text: the page only cares about
// the final submitted string, so lifting per-keystroke state up would be needless churn.
// `pending` disables it while an answer is in flight (one question at a time this phase).

type Props = {
  onSubmit: (query: string) => void;
  pending: boolean;
};

export default function Composer({ onSubmit, pending }: Props) {
  const [draft, setDraft] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  function submit() {
    const query = draft.trim();
    if (!query || pending) return;
    onSubmit(query);
    setDraft("");
    // Reset the auto-grown height back to one row after sending.
    if (ref.current) ref.current.style.height = "auto";
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function handleInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setDraft(e.target.value);
    // Grow the textarea to fit its content, capped by max-height in the className.
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="flex items-end gap-2 rounded-2xl border border-ink-700 bg-ink-850 p-2 shadow-lg shadow-ink-950/40 transition-colors focus-within:border-ink-600"
    >
      <textarea
        ref={ref}
        value={draft}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        rows={1}
        placeholder="Ask the corpus…"
        aria-label="Ask a question"
        disabled={pending}
        className="max-h-40 min-h-[2.25rem] flex-1 resize-none bg-transparent px-2 py-1.5 text-[0.95rem] leading-relaxed text-fog-100 placeholder:text-fog-500 focus:outline-none disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={pending || draft.trim().length === 0}
        aria-label="Send"
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-amber-400 text-ink-950 transition-all hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-ink-700 disabled:text-fog-500"
      >
        <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden>
          <path
            d="M8 13V3M8 3 3.5 7.5M8 3l4.5 4.5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </form>
  );
}
