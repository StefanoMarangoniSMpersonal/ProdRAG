import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DocumentStatus } from "@/lib/types";

// Mock the API layer, keeping the real ApiError (the page branches on it for upload errors).
vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>();
  return { ...actual, uploadDocument: vi.fn(), getDocumentStatus: vi.fn() };
});

import UploadPage from "@/app/upload/page";
import { getDocumentStatus, uploadDocument } from "@/lib/api";

const mockedUpload = vi.mocked(uploadDocument);
const mockedStatus = vi.mocked(getDocumentStatus);

function statusBody(status: DocumentStatus["status"]): DocumentStatus {
  return { id: "doc-1", status, filename: "notes.pdf", error: null };
}

describe("UploadPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockedUpload.mockReset();
    mockedStatus.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("polls a freshly uploaded doc from processing to ready", async () => {
    mockedUpload.mockResolvedValue({ id: "doc-1", status: "pending" });
    // Two poll cycles: still processing, then done.
    mockedStatus
      .mockResolvedValueOnce(statusBody("processing"))
      .mockResolvedValueOnce(statusBody("ready"));

    render(<UploadPage />);

    const file = new File(["hello corpus"], "notes.pdf", { type: "application/pdf" });
    // Drive the hidden file input; act() flushes the upload promise so the tracking row
    // mounts and schedules its first poll.
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Choose files to ingest"), {
        target: { files: [file] },
      });
    });
    expect(screen.getByText("notes.pdf")).toBeInTheDocument();

    // First poll (~1.5s) -> processing.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(screen.getByText("processing")).toBeInTheDocument();

    // Second poll -> ready; the loop then stops (no third getDocumentStatus call).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(mockedStatus).toHaveBeenCalledTimes(2);
  });

  it("surfaces an upload failure as an error row", async () => {
    const { ApiError } = await import("@/lib/api");
    mockedUpload.mockRejectedValue(new ApiError(400, "Empty file."));

    render(<UploadPage />);

    const file = new File([""], "empty.pdf", { type: "application/pdf" });
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Choose files to ingest"), {
        target: { files: [file] },
      });
    });

    expect(screen.getByText("upload failed")).toBeInTheDocument();
    expect(screen.getByText("Empty file.")).toBeInTheDocument();
    // A failed upload never enters the poll loop.
    expect(mockedStatus).not.toHaveBeenCalled();
  });
});
