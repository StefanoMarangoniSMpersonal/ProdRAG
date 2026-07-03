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

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process."""
    return Settings()
