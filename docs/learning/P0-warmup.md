# P0 — Reranker warm-up on startup

_Phase 2.5, milestone P0 (the first). Pays a one-time model-load cost at process startup
instead of on the first user query — the "obvious future fix" Q10 flagged when it measured
a 24.8 s cold first `/ask`._

---

## 1. The problem: lazy loading moves the cost, it doesn't remove it

The rerank stage (Q7) loads a ~90 MB cross-encoder the first time it runs, via
`rerank._get_reranker` — an `@lru_cache(maxsize=1)` function whose body imports torch and
calls `AutoModel...from_pretrained`. That laziness is deliberate and worth keeping: it's
why importing `app.retrieve.rerank` (and running the whole offline test suite) never drags
torch in, and why rerank-OFF runs stay light.

But lazy loading only *relocates* the cost to whoever triggers the first call. With rerank
enabled, that's the **first live query** — measured at **24.8 s**, versus **2.3 s** once
warm. A latency papercut, but a brutal first impression.

P0's move: keep the loader lazy, but *trigger it ourselves at startup* so no user request
is ever the one that pays. This is the general **eager warm-up vs lazy init** trade — lazy
init defers cost until first use and risks a cold-start spike; eager warm-up front-loads it
into a phase where a spike is free (nobody is waiting on a boot). The read path is the right
place for eager, because it's a long-running server: one warm-up amortises across every
request the process ever serves.

## 2. Where startup code goes now: the ASGI lifespan protocol

FastAPI's old `@app.on_event("startup")` / `("shutdown")` decorators are deprecated. The
modern replacement is a **lifespan** — a single `@asynccontextmanager` passed as
`FastAPI(lifespan=...)`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    ...        # runs once on startup, BEFORE the first request
    yield      # the app serves requests here
    ...        # runs once on shutdown
```

Vocabulary: **ASGI lifespan** is a protocol-level channel (separate from HTTP requests) the
server uses to tell the app "I'm starting" / "I'm stopping". The single context manager
replaces the two decorators because setup-before-`yield` and teardown-after-`yield` is the
natural shape for a resource that's acquired once and released once — and, unlike two
independent callbacks, a `try/finally` around the `yield` can pair them.

## 3. Two decisions that keep P0 from leaking

**Gate on the flag, read it fresh.** Warm-up runs only when `get_settings().rerank_enabled`
is true. Skipping the gate would defeat the whole point of the lazy loader: an unconditional
warm-up would import torch on *every* boot, including the default rerank-OFF configuration
that is carefully torch-free. And it reads `get_settings()` **freshly inside the lifespan**,
not the module-level `settings` captured at import — so a test can flip the flag on the
cached settings object and the lifespan sees it.

**`asyncio.to_thread` for the load.** The model load is synchronous, CPU-bound, and takes
seconds. Awaiting it directly in the async lifespan would block the event loop for that
whole time. `await asyncio.to_thread(rerank._get_reranker)` runs the blocking work on a
worker thread and hands control back to the loop — the same discipline `rerank()` itself
uses for the forward pass. (The lifespan runs before any request, so nothing is *waiting* on
the loop here — but the habit is the point: never call blocking code straight from `async`.)

## 4. Best-effort: a speedup must not become a boot failure

Loading weights can fail — no network to fetch them, a corrupt cache, an OOM. The warm-up is
wrapped so any exception is logged (`app.startup`) and **swallowed**; startup proceeds. The
consequence of a swallowed failure is precisely today's behavior: the first query loads the
model lazily and pays the cold cost. That's a graceful degradation, not an outage.

The stance mirrors `ask.py`'s best-effort audit write ("the audit matters; it does not
matter more than the product"): an *optimisation* that can take down the server is worse than
no optimisation at all.

## 5. The gotcha: `ASGITransport` does not fire lifespan events

Every existing endpoint test drives the app with `httpx.ASGITransport(app=app)` — and
**ASGITransport deliberately skips the lifespan protocol**, sending only HTTP scopes. That
cuts two ways here:

- **It's why P0 is safe for the immutable tests.** `test_ask.py` / `test_upload.py` pin exact
  monkeypatch seams; adding a lifespan can't perturb them because their transport never runs
  the startup hook. No spec revision, nothing to stop-and-ask about.
- **It's why the P0 test can't use ASGITransport.** To exercise startup, `test_warmup.py`
  enters the lifespan **directly**:

  ```python
  async with app.router.lifespan_context(app):
      assert recorder.calls == 1
  ```

  No new dependency (`asgi-lifespan` would have been the alternative), no torch: the
  `_get_reranker` seam is monkeypatched with a call-recorder, so "was the model warmed?"
  collapses to "was the seam called, exactly once?". Three cases pinned: gate-off → zero
  calls (proves no torch path is even touched), gate-on → exactly one, and gate-on-but-raises
  → the lifespan still completes.

## 6. What's verified

- Offline suite **147 pass + 5 deselected-live, 0 skip** (was 144+5): `test_warmup.py` (3),
  all red-first — the two gate-on cases failed before the lifespan existed. `test_ask.py` /
  `test_upload.py` unchanged and green, confirming the lifespan didn't disturb the
  ASGITransport-driven tests.
- Live (optional): `RERANK_ENABLED=true` + `.\dev.ps1` → the `app.startup` warm-up line logs
  before any request and the **first** `/ask` returns in the warm ~2.3 s band, not ~24.8 s;
  with the flag unset, startup logs the skip and no torch is imported.
