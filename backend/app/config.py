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

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process."""
    return Settings()
