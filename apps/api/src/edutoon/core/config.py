"""Application settings.

All configuration is read from the environment (or a local ``.env`` file).
Required variables have no default: if any are missing, :func:`get_settings`
raises a single error that names *every* missing variable at once, rather
than failing on the first one.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Required (no defaults) ------------------------------------------------
    ENVIRONMENT: str
    DATABASE_URL: str
    DATABASE_DIRECT_URL: str
    REDIS_URL: str
    STORAGE_ENDPOINT_URL: str
    STORAGE_ACCESS_KEY_ID: str
    STORAGE_SECRET_ACCESS_KEY: str
    BUCKET_UPLOADS: str
    BUCKET_ASSETS: str
    BUCKET_SEGMENTS: str
    BUCKET_OUTPUTS: str
    CLERK_JWKS_URL: str
    CLERK_ISSUER: str

    # --- Optional (with defaults) -------------------------------------------------
    LOG_LEVEL: str = "INFO"
    APP_BASE_URL: str = "http://localhost:3000"
    API_BASE_URL: str = "http://localhost:8000"
    MAX_UPLOAD_BYTES: int = 104_857_600
    MAX_PDF_PAGES: int = 300


class MissingSettingsError(RuntimeError):
    """Raised when one or more required settings are absent from the environment."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        joined = "\n".join(f"  - {name}" for name in missing)
        super().__init__(
            "Missing required environment variables:\n"
            f"{joined}\n\n"
            "Set them in the environment or copy .env.example to .env."
        )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = sorted(
            {
                str(error["loc"][0])
                for error in exc.errors()
                if error["type"] == "missing" and error["loc"]
            }
        )
        if missing:
            raise MissingSettingsError(missing) from exc
        raise
