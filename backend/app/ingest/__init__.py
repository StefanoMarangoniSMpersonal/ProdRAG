"""The ingestion pipeline package.

Phase 1 builds this as a chain of small stages, each its own module:
M2 parse -> M3 chunk -> M4 embed -> M5 write, tied together by the M6 orchestrator.
Only `parse` exists so far. Stages before M5 speak Unstructured's native `Element`
type; the mapping onto our own `Chunk` model happens once, at the write stage.
"""
