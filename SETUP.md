# Claude Code Setup — do this before Phase 1

An ordered checklist. Roughly a day of work; it pays for itself within the first week.
Items marked **(decision)** are things *you* decide — don't let Claude Code pick them.

## 0. Install and authenticate Claude Code

```bash
# Recommended: native installer
curl -fsSL https://claude.ai/install.sh | bash
# (or: brew install --cask claude-code)
claude auth login
```

Verify with `claude --version` and open a session in an empty project dir.

## 1. Provision accounts and keys (do these up front so you never stall mid-build)

- [ ] **Google AI / Gemini** API access (AI Studio or Vertex)
- [ ] **AWS** account with an IAM user/role scoped to S3 + Fargate
- [ ] **Supabase** project — enable the `vector` (pgvector) extension
- [ ] **Supabase Auth** — enable Email (and any desired social) providers on the same
      project; switch it to **asymmetric JWT signing keys (ES256)** so the backend can
      verify tokens via the JWKS endpoint
- [ ] **Sentry** project + **LangSmith** account
- [ ] **GitHub** repo created
- [ ] Reranker + parsing provider keys **if** going hosted (e.g. Cohere Rerank; Docling/
      LlamaParse as parsing alternatives to Unstructured)
- [ ] Decide a **secrets approach** before any key touches the repo (start simple: a
      git-ignored `.env` + `.env.example`; never commit real values)

## 2. Make the two upstream decisions **(decision)**

These sit above your database schema — settle them before Phase 2, ideally now:

- [ ] **Embedding model** — model + dimensionality + API-vs-self-hosted + multilingual?
- [ ] **Chunking strategy** — target size + overlap + structure-aware rules

Record both in `CLAUDE.md` (there are `TODO` placeholders waiting for them).

## 3. Prepare your data **(decision — most-forgotten step)**

- [ ] Assemble a **real, representative corpus** you actually understand (your own PDFs, a
      domain you know). Toy data teaches you nothing about chunking or retrieval.
- [ ] Hand-write a small **golden set** — 15–25 question/expected-answer pairs over that
      corpus — and save it to `/eval/golden.jsonl`. Build this yourself; it's how you
      internalize what "good" looks like. Every later phase measures against it.

## 4. Drop in the Claude Code config (these files)

Copy this bundle into your repo root:

```
your-repo/
├── CLAUDE.md                              # project constitution (read every session)
└── .claude/
    ├── agents/
    │   └── retrieval-debugger.md          # read-only RAG diagnostic subagent
    └── skills/
        ├── eval-run/SKILL.md              # /eval-run   — measure quality vs golden set
        ├── ingest-inspect/SKILL.md        # /ingest-inspect — eyeball chunking
        └── explain-retrieval/SKILL.md     # /explain-retrieval — trace a query
```

Then, inside a Claude Code session in the repo:

- [ ] Run `/agents` to confirm `retrieval-debugger` is registered.
- [ ] Type `/` to confirm the three skills appear (`/eval-run`, `/ingest-inspect`,
      `/explain-retrieval`).
- [ ] Fill in the `TODO` decisions and the `Commands` section of `CLAUDE.md` (you can run
      `/init` to auto-generate a first draft of build commands, then refine — but keep the
      learning-oriented instructions from the provided CLAUDE.md).

## 5. Connect the Postgres MCP server

This lets Claude Code inspect your pgvector tables, indexes, and query plans directly —
which is what makes `retrieval-debugger` genuinely useful.

- [ ] Add a Postgres MCP server pointing at your Supabase (read-only role recommended):
      `claude mcp add ...`  then verify inside a session with `/mcp`.

## 6. Set permissions so you stay in the driver's seat

- [ ] Run `/permissions` and **pre-approve safe, routine tools** (reads, `git` status/diff,
      common build/test commands) so you're not clicking approve constantly.
- [ ] **Do NOT** auto-approve anything destructive — deploys, `DROP`, migrations, `rm`.
      Side-effect skills already carry manual-only intent; keep it that way.

## 7. Learn the five session habits (these are the whole game)

- [ ] **Plan first.** `Shift+Tab` (or `/plan`) before any non-trivial change. Read the
      plan, adjust it, *then* let it build.
- [ ] **Review every diff** in the RAG core. `/diff` after a batch of edits.
- [ ] **Context hygiene.** `/clear` between unrelated tasks; `/compact` before starting a
      new phase in a long session; `/context` to watch the window.
- [ ] **Delegate exploration** to the built-in **Explore** agent so your main thread stays
      clean ("use the Explore agent to map the ingestion pipeline").
- [ ] **Interrogate, don't autopilot** on the RAG core — ask "why this over the
      alternatives" and read the answer.

## Then start Phase 0

Ask Claude Code (in plan mode) to scaffold: docker-compose (Postgres+pgvector, Redis),
the FastAPI skeleton, and the Next.js skeleton — and to explain what each service does as
it goes. Goal: "hello world" travels the whole stack.

---

### The one rule that matters most
On the boring layers, let Claude Code run. On ingestion, retrieval, and evaluation, slow
it down and make it teach — those three are the skill you're actually here to build.
