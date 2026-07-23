"""Application configuration.

Everything that varies by environment (DB URL, allowed CORS origins) is read
from environment variables / a git-ignored `.env` file via pydantic-settings.
No secrets or environment-specific values live in code.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # asyncpg driver URL. Note the "+asyncpg" — SQLAlchemy uses it to pick the
    # async driver. Defaults point at the local docker-compose Postgres.
    database_url: str = "postgresql+asyncpg://prodrag:prodrag@localhost:5432/prodrag"

    # Origins allowed to call the API from a browser. Comma-separated in env.
    cors_origins: str = "http://localhost:3000"

    # Root directory where raw ingested files are stored on local disk (M1). Relative
    # paths resolve against the backend process's working directory, so the default
    # lands at backend/storage/ (git-ignored). Swapped for an S3 bucket later.
    storage_dir: str = "storage"

    # PDF/image parse strategy the ingestion pipeline uses (M2). Defaults to "hi_res"
    # because we always request table-structure inference, which only runs under hi_res
    # (it needs the poppler + tesseract binaries and runs a layout + table model —
    # slower, but ingestion is job-shaped so per-doc latency doesn't matter). Override
    # per-environment via INGEST_PDF_STRATEGY; ignored for text formats.
    ingest_pdf_strategy: str = "hi_res"

    # Chunking knobs the ingestion pipeline uses (M3, Unstructured `by_title`). The M6
    # orchestrator reads these and passes them to chunk_document (the same way it reads
    # ingest_pdf_strategy for parse). `max_characters` is the hard cap per chunk — kept
    # well under the ~8,192-token embedding input limit and tunable via /ingest-inspect;
    # `combine_text_under_n_chars` merges runt sections mis-detected as Titles. Override
    # per-environment via INGEST_CHUNK_MAX_CHARACTERS /
    # INGEST_CHUNK_COMBINE_TEXT_UNDER_N_CHARS.
    ingest_chunk_max_characters: int = 1500
    ingest_chunk_combine_text_under_n_chars: int = 500

    # Embedding (M4). We use the Gemini Developer API via the google-genai SDK with
    # API-key auth (not Vertex) — one key, no GCP project/IAM, matching single-tenant
    # naive-first. The key is read from GEMINI_API_KEY and defaults to "" so the app
    # still imports without it; embed_texts raises a clear error only when a real call
    # is attempted. 768 dims is the locked output size (fits pgvector's HNSW 2000-dim
    # cap). embedding_batch_size caps texts sent per API request. Override via
    # GEMINI_API_KEY / EMBEDDING_MODEL / EMBEDDING_DIMENSIONS / EMBEDDING_BATCH_SIZE.
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = 768
    embedding_batch_size: int = 100

    # Async ingestion layer (Celery + Redis). The worker consumes ingestion jobs from
    # Redis (broker-only — no result backend; documents.status is the source of truth).
    # Defaults point at the local docker-compose Redis. Override via CELERY_BROKER_URL.
    celery_broker_url: str = "redis://localhost:6379/0"

    # Stuck-job reaper knobs. `stuck_after` must exceed the longest legitimate
    # processing time (a hi_res parse+embed is tens of seconds); the reaper flips
    # 'processing' rows older than this back to 'pending' and requeues them, and the
    # fence makes a too-eager requeue merely wasteful, never corrupting.
    # `max_processing_attempts` is the poison-pill cap: past it the reaper marks a
    # repeatedly-crashing doc 'failed' instead of looping forever. `reaper_interval`
    # is the Beat cadence. Override via
    # INGEST_STUCK_AFTER_SECONDS / INGEST_REAPER_INTERVAL_SECONDS /
    # INGEST_MAX_PROCESSING_ATTEMPTS.
    ingest_stuck_after_seconds: int = 600
    ingest_reaper_interval_seconds: int = 120
    ingest_max_processing_attempts: int = 3

    # Retrieval (Q2). hnsw.ef_search is the HNSW recall knob: higher searches more graph
    # candidates -> better recall, slower. 40 is pgvector's default (no behavior change
    # from leaving it unset); search_semantic issues it per query via set_config.
    # Override via RETRIEVAL_HNSW_EF_SEARCH.
    retrieval_hnsw_ef_search: int = 40

    # Number of chunks retrieve() returns (Q3) -- the FINAL result size, unchanged
    # by Q7. Callers depend on it: the eval harness retrieves at k=10 and slices
    # hit@1/3/5, and the immutable retrieve tests assert retrieve(k=N) returns N. The
    # candidate pool the reranker sees is a SEPARATE knob (retrieval_candidate_k
    # below). Overridable per call and via RETRIEVAL_K.
    retrieval_k: int = 10

    # Reciprocal Rank Fusion smoothing constant (Q6): fused_score = sum 1/(k_constant +
    # rank) across the semantic + lexical rankings. 60 is the Cormack et al. default;
    # higher flattens the weight gap between adjacent ranks (consensus matters more than
    # any one list's #1), lower sharpens it. Override via RETRIEVAL_RRF_K_CONSTANT.
    retrieval_rrf_k_constant: int = 60

    # Cross-encoder rerank (Q7). rerank_enabled gates the stage: OFF (default) leaves
    # retrieve() at exact Q6 hybrid behavior -- no torch loaded, no model download, the
    # offline suite unaffected -- so set RERANK_ENABLED=true only for live eval / prod.
    # When ON, each retrieval arm fetches retrieval_candidate_k candidates (the wide
    # pool), RRF fuses them, and the cross-encoder reorders that pool down to
    # retrieval_k. A pool of 50 comfortably covers the near-misses rerank exists to
    # promote while keeping the forward pass to ~0.5-2 s on CPU. rerank_model is a local
    # HuggingFace cross-encoder (loaded via the already-installed transformers; ~90 MB,
    # cached on first use). Override via
    # RERANK_ENABLED / RETRIEVAL_CANDIDATE_K / RERANK_MODEL.
    rerank_enabled: bool = False
    retrieval_candidate_k: int = 50
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # Generation (Q8). The final RAG stage: a Gemini *generation* model turns the
    # retrieved chunks into a grounded, cited answer (reuses the same google-genai SDK +
    # GEMINI_API_KEY as embedding — no second key). generation_model is the model id
    # (architect-locked default; a config swap behind the generate() seam).
    # generation_temperature stays 0 for grounded extraction — we want faithful answers
    # from context, not creative ones, so determinism beats variety.
    # generation_max_output_tokens caps the answer length. Override via
    # GENERATION_MODEL / GENERATION_TEMPERATURE / GENERATION_MAX_OUTPUT_TOKENS.
    generation_model: str = "gemini-3.1-flash-lite"
    generation_temperature: float = 0.0
    generation_max_output_tokens: int = 1024

    # Guardrails (P2). Two guards around the read path, both typed Settings knobs.
    # citation_guard_enabled gates the OUTPUT guard: ON (default) validates the model's
    # citations against the chunks it was actually shown and repairs the client response
    # by dropping phantom (never-shown) ids; OFF returns the model's raw citations
    # unrepaired and emits no violation warning — a kill-switch to measure raw model
    # grounding in eval. The check itself is pure and always computed for the audit
    # log, so violations stay visible in the logs even with the guard off.
    # max_query_chars is the INPUT guard's length cap: a query longer than this is
    # rejected pre-spend (400), kept generously under the ~8,192-token embedding
    # input limit. Override via CITATION_GUARD_ENABLED / MAX_QUERY_CHARS.
    citation_guard_enabled: bool = True
    max_query_chars: int = 4000

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process."""
    return Settings()
