"""Application settings.

All configuration is read from the environment (or a local ``.env`` file).
Required variables have no default: if any are missing, :func:`get_settings`
raises a single error that names *every* missing variable at once, rather
than failing on the first one.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationError
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
    OPENAI_API_KEY: str
    ANTHROPIC_API_KEY: str

    # --- Optional (with defaults) -------------------------------------------------
    LOG_LEVEL: str = "INFO"
    APP_BASE_URL: str = "http://localhost:3000"
    API_BASE_URL: str = "http://localhost:8000"
    MAX_UPLOAD_BYTES: int = 104_857_600
    MAX_PDF_PAGES: int = 300
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    LLM_MODEL: str = "claude-sonnet-5"

    # Script Generation Engine (SG-2). ``SCRIPT_LLM_MODEL`` is the name recorded
    # on a script when it is queued (``services.scripts``); it does not choose the
    # model that is called - the API and the worker each build one client from
    # ``LLM_MODEL`` and use it for every task - so keep the two equal. Segment and
    # claim caps bound a single generation's size and cost; words-per-minute
    # converts a segment's word count into an estimated spoken duration.
    SCRIPT_LLM_MODEL: str = "claude-sonnet-5"
    MAX_SCRIPT_SEGMENTS: int = 12
    MAX_CLAIMS_PER_GENERATION: int = 40
    SCRIPT_WORDS_PER_MINUTE: int = 150

    # Topic extraction (Phase 5). Every one is optional: an ``.env`` written
    # before Phase 5 boots unchanged. ``TOPIC_INPUT_BUDGET_CHARS`` bounds how
    # much of the uploaded material one extraction call is shown - and so its
    # cost; the rest bound the size of the outline the model may return. They
    # are the single home of the numbers ``domain.topic.TopicLimits`` is built
    # from (the domain never reads configuration itself). ``ge=1`` rejects a
    # nonsensical value at start-up rather than mid-job.
    TOPIC_INPUT_BUDGET_CHARS: int = Field(default=48_000, ge=1)
    MAX_TOPICS_PER_EXTRACTION: int = Field(default=30, ge=1)
    MAX_TOPIC_DEPTH: int = Field(default=3, ge=1)
    MAX_TOPIC_TITLE_CHARS: int = Field(default=120, ge=1)
    MAX_TOPIC_SUMMARY_CHARS: int = Field(default=400, ge=1)
    MAX_TOPIC_KEYWORDS: int = Field(default=8, ge=1)
    MAX_TOPIC_KEYWORD_CHARS: int = Field(default=40, ge=1)
    MAX_TOPIC_EVIDENCE: int = Field(default=6, ge=1)

    # How long an extraction may sit ``running`` with no worker holding it before
    # it is treated as abandoned by a crashed worker and failed, so the project's
    # topic edits are not blocked for ever. A live job is never failed by this:
    # its worker holds the job's row lock (see ``jobs.fail_stale_running``), so
    # this is only the backstop. It must comfortably exceed the longest a model
    # call can take - the SDK's 600 s read timeout over its three attempts is
    # about 30 minutes - hence the default; lower it only if the client's timeout
    # is lowered too. ``ge=60`` rejects a value short enough to race a healthy
    # worker starting the job.
    TOPIC_EXTRACTION_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)


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
