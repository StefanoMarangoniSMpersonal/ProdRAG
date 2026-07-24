import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AskResponse } from "@/lib/types";

// Mock the API layer, keeping the REAL ApiError class (the page does `instanceof ApiError`
// to branch a 400 vs. a network failure — a plain stub would break that check). Only `ask`
// is replaced with a spy we drive per test.
vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>();
  return { ...actual, ask: vi.fn(), getAskConfig: vi.fn() };
});

import ChatPage from "@/app/page";
import { ApiError, ask, getAskConfig } from "@/lib/api";

const mockedAsk = vi.mocked(ask);

const CANNED: AskResponse = {
  query_id: "q-1",
  answer: "Proactive autoscaling scales ahead of predicted load.",
  citations: [147], // chunk 147 is cited; chunk 11 was shown but not used
  retrieved_chunk_ids: [147, 11, 3],
  reranked_chunk_ids: [147, 11],
  sources: [
    { id: 147, content: "Scale ahead of demand using a forecast signal.", score: 0.912 },
    { id: 11, content: "Reactive scaling only fires after a threshold trips.", score: 0.734 },
  ],
  timings_ms: { total: 512 },
};

describe("ChatPage", () => {
  beforeEach(() => {
    mockedAsk.mockReset();
    // The page seeds its controls from GET /ask/config on mount; keep that offline and
    // deterministic so it never touches the network during the test.
    vi.mocked(getAskConfig).mockResolvedValue({
      rerank_enabled: false,
      cache_enabled: false,
      rerank_score_floor: null,
    });
  });

  it("renders the grounded answer with its cited and seen sources", async () => {
    mockedAsk.mockResolvedValue(CANNED);
    const user = userEvent.setup();

    render(<ChatPage />);
    await user.type(
      screen.getByLabelText("Ask a question"),
      "How does autoscaling work?{Enter}",
    );

    // The user's question and the grounded answer both land in the transcript.
    expect(await screen.findByText("How does autoscaling work?")).toBeInTheDocument();
    expect(
      await screen.findByText(/Proactive autoscaling scales ahead/),
    ).toBeInTheDocument();

    // ask() was called with the trimmed query AND the per-request controls object
    // (authorized spec revision: the live-controls feature makes ask carry the settings).
    expect(mockedAsk).toHaveBeenCalledWith(
      "How does autoscaling work?",
      expect.any(Object),
    );

    // The sources panel: 2 shown, 1 cited — and the cited/seen split is surfaced.
    expect(screen.getByText(/2 shown · 1 cited/)).toBeInTheDocument();
    expect(screen.getByText("cited")).toBeInTheDocument();
    expect(screen.getByText("seen")).toBeInTheDocument();

    // Each shown passage's text is rendered (the evidence behind the answer). It appears
    // twice per source — the collapsed summary preview and the expandable body — so match
    // all occurrences rather than expecting exactly one.
    expect(screen.getAllByText(/Scale ahead of demand/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Reactive scaling only fires/).length).toBeGreaterThan(0);
  });

  it("shows a backend 400 detail inline instead of the answer", async () => {
    mockedAsk.mockRejectedValue(new ApiError(400, "Query must not be blank."));
    const user = userEvent.setup();

    render(<ChatPage />);
    await user.type(screen.getByLabelText("Ask a question"), "hi{Enter}");

    expect(await screen.findByText("Query must not be blank.")).toBeInTheDocument();
  });
});
