"""Phase 2 read side: query -> retrieved chunks.

One stage per module, mirroring `app/ingest/`: semantic (Q2), lexical (Q5), fusion (Q6),
rerank (Q7). `types.py` holds the shared result vocabulary (`ScoredChunk`).
"""
