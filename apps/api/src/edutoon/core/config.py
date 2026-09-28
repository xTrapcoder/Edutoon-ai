"""Application settings.

All configuration is read from the environment (or a local ``.env`` file).
Required variables have no default: if any are missing, :func:`get_settings`
raises a single error that names *every* missing variable at once, rather
than failing on the first one.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationError, model_validator
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

    # Script Generation Engine (SG-2). ``SCRIPT_LLM_MODEL`` is the model that
    # writes scripts: the worker builds a second LLM client bound to it for the
    # ``generate_script`` job, and the API queues a script for it. Claim judging
    # and topic extraction keep using ``LLM_MODEL``. The script records the model
    # its job actually ran with. Segment and claim caps bound a single
    # generation's size and cost; words-per-minute converts a segment's word count
    # into an estimated spoken duration.
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

    # Candidate claim extraction (Phase 6). Turns each *accepted* topic into a
    # handful of candidate factual claims, then runs each through the existing
    # Evidence Engine unchanged. Every one is optional: an ``.env`` written
    # before Phase 6 boots unchanged. ``CLAIM_INPUT_BUDGET_CHARS`` bounds how
    # much of a topic's own cited material one topic's extraction call is
    # shown - deliberately far smaller than ``TOPIC_INPUT_BUDGET_CHARS``,
    # since it is per topic, not per document. They are the single home of the
    # numbers ``domain.claim_extraction.ClaimCandidateLimits`` is built from.
    CLAIM_INPUT_BUDGET_CHARS: int = Field(default=12_000, ge=1)
    MAX_CLAIMS_PER_TOPIC: int = Field(default=6, ge=1)
    MAX_CLAIM_TEXT_CHARS: int = Field(default=300, ge=1)

    # Mirrors ``TOPIC_EXTRACTION_STALE_AFTER_SECONDS``: how long an
    # ``extract_claims`` job may sit "running" with no worker holding it
    # before it is treated as abandoned and failed.
    CLAIM_EXTRACTION_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)

    # Duration-aware, topic-scoped script generation (Phase 7). Every one is
    # optional: an ``.env`` written before Phase 7 boots unchanged. Replaces the
    # single whole-project ``compose_script`` call with one outline call plus one
    # call per topic "section" (further split if a topic's own budget would make
    # a single call too large) - see ``domain.script_duration`` for the pure
    # allocation/splitting maths these bound, and ``services.scripts`` for how
    # they are spent. ``MAX_CLAIMS_PER_GENERATION`` (above) still bounds the
    # *whole* script's claim count; ``MAX_CLAIMS_PER_SECTION`` bounds one
    # section's call.
    #
    # ``MIN_SECTION_DURATION_MS`` is the floor every accepted topic's section
    # receives before evidence-proportional duration is added on top, and is
    # also the enqueue-time check that a project has not accepted more topics
    # than its target duration can give each a floor (rule: never silently
    # squeeze a topic).
    #
    # ``MAX_SECTION_DURATION_MS`` is a *per provider call* ceiling, not a cap on
    # a topic's total budget: a topic allocated more than this is split into
    # several ordered sub-sections (same ``topic_id``), never one huge call.
    MIN_SECTION_DURATION_MS: int = Field(default=30_000, ge=1)
    MAX_SECTION_DURATION_MS: int = Field(default=300_000, ge=1)

    # How many accepted topics one generation may cover - bounds worst-case
    # provider-call count and cost independently of how many topics a project
    # happens to have accepted. Enforced at enqueue time (a clear validation
    # error, never a silent drop of the excess topics).
    MAX_SCRIPT_SECTIONS: int = Field(default=20, ge=1)

    # How many of a topic's own supported claims one section's provider call may
    # see - the oldest-first, deterministically chosen subset when a topic has
    # more than this many (never random, never borrowed from another topic).
    MAX_CLAIMS_PER_SECTION: int = Field(default=15, ge=1)

    # How far the WPM-based ``estimated_duration_ms`` may sit from
    # ``project.target_duration_ms`` (a fraction of the target) before exactly
    # one bounded correction pass re-generates every section with a rescaled
    # word budget. The script is accepted after that pass whatever the result -
    # duration is a target for a not-yet-real TTS estimate, never a hard gate
    # (only structural/provenance failures reject a script).
    SCRIPT_DURATION_TOLERANCE: float = Field(default=0.15, gt=0, lt=1)

    # How many bounded correction passes a generation may run when the
    # assembled script lands outside ``SCRIPT_DURATION_TOLERANCE`` - each pass
    # re-generates every section once with a rescaled word budget. ``0``
    # disables correction outright (the first pass's result is always
    # accepted); there is no automatic retry beyond this: a script is accepted
    # after its last permitted pass whatever the remaining difference.
    MAX_SCRIPT_CORRECTION_ATTEMPTS: int = Field(default=1, ge=0)

    # How bounded continuity context (previous/next section) is capped, in
    # characters, when built into a section's prompt - deliberately far smaller
    # than a whole script, so passing it never approaches a single huge call.
    CONTINUITY_CONTEXT_CHARS: int = Field(default=600, ge=1)

    @model_validator(mode="after")
    def _section_duration_bounds_are_consistent(self) -> Settings:
        if self.MAX_SECTION_DURATION_MS < self.MIN_SECTION_DURATION_MS:
            raise ValueError(
                "MAX_SECTION_DURATION_MS must be >= MIN_SECTION_DURATION_MS "
                f"({self.MAX_SECTION_DURATION_MS} < {self.MIN_SECTION_DURATION_MS})."
            )
        return self


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
