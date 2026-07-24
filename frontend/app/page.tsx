"use client";

import { useEffect, useRef, useState } from "react";

import ChatMessage, { type ChatTurn } from "@/components/ChatMessage";
import Composer from "@/components/Composer";
import { ApiError, ask } from "@/lib/api";

// The chat surface — the home page. It owns a session-local list of turns (no
// persistence this phase) and drives the ask() lifecycle: on submit it appends the user
// turn AND a placeholder assistant turn in the "thinking" state, then flips that
// assistant turn to "done" or "error" when ask() settles. Because ask() is the single
// SSE swap seam in lib/api.ts, this page needs no changes if streaming is unparked later
// — it would just receive tokens into the same assistant turn instead of one blob.

// Module-level counter for turn keys: deterministic (unlike crypto.randomUUID, which some
// test/jsdom environments lack) and monotonic, which is all React needs for stable keys.
let _seq = 0;
const nextId = () => `t${++_seq}`;

const EXAMPLES = [
  "What does the corpus say about autoscaling?",
  "Summarize the retrieval evaluation results.",
  "How is document ingestion structured?",
];

export default function ChatPage() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [pending, setPending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  async function handleSubmit(query: string) {
    const assistantId = nextId();
    setTurns((prev) => [
      ...prev,
      { role: "user", id: nextId(), text: query },
      { role: "assistant", id: assistantId, state: "thinking" },
    ]);
    setPending(true);

    // Replace just the pending assistant turn (matched by id) with its settled form.
    const settle = (patch: Partial<Extract<ChatTurn, { role: "assistant" }>>) =>
      setTurns((prev) =>
        prev.map((t) =>
          t.id === assistantId && t.role === "assistant" ? { ...t, ...patch } : t,
        ),
      );

    try {
      const response = await ask(query);
      settle({ state: "done", response });
    } catch (err) {
      // Distinguish the two failure modes the UI can meaningfully explain: a 400 is the
      // backend rejecting the query (blank / too long) and carries a useful detail; a
      // status-0 ApiError is a network/CORS failure (is the backend running?).
      let message = "Something went wrong generating an answer.";
      if (err instanceof ApiError) {
        message =
          err.status === 0
            ? `Can't reach the backend — is it running on :8000? (${err.message})`
            : err.message;
      }
      settle({ state: "error", error: message });
    } finally {
      setPending(false);
    }
  }

  const empty = turns.length === 0;

  return (
    <main className="mx-auto flex min-h-[calc(100vh-4rem)] max-w-4xl flex-col px-6">
      {empty ? (
        <div className="flex flex-1 flex-col items-center justify-center py-16 text-center">
          <p className="rise font-mono text-[0.7rem] uppercase tracking-[0.3em] text-amber-400">
            grounded · cited · retrieval-augmented
          </p>
          <h1
            className="rise mt-4 font-display text-5xl leading-tight text-fog-100"
            style={{ animationDelay: "0.05s" }}
          >
            Ask the corpus.
          </h1>
          <p
            className="rise mt-3 max-w-md text-sm leading-relaxed text-fog-400"
            style={{ animationDelay: "0.1s" }}
          >
            Every answer is drawn only from ingested documents and shows the passages it
            was built from. If the corpus doesn&rsquo;t hold the answer, it says so.
          </p>
          <div
            className="rise mt-8 flex flex-wrap justify-center gap-2"
            style={{ animationDelay: "0.15s" }}
          >
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                onClick={() => handleSubmit(ex)}
                className="rounded-full border border-ink-700 bg-ink-850 px-3.5 py-1.5 text-xs text-fog-300 transition-colors hover:border-amber-400/50 hover:text-fog-100"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="flex-1 space-y-5 py-8">
          {turns.map((turn) => (
            <ChatMessage key={turn.id} turn={turn} />
          ))}
          <div ref={bottomRef} />
        </div>
      )}

      {/* Composer docks to the bottom; sticky so it stays reachable as the log scrolls. */}
      <div className="sticky bottom-0 -mx-6 bg-gradient-to-t from-ink-950 via-ink-950/95 to-transparent px-6 pb-6 pt-4">
        <Composer onSubmit={handleSubmit} pending={pending} />
        <p className="mt-2 text-center font-mono text-[0.65rem] text-fog-500">
          Answers are grounded in the ingested corpus · non-streaming
        </p>
      </div>
    </main>
  );
}
