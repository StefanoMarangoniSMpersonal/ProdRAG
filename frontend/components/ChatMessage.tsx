"use client";

import type { AskResponse } from "@/lib/types";
import SourceList from "./SourceList";

// One turn in the session-local conversation. A user turn is just text; an assistant
// turn is a small state machine — "thinking" while /ask is in flight, then either "done"
// (carrying the full AskResponse) or "error" (carrying a human-readable message). The
// page owns the array of these and flips an assistant turn's state when ask() settles.

export type ChatTurn =
  | { role: "user"; id: string; text: string }
  | {
      role: "assistant";
      id: string;
      state: "thinking" | "done" | "error";
      response?: AskResponse;
      error?: string;
    };

function ThinkingDots() {
  // Three amber dots on the "signal-pulse" keyframe, phase-shifted by animation-delay so
  // they ripple rather than blink in unison — the retrieval instrument "listening".
  return (
    <span className="flex items-center gap-1.5" aria-label="Thinking">
      {[0, 0.2, 0.4].map((d) => (
        <span
          key={d}
          className="pulse-dot h-1.5 w-1.5 rounded-full bg-amber-400"
          style={{ animationDelay: `${d}s` }}
        />
      ))}
    </span>
  );
}

export default function ChatMessage({ turn }: { turn: ChatTurn }) {
  if (turn.role === "user") {
    return (
      <div className="rise flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm border border-ink-700 bg-ink-800 px-4 py-2.5 text-sm leading-relaxed text-fog-100">
          {turn.text}
        </div>
      </div>
    );
  }

  return (
    <div className="rise flex justify-start">
      <div className="w-full max-w-[92%]">
        {turn.state === "thinking" && (
          <div className="flex items-center gap-3 px-1 py-2 text-sm text-fog-400">
            <ThinkingDots />
            <span className="font-mono text-[0.7rem] uppercase tracking-[0.18em]">
              retrieving · grounding
            </span>
          </div>
        )}

        {turn.state === "error" && (
          <div className="rounded-xl border border-signal-bad/40 bg-signal-bad/10 px-4 py-3 text-sm text-signal-bad">
            {turn.error}
          </div>
        )}

        {turn.state === "done" && turn.response && (
          <div className="rounded-2xl rounded-bl-sm border border-ink-800 bg-ink-900/70 px-4 py-3.5">
            <p className="whitespace-pre-wrap text-[0.95rem] leading-relaxed text-fog-100">
              {turn.response.answer}
            </p>
            <SourceList
              sources={turn.response.sources}
              citations={turn.response.citations}
            />
          </div>
        )}
      </div>
    </div>
  );
}
