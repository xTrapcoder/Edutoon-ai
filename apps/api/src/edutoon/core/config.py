"""Application settings.

All configuration is read from the environment (or a local ``.env`` file).
Required variables have no default: if any are missing, :func:`get_settings`
raises a single error that names *every* missing variable at once, rather
than failing on the first one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from edutoon.domain.render_animation import DEFAULT_FADE_IN_MS, DEFAULT_FADE_OUT_MS, MAX_FADE_MS


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

    # How a provider call is made safe to repeat and safe to wait for (pre-API readiness). Every
    # one is optional: an ``.env`` written before it boots unchanged.
    #
    # PROVIDER_RETRY_* is the one policy for repeating a call that failed *transiently* (a rate
    # limit, a timeout, a dropped connection, a 5xx) - ``providers.retry``. ``ATTEMPTS`` counts the
    # first try (``1`` disables retrying); the wait doubles from ``BASE`` up to ``MAX`` seconds.
    # A bad request, a rejected key or an exhausted quota is never retried.
    #
    # EMBEDDING_MAX_* are the size of one embeddings request. The defaults are the provider's own
    # hard ceilings for ``text-embedding-3-small`` (2048 inputs, 300 000 tokens); lower them to make
    # each request smaller, never higher. ``domain.embedding_batches`` explains the token estimate.
    #
    # LLM_THINKING is how extended thinking is configured on every Anthropic request: ``disabled``
    # sends ``thinking: {"type": "disabled"}`` (required, because a forced tool call cannot be
    # combined with thinking and the current model thinks adaptively when it is omitted); ``omit``
    # leaves the parameter out, for a model that rejects a disabled value. LLM_TIMEOUT_SECONDS
    # bounds one attempt at a call.
    LLM_THINKING: Literal["disabled", "omit"] = "disabled"
    LLM_TIMEOUT_SECONDS: float = Field(default=300.0, ge=10)
    #
    # The in-process spend guard (``services.spend_guard``; the application has no billing, so
    # the provider consoles' own spend limits are the real ceiling - set them before any real
    # testing). MAX_PROVIDER_CALLS_PER_JOB is a tripwire against a runaway loop, not a budget: no
    # legitimate job comes near it (the largest, a full-length script, is bounded by
    # ``domain.script_duration.max_provider_calls``, 65 at the defaults), and a job that reaches it
    # stops before making the next call. MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR bounds how many
    # paid jobs (indexing, topic and claim extraction, script and storyboard generation) one
    # project may start in an hour, and MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR how
    # many claims typed in by hand (each one an embedding and a model call) - both refuse with a
    # 429 before anything is started.
    MAX_PROVIDER_CALLS_PER_JOB: int = Field(default=500, ge=1)
    MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR: int = Field(default=30, ge=1)
    MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR: int = Field(default=120, ge=1)
    PROVIDER_RETRY_ATTEMPTS: int = Field(default=3, ge=1, le=8)
    PROVIDER_RETRY_BASE_DELAY_SECONDS: float = Field(default=2.0, ge=0)
    PROVIDER_RETRY_MAX_DELAY_SECONDS: float = Field(default=30.0, ge=0)
    EMBEDDING_MAX_INPUTS_PER_REQUEST: int = Field(default=2048, ge=1, le=2048)
    EMBEDDING_MAX_REQUEST_TOKENS: int = Field(default=300_000, ge=8192, le=300_000)

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

    # Mirrors TOPIC_EXTRACTION_STALE_AFTER_SECONDS / CLAIM_EXTRACTION_STALE_AFTER_SECONDS:
    # how long a generate_script job may sit "running" with no worker holding it (the
    # handler releases its row lock immediately, same as topic/claim extraction) before it
    # is treated as abandoned by a crashed worker and failed.
    SCRIPT_GENERATION_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)

    # Local narration synthesis with Piper (Phase 8). Every one is optional: an .env
    # written before Phase 8 boots unchanged. Piper is entirely local, open-source and
    # free to run - no company API key is needed for any of this.
    #
    # PIPER_VOICES_DIR holds the downloaded .onnx/.onnx.json voice files
    # (make voices / scripts/download_piper_voices.py); never committed to git.
    #
    # Piper has no genuine Indian-English voice in its catalogue (see
    # apps/api/README.md, "Text-to-speech"). PIPER_VOICE_EN_IN is therefore, by
    # default, the identical catalogue id as PIPER_VOICE_EN_GB, and
    # PIPER_EN_IN_IS_TEMPORARY_SUBSTITUTE records that fact explicitly rather than
    # letting it be inferred - domain.tts_voice, the API and the frontend all read
    # this flag so an en-IN segment's audio is always labelled as the temporary
    # substitution it is, never silently presented as Indian English.
    PIPER_VOICES_DIR: str = "var/piper-voices"
    PIPER_VOICE_EN_GB: str = "en_GB-cori-medium"
    PIPER_VOICE_EN_IN: str = "en_GB-cori-medium"
    PIPER_EN_IN_IS_TEMPORARY_SUBSTITUTE: bool = True

    # Safety caps on one synthesis job, all generous defaults - never required, never
    # meant to bind in ordinary use. MAX_TTS_CHARS_PER_CALL bounds one internal Piper
    # invocation (domain.script_audio splits a segment's text into chunks at sentence
    # boundaries under this, then concatenates their audio - never exposed as separate
    # segments). MAX_TTS_CHARS_PER_SEGMENT and MAX_TTS_CHARS_PER_SCRIPT are refused at
    # enqueue/synthesis time before any audio is generated, the same "cheap check
    # first" discipline as script generation's own MAX_SCRIPT_SECTIONS.
    MAX_TTS_CHARS_PER_CALL: int = Field(default=1000, ge=1)
    MAX_TTS_CHARS_PER_SEGMENT: int = Field(default=8000, ge=1)
    MAX_TTS_CHARS_PER_SCRIPT: int = Field(default=200_000, ge=1)

    # How many segments may synthesise at once. Piper is CPU-bound and fast (see
    # apps/api/README.md for measured real-time factors); 1 keeps a synthesis job's
    # resource use predictable and avoids contending for the same worker process.
    MAX_CONCURRENT_TTS: int = Field(default=1, ge=1)

    # How long a single segment's synthesis (all of its internal chunks) may run
    # before it is treated as failed - comfortably above the worst real measurement
    # (see apps/api/README.md), never expected to bind for Piper's typical text
    # lengths.
    TTS_SEGMENT_TIMEOUT_SECONDS: int = Field(default=120, ge=1)

    # Mirrors the other STALE_AFTER_SECONDS settings: how long a synthesise_voice job
    # may sit "running" with no worker holding it (same released-lock-immediately
    # shape as generate_script) before it is treated as abandoned and failed.
    SYNTHESIS_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)

    # Scene / Storyboard layer (Phase 9). Every one is optional: an .env written before
    # Phase 9 boots unchanged. Turns each segment's measured narration audio
    # (script_segment_audio.measured_duration_ms - the sole narration-completeness gate,
    # see domain.storyboard_timing) into an ordered set of fixed-template scenes. The
    # six templates and their per-slot character limits are a fixed contract
    # (domain.storyboard_template), not configuration.
    #
    # MIN_SCENE_DURATION_MS is the shortest a single scene may run; a segment too short
    # to give even one scene this floor is never squeezed into an under-length scene
    # (domain.storyboard_timing.effective_scene_count) - it is handled as the documented
    # degenerate case instead. MAX_SCENES_PER_SEGMENT caps how many scenes one segment's
    # single provider call may propose.
    MIN_SCENE_DURATION_MS: int = Field(default=1500, ge=1)
    MAX_SCENES_PER_SEGMENT: int = Field(default=6, ge=1)

    # Worst-case size bounds, enforced at enqueue time before any provider call
    # (never merely documented) - the same "cheap check first" discipline as script
    # generation's MAX_SCRIPT_SECTIONS. MAX_SCENES_PER_SCRIPT bounds a whole script's
    # scene count; MAX_STORYBOARD_SEGMENTS bounds how many segments (and so provider
    # calls - exactly one per segment, never one per scene) one storyboard generation
    # may cover.
    MAX_SCENES_PER_SCRIPT: int = Field(default=300, ge=1)
    MAX_STORYBOARD_SEGMENTS: int = Field(default=200, ge=1)

    # Mirrors the other STALE_AFTER_SECONDS settings: how long a generate_storyboard job
    # may sit "running" with no worker holding it before it is treated as abandoned by a
    # crashed worker and failed.
    STORYBOARD_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)

    # Video rendering (Phase 10). Every one is optional: an .env written before Phase 10
    # boots unchanged. FFMPEG_BINARY / FFPROBE_BINARY name the executables (looked up on
    # PATH unless given as a full path). RENDER_VIDEO_ENCODER picks the H.264 encoder:
    # ``libx264`` (software; the default) or ``h264_nvenc`` (NVIDIA hardware) - the encoder
    # is part of every render chunk's fingerprint, so switching it re-renders rather than
    # ever mixing two encoders in one video. Choosing an encoder does not settle H.264 or
    # AAC patent licensing, which is a separate business/legal decision
    # (apps/api/README.md, "Video rendering").
    #
    # MAX_RENDER_DURATION_MS is the longest video one render may produce (10 minutes today;
    # the chunked design itself does not depend on it). RENDER_WORK_DIR is where a render's
    # scratch files live ("" = the operating system's temporary directory); before a render
    # is queued the free space there must cover the estimate plus RENDER_MIN_FREE_DISK_MB.
    # RENDER_CONCURRENCY is how many chunks one job renders at once.
    FFMPEG_BINARY: str = "ffmpeg"
    FFPROBE_BINARY: str = "ffprobe"
    RENDER_VIDEO_ENCODER: Literal["libx264", "h264_nvenc"] = "libx264"
    MAX_RENDER_DURATION_MS: int = Field(default=600_000, ge=1_000)
    RENDER_WORK_DIR: str = ""
    RENDER_MIN_FREE_DISK_MB: int = Field(default=1024, ge=0)
    RENDER_CONCURRENCY: int = Field(default=2, ge=1, le=16)

    # Scene transitions (Phase 11). Between two scenes of the same topic the cut is straight;
    # at a topic boundary (and at the start and end of the video) a scene fades in over
    # RENDER_FADE_IN_MS and out over RENDER_FADE_OUT_MS. Milliseconds, 0 to 1500; 0 is a
    # straight cut. The defaults are shorter than the fixed 450 / 300 ms Phase 10 used, to
    # spend fewer frames on a blank or dim screen. Each fade length is part of the
    # fingerprint of the scenes that fade, so changing one re-renders only those scenes.
    RENDER_FADE_IN_MS: int = Field(default=DEFAULT_FADE_IN_MS, ge=0, le=MAX_FADE_MS)
    RENDER_FADE_OUT_MS: int = Field(default=DEFAULT_FADE_OUT_MS, ge=0, le=MAX_FADE_MS)

    # Mirrors the other STALE_AFTER_SECONDS settings: how long a render_video job may sit
    # "running" with no worker holding it before it is treated as abandoned by a crashed
    # worker and failed. Chunks it had already finished are kept and reused by the retry.
    RENDER_STALE_AFTER_SECONDS: int = Field(default=3600, ge=60)

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
