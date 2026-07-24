import { afterEach, describe, expect, it, vi } from "vitest";

import { ask, getAskConfig } from "@/lib/api";

// The API-layer spec for the live-controls feature: `ask()` must carry the per-request
// read-path overrides in the POST body, and `getAskConfig()` must read the server's
// current defaults so the control panel can mirror them. We stub the global `fetch` and
// inspect exactly what the layer sends — no backend, no network.

function stubFetch(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => body,
  } as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("ask()", () => {
  it("sends the query and the per-request control overrides in the body", async () => {
    const fetchMock = stubFetch({});

    await ask("hello?", {
      rerank_enabled: true,
      cache_enabled: false,
      rerank_score_floor: -3.5,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/ask");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toMatchObject({
      query: "hello?",
      rerank_enabled: true,
      cache_enabled: false,
      rerank_score_floor: -3.5,
    });
  });

  it("sends only the query when called with no overrides", async () => {
    const fetchMock = stubFetch({});

    await ask("hi?");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ query: "hi?" });
  });
});

describe("getAskConfig()", () => {
  it("GETs /ask/config and returns the parsed defaults", async () => {
    const cfg = {
      rerank_enabled: true,
      cache_enabled: false,
      rerank_score_floor: null,
    };
    const fetchMock = stubFetch(cfg);

    const result = await getAskConfig();

    expect(result).toEqual(cfg);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit | undefined];
    expect(url).toContain("/ask/config");
    expect(init?.method ?? "GET").toBe("GET");
  });
});
