# Q5 — Lexical search: query string → chunks whose *words* match, ranked

_Phase 2, milestone Q5. The second retrieval signal, beside Q2's semantic search: Postgres
full-text search. The half that catches what vectors miss._

Q5 is `app/retrieve/lexical.py::search_lexical(session, query_text, *, k,
owner_id=DEV_OWNER_ID) -> list[ScoredChunk]`. It parses the query string into a `tsquery`,
matches it against the `chunks.tsv` column (`@@`), ranks the matches by `ts_rank_cd`, and
returns them best-first — the same shape as `search_semantic`, so Q6 can fuse the two lists.
It is **not** wired into `retrieve()` yet; that happens at Q6 (fusion). Q5 stands the signal
up and measures it alone.

---

## 1. Semantic vs lexical — meaning vs words

The two signals fail in opposite directions, which is exactly why we run both:

| | Semantic (Q2) | Lexical (Q5) |
|---|---|---|
| Matches on | *meaning* (vector proximity) | *the actual words* (lexemes) |
| Great at | paraphrase, synonyms, "gist" | proper nouns, acronyms, IDs, code, rare tokens |
| Blind to | an exact token it never learned a direction for | anything phrased differently from the text |
| Returns | the `k` **nearest** rows — always something | **only rows that MATCH** — can be few, or zero |

An embedding model has no learned direction for a made-up product name like `Quenlorix` or
an ID like `INV-4471`, so semantic search ranks them by loose association. Lexical nails them
because the literal token is either present or not. Conversely, a question worded nothing like
the source text shares no lexemes and lexical returns nothing — where semantic shines.

---

## 2. The full-text machinery: `tsvector`, `tsquery`, `@@`

Postgres full-text search has three moving parts:

- **`tsvector`** — the searchable form of a *document*: its text lexed into normalized
  **lexemes** (lowercased, stop-words dropped, stemmed — "governs"/"governing" → `govern`)
  with positions. Our `chunks.tsv` is a **`GENERATED … STORED`** column
  (`to_tsvector('english', content)`, added in Q1), so Postgres maintains it from `content`
  automatically — ingest never writes it, and it's covered by a **GIN** index.
- **`tsquery`** — the searchable form of a *query*: lexemes combined with operators
  (`&` and, `|` or, `!` not, `<->` followed-by).
- **`@@`** — the match operator: `tsv @@ tsquery` is true when the doc satisfies the query.
  The GIN index makes this fast.

```python
tsquery = func.websearch_to_tsquery("english", query_text)
rank    = func.ts_rank_cd(Chunk.tsv, tsquery)
select(Chunk, rank).where(Chunk.tsv.op("@@")(tsquery)).order_by(rank.desc()).limit(k)
```

Note the structural difference from semantic search: there's a `WHERE … @@ …` **match
predicate**. Semantic search has no such filter — every row is a candidate and distance only
orders them. Lexical returns *only* matches, so it can return fewer than `k`, or zero. That's
not a bug to hide; it's the property that makes it complementary.

---

## 3. Parsing user input: `websearch_to_tsquery` (not `plainto`/`to_tsquery`)

A user types a search box, not a raw `tsquery`. Three parsers turn text into a `tsquery`:

| parser | honors operators? | on plain prose |
|---|---|---|
| `to_tsquery` | requires raw syntax (`a & b`) | **raises** on prose — too brittle |
| `plainto_tsquery` | ignores all (quotes, `or`, `-`) | ANDs every term |
| **`websearch_to_tsquery`** | `"phrase"`→`<->`, `or`→`\|`, `-x`→`!x` | never raises; Google-style |

We use **`websearch_to_tsquery`**: the caller is a human search box, so it should degrade
gracefully and give users the operators they already expect. A test
(`test_search_lexical_parses_websearch_or_operator`) pins this: the query `"orbital or
quenlorix"` must match a chunk containing *either* term — `websearch` reads `or` as `|`,
whereas `plainto` would drop "or" as a stop-word and AND the rest, matching neither.

**The AND default has teeth** (see §5): for space-separated words with no operator,
`websearch_to_tsquery` ANDs them (`port kessler harbor` → `port & kessler & harbor`). A full
natural-language question therefore requires *every* non-stop-word to appear in one chunk —
usually nothing does.

---

## 4. Ranking with `ts_rank_cd`, and the score direction

`@@` is boolean; ranking orders the matches. We use **`ts_rank_cd`** ("cover density"):
it scores by how many query lexemes hit **and how tightly they cluster** — a chunk where the
terms sit close together (a small "cover") beats one where they're scattered. We picked `_cd`
over plain `ts_rank` because proximity is a real relevance signal for multi-word queries; for
a single term the two behave alike (frequency-driven).

The `ts_rank_cd` value **is** the `ScoredChunk.score`, and **higher = more relevant** — the
same direction as semantic's cosine similarity, so fusion (Q6) reads both lists uniformly. As
in `semantic.py`, we build the rank expression **once** and reuse it for both the `SELECT` and
the `ORDER BY`, so the sort and the reported score can't drift apart.

### How this differs from BM25

`ts_rank_cd` is *not* **Okapi BM25** — the standard IR ranking function (Lucene /
Elasticsearch / Postgres-via-ParadeDB). The distinction is precise: **BM25 is a *ranking*
function, not a matching one.** The *matching* step is the same in both (does the doc contain
the query lexemes, per the boolean query) — our `WHERE tsv @@ tsquery`. All the difference is
in how the matched docs are then *scored*, so the real comparison is `ts_rank_cd` **vs** the
BM25 scoring formula:

```
              IDF(term) · [ TF · (k1 + 1) ]
BM25 = Σ_terms ─────────────────────────────────────
              TF + k1 · (1 − b + b · doclen/avgdoclen)
```

Three ideas are baked into that formula; our Postgres ranking has **none** of them:

- **IDF — term rarity (the big one).** BM25 upweights rare, discriminating terms using
  corpus-wide document-frequency stats. `ts_rank_cd` has **no IDF** — it never looks at how
  common a term is. Query `Quenlorix protocol`: BM25 knows "Quenlorix" (in one chunk) is the
  signal and "protocol" (in many) is noise, and weights the rare term far higher; `ts_rank_cd`
  treats them roughly equally. This is exactly the exact-token case lexical is *meant* to win,
  so it's the most consequential gap.
- **TF saturation (`k1`).** BM25 makes term frequency saturate — the 10th occurrence adds far
  less than the 2nd. `ts_rank` grows ~linearly, so a repetitive/keyword-stuffed chunk can
  over-rank.
- **Length normalization (`b`).** BM25 principledly discounts long docs (they match more by
  chance). Postgres offers only a crude version via `ts_rank_cd`'s `normalization` bitmask arg
  — coarse, and off by default (we don't pass it).

Conversely, `ts_rank_cd` has **one** signal classic BM25 lacks: **cover density** (term
*proximity*); BM25 is a pure bag of words. And one difference that is *our* choice, not
Postgres-vs-BM25: we set `websearch_to_tsquery`'s **AND** default (all terms required), whereas
BM25 engines usually default to **OR with partial credit** — that policy is what produced the
30/48 zero-match result in §5.

| | `ts_rank_cd` (ours) | BM25 |
|---|---|---|
| Term rarity (IDF) | ❌ none | ✅ core |
| TF saturation | ❌ ~linear | ✅ `k1` |
| Length normalization | ⚠️ crude, off | ✅ `b` |
| Term proximity | ✅ cover density | ❌ bag of words |
| Default match policy (as set) | AND (all terms) | usually OR (partial) |

So Postgres FTS ranking is **"BM25-ish"**: correct boolean matching, weaker scoring. True BM25
needs an extension — **ParadeDB / `pg_search`**, which embeds a Tantivy (Rust Lucene) index
with real BM25. **Deferred on purpose:** Q5's job was to stand up the lexical *signal* with zero
new infrastructure, and its ranking weaknesses are largely masked once **RRF fusion (Q6)**
combines it with semantic — RRF uses each list's *rank order*, not absolute scores, so lexical
only needs to float the right chunk near its own top (which even a no-IDF ranker often does for
exact-token queries). If eval later shows lexical *ranking* is the bottleneck, `pg_search` is
the swap: `search_lexical` is a sealed seam returning `list[ScoredChunk]`, so replacing its
internals touches neither fusion nor any caller.

---

## 5. Eval: the lexical-standalone baseline (and why it's "worse")

Ran the golden set (48 questions, 9 docs / 97 chunks) through `search_lexical` alone, against
the Q4 semantic baseline (`retrieval-20260717T124400_884165Z.json`):

| metric | semantic (baseline) | **lexical alone** |
|---|---|---|
| MRR | 0.881 | **0.344** |
| hit@1 | 0.792 | 0.333 |
| hit@3 | 0.979 | 0.354 |
| hit@10 | 1.000 | 0.354 |

**30 of 48 questions returned zero lexical matches.** That's §3's AND default: a
natural-language question ANDs all its content words, and no single chunk contains every one.
And hit@3 = hit@10 (0.354) — flat — because when lexical *does* match it matches few
candidates and ranks the right one high, or it matches nothing; extra `k` adds no recall.

**This is the expected, informative result, not a regression.** Two readings:

1. **Raw lexical on prose questions is brittle.** It is not a drop-in replacement for
   semantic and never was — used alone on full questions its recall ceiling is ~0.35.
2. **Its value is complementary, and only paid out under fusion.** For the exact-token cases
   (proper nouns, IDs) semantic ranks at position 2–3, lexical often ranks at position 1. RRF
   (Q6) combines the two rankings so the hybrid keeps semantic's broad recall *and* gains
   lexical's precision on the tokens vectors fumble. **The real delta to record is
   hybrid-vs-semantic at Q6** — Q5's job is to prove the lexical signal is correct and
   understand why it looks weak alone.

(Measurement was a throwaway adapter that swapped the harness's `retrieve` seam for a
lexical-backed one; the eval harness itself stays semantic-only by design — lexical enters
`retrieve()` via fusion at Q6, and *that* run gets recorded under `eval/results/`.)

The live view: `python -m app.retrieve.explain "Port Kessler harbor"` now prints the semantic
list (5 broad meaning-matches) beside the lexical list (2 chunks containing `port & kessler &
harbor`) — precise vs fuzzy, side by side.

---

## Alternatives rejected

- **`plainto_tsquery` / `to_tsquery`** — `to_tsquery` raises on prose; `plainto` throws away
  the operators a search-box user expects. `websearch_to_tsquery` is built for exactly this
  input (§3).
- **Plain `ts_rank`** — ignores term proximity; `ts_rank_cd` uses cover density, a better
  multi-word signal at no extra cost (§4).
- **Reaching `tsv` via raw SQL** instead of mapping it on the model — Q1 already mapped
  `Chunk.tsv` (typed `TSVECTOR`), so `Chunk.tsv.op("@@")(...)` stays in the ORM, no raw SQL.
- **Wiring lexical into `retrieve()` now** — tempting, but fusion is its own concern (Q6);
  keeping Q5 a standalone stage lets us measure the signal in isolation first.
- **Defining `ScoredChunk` per stage** — same reasoning as Q2: the shared type lives in
  `app/retrieve/types.py` so semantic + lexical lists are the *same* class for fusion.

## Gotcha hit during build

The AND default surprised the eval: full natural-language questions produced
`term & term & term …` tsqueries that matched nothing for 30/48 questions. Not a code bug —
it's how `websearch_to_tsquery` treats space-separated words, and it's precisely why lexical
alone underperforms and why fusion (Q6) is where it earns its keep.
