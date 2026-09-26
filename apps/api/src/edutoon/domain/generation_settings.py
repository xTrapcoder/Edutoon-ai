"""Project-level generation settings: the vocabulary, limits and defaults.

A project carries three settings that later phases (scene planning, narration,
rendering) read to decide what to produce:

- ``orientation`` - the shape of the finished video;
- ``target_duration_ms`` - how long the user wants it to run;
- ``language`` - the language it is written and narrated in.

They live on ``projects`` as plain columns, and this module is the one place
that names their allowed values. The vocabulary mirrors the ``projects`` CHECK
constraints (``ck_projects_orientation``, ``ck_projects_target_duration_ms_range``
and ``ck_projects_language``, all from migration 0010) rather than re-deriving
them: the migrations stay the source of truth for what the database accepts, and
these constants are how the rest of the code names those values. The API schemas
build their validation from them, so a request is refused with a 422 before it
can reach a CHECK.

**Duration** is an integer number of milliseconds like every duration in this
codebase (rule 5), so a target compares directly with a script's
``estimated_duration_ms``. It runs from one minute to sixty.

**Language** is an IETF BCP 47 tag drawn from :data:`SUPPORTED_LANGUAGES`, and
``en-IN`` is the default. To support another, add it here, widen
``ck_projects_language`` in a new migration, and give it a writing style in
``providers.llm`` (a test fails until it has one); nothing else names a language.
``en-GB`` stays supported because every project created before ``en-IN`` became the
default holds it. The language is what the script and topic prompts are written
for - the stored value is the only source, never guessed from a user's text.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

# --- vocabulary (mirrors the projects CHECK constraints) ---------------------------

Orientation = Literal["horizontal", "vertical"]
"""The shape of the video: ``horizontal`` is landscape (16:9), ``vertical`` is
portrait (9:16)."""

Language = Literal["en-GB", "en-IN"]
"""The language a project's script and narration are written in."""

ORIENTATION_HORIZONTAL: Final[Orientation] = "horizontal"
ORIENTATION_VERTICAL: Final[Orientation] = "vertical"

LANGUAGE_EN_GB: Final[Language] = "en-GB"
LANGUAGE_EN_IN: Final[Language] = "en-IN"

# Derived from the Literals, so there is one list to edit and the two cannot drift.
ORIENTATIONS: Final[frozenset[str]] = frozenset(get_args(Orientation))
SUPPORTED_LANGUAGES: Final[frozenset[str]] = frozenset(get_args(Language))

# --- limits ------------------------------------------------------------------------

MS_PER_MINUTE: Final = 60_000

MIN_TARGET_DURATION_MS: Final = 1 * MS_PER_MINUTE
"""The shortest video a project can ask for: one minute."""

MAX_TARGET_DURATION_MS: Final = 60 * MS_PER_MINUTE
"""The longest video a project can ask for: sixty minutes."""

# --- defaults ----------------------------------------------------------------------
# What a project gets when its creator says nothing. The column defaults in
# migrations 0010 and 0011 must agree; a test holds them to it.

DEFAULT_ORIENTATION: Final[Orientation] = ORIENTATION_HORIZONTAL
DEFAULT_LANGUAGE: Final[Language] = LANGUAGE_EN_IN
DEFAULT_TARGET_DURATION_MS: Final = 5 * MS_PER_MINUTE
