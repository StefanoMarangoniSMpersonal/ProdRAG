# P6 — Chat + Upload Frontend

> Phase 2.5's last milestone: the first real user-facing surface. Until now the only page
> was a `/health/db` probe; the RAG system could only be driven with `curl`. P6 builds the
> chat + upload UI on Next.js (App Router), consuming the existing `POST /ask` and
> `POST /documents` contracts. **No auth** — everything runs as `DEV_OWNER_ID` server-side;
> auth is Phase 3. This note is the teaching record; the status table is
> `docs/PROGRESS-PHASE2.5.md`, the contract-change rationale is ADR `0007`.

## What shipped

- **Part A (backend, additive):** enriched the `POST /ask` response with `sources` — the
  shown passages (`{id, content, score}`), not just bare `citations: list[int]`. The chunks
  were already in memory at response time (`result.chunks`), so returning them is free.
  Rationale + why `sources` = *shown* chunks (not only *cited*): ADR `0007`.
- **Part B (foundation):** `lib/types.ts` (hand-written mirrors of the backend Pydantic
  shapes), `lib/api.ts` (the single fetch wrapper + typed `ApiError`), and the layout
  header (Chat | Upload nav + a live `/health/db` badge). The "Signal" design system lives
  in `app/globals.css`.
- **Part C (chat):** `app/page.tsx` + `components/{ChatMessage,Composer,SourceList}.tsx`.
- **Part D (upload):** `app/upload/page.tsx` + `components/{UploadDropzone,DocumentStatusRow}.tsx`
  + `hooks/usePollDocument.ts`.
- **Part E (tests):** Vitest + React Testing Library; `chat.test.tsx`, `upload.test.tsx`.

## 1. Consuming a non-streaming JSON answer

The chat POSTs a query and **awaits the whole answer** — there is no token stream. (P1, SSE
streaming, is parked; when it unparks it changes *one function*, see §4.) The UI models this
as a tiny state machine on each assistant turn:

```
thinking  ──ask() resolves──▶  done   (carries the full AskResponse)
          ──ask() rejects───▶  error  (carries a human-readable message)
```

On submit, the page optimistically appends **two** turns — the user's bubble and a
placeholder assistant turn in `"thinking"` — then flips the assistant turn to `done`/`error`
when `ask()` settles (matched by a stable id). This "optimistic + settle" shape is exactly
what a streaming version would reuse: tokens would flow into the same `"thinking"` turn
instead of a single blob replacing it.

**Error branching that matters to a user.** `lib/api.ts` collapses every failure into one
`ApiError` type carrying an HTTP `status`, so the page can say something useful:
- `status === 0` → the fetch itself rejected (backend down / CORS / offline) → *"Can't reach
  the backend — is it running on :8000?"*
- `status === 400` → the backend rejected the query (blank / too long, from the P2 input
  guard) and put a reason in `detail` → show that reason inline.

That split is the whole reason `request()` normalizes network failures to `status: 0` rather
than letting the raw `TypeError` from `fetch` escape.

## 2. The citation / source contract (and a deviation from the plan)

The plan assumed inline `[n]` citation badges in the answer prose. **The real backend
contract doesn't support that faithfully**, and discovering this drove the whole
`SourceList` design:

- `generate.py` returns `{answer: str, citations: list[int]}`. The `answer` is plain prose
  with **no inline `[n]` markers**; `citations` is a *separate* list of raw **chunk ids**
  (e.g. `147`) — meaningless numbers to a reader, and there's nothing in the text to anchor a
  badge to (chunk-level citation, per ADR `0002`).
- `sources` (added in Part A) is the **superset actually shown** to the model — every
  reranked chunk, `citations ⊆ {s.id for s in sources}`.

So instead of fabricating inline anchors, `SourceList` renders a panel beneath the answer:
each shown passage gets a stable **1-based display number**, and the cited/uncited split is
surfaced — **cited** = the model's evidence, **seen** = context it was shown but didn't lean
on. That's honest to the contract *and* useful for eyeballing grounding (did the answer
actually use the retrieved passages, or ignore them?). Getting inline badges would require a
**backend** change (emit answers with inline span markers) — an architecture decision, noted
for later, not hacked in the UI.

## 3. The 202-and-poll upload

Ingestion is **job-shaped** (a core project principle — see CLAUDE.md): `POST /documents`
persists the file, inserts a `pending` row, kicks off the job, and returns **202 + the doc
id** — it does *not* block until the document is ready. The client's job is to **poll**
`GET /documents/{id}` until the status is terminal.

`usePollDocument(id, initialStatus)` is that watch loop for one document:
- **`setTimeout` recursion, not `setInterval`.** Each tick awaits a network round-trip;
  `setInterval` would fire again regardless of whether the previous request returned, stacking
  overlapping polls under a slow backend. Chaining the next timer only *after* the current
  tick resolves gives a clean "wait ~1.5 s **between** polls" with no overlap.
- **Stops at a terminal state** (`ready`/`failed`) — it simply doesn't schedule the next
  timer. Cleanup on unmount (`alive` flag + `clearTimeout`) means a row that scrolls away or a
  navigation stops hitting the backend.
- **Fail-soft:** a transient poll error flags `unreachable` and keeps retrying rather than
  killing the loop, so the row recovers on its own when the backend blips back.

**A rules-of-hooks lesson.** A row that's still uploading (no id yet) or that failed to upload
has nothing to poll — but you can't call a hook conditionally. So the polling lives in a
`TrackedDocumentRow` that only *mounts* once an id exists, and the plain presentational
`DocumentStatusRow` stays hook-free. The component boundary is what makes the conditional
"poll or don't" legal.

**Scope boundary:** there is no `GET /documents` list endpoint, so the upload page can only
show docs uploaded *this session* — a reload clears the list (the docs stay ingested; we just
can't re-enumerate them). A persistent library is a future backend addition.

## 4. Why `lib/api.ts` is the SSE swap seam

Every network call goes through one `request()` wrapper, and every component imports
`ask`/`uploadDocument`/… by name. When P1 unparks, `ask()` becomes an `EventSource`/
`ReadableStream` reader that yields tokens — **and nothing else changes**: the chat page
already drives an assistant turn through a `thinking → done` lifecycle, so streaming just
feeds that same turn incrementally. Centralizing fetch logic in one module is precisely so
this transport swap is a one-file change, not a component rewrite.

## 5. Why auth is a separate phase

The frontend does **zero** auth: no Supabase client, no JWT, no session. The backend runs
every request as `DEV_OWNER_ID`. This is deliberate — auth (Supabase Auth: JWT/ES256/JWKS +
RLS + the Supavisor per-transaction-claims dance) is a whole layer with its own failure modes
and is **Phase 3**. P6 keeps `owner_id` as the seam it already is, so dropping real identity
in later is additive, not a reshape of the UI.

## 6. Testing (Vitest + React Testing Library)

- **`chat.test.tsx`** mocks `lib/api` but keeps the **real `ApiError`** class (the page does
  `instanceof ApiError` to branch 400-vs-network — a plain object stub would break that). It
  drives a canned `AskResponse` and asserts the answer renders, `ask()` got the trimmed query,
  and the sources panel shows the cited/seen split; a second test drives a rejected
  `ApiError(400)` and asserts the `detail` shows inline.
- **`upload.test.tsx`** uses **fake timers** to walk the poll loop: mock `getDocumentStatus`
  to return `processing` then `ready`, `advanceTimersByTimeAsync(1500)` twice, and assert the
  row transitions and that the loop **stops** after terminal (exactly 2 status calls).

**Two tooling decisions (glue, recorded for the next dependency refresh):**
1. **vitest 2, not 3.** vitest 3 pulls vite 7, whose peer demands a newer `@types/node` than
   the repo pins (`22.10.2`). Staying on vitest 2 avoids forcing a version bump; it fully
   supports React 19 + Testing Library 16.
2. **Explicit `vite@^5` pin.** `@vitejs/plugin-react` greedily resolved vite 6 top-level while
   vitest bundled vite 5 nested — two majors meant `defineConfig`'s `Plugin` type didn't match
   `react()`'s, and `tsc` failed on the config file (the tests ran fine). Pinning top-level
   vite to 5 collapses everything to one vite copy, fixing the type clash properly instead of
   casting it away.

A jsdom gotcha worth noting: jsdom doesn't implement `Element.scrollIntoView`, and the chat
page calls it in an effect on every turn change (optional chaining guards a null ref, not a
missing method). `vitest.setup.ts` installs a no-op stub.

## 7. The design system ("Signal")

Config-less Tailwind v4 (`@theme` in `app/globals.css`): a deep-ink ground, a single warm
amber accent (an instrument readout, not the purple-on-white AI cliché), an editorial serif
for display, a technical sans for body, and **mono for every piece of machine data** (chunk
ids, scores, timings) — a deliberate signal that those are exact values from the pipeline,
not prose. One orchestrated page-load reveal (`rise`) and a `pulse-dot` "thinking" animation,
both guarded by `prefers-reduced-motion`.
