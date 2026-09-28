"""Phase 9's fixed scene-template vocabulary, slot validation and
orientation-aware layout - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services).

**Templates.** There are exactly six, forming the whole of Phase 9's visual
vocabulary (``TEMPLATES``); nothing here adds a seventh. There is deliberately
no "section recap" template - a recap is represented as an ordinary
``bullet_points`` scene titled "Recap". Three templates
(``title_card``, ``bullet_points``, ``formula_or_code``) carry no illustration
slot and are fully renderable from text alone; the other three
(``labelled_diagram``, ``split_text_illustration``, ``full_bleed_illustration``)
require one bounded :func:`validate_illustration` slot each. No image is ever
generated in this phase - an illustration slot is only a description and a
handful of subject labels; the frontend shows "Illustration pending" for it
and must never pretend the image exists.

**Content is orientation-independent** (spec section 8): the same
``content`` dict is valid for both orientations, and
:func:`validate_scene_content` never looks at orientation. Layout is decided
separately, by :func:`layout_for`: a *deterministic, code-only* lookup of
``(template, orientation)`` to a fixed :class:`LayoutSpec` - the database
never stores layout or renderer coordinates (migration 0015's ``content``
column holds only the validated slot data), and this module is the one place
that vertical genuinely differs, structurally, from horizontal. A vertical
layout is never simply "horizontal, squeezed": see each ``LayoutSpec``'s
``arrangement``, which is a distinct tag per orientation for every template.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, get_args

from edutoon.domain.generation_settings import Orientation

Template = Literal[
    "title_card",
    "bullet_points",
    "labelled_diagram",
    "split_text_illustration",
    "full_bleed_illustration",
    "formula_or_code",
]

TEMPLATES: Final[frozenset[str]] = frozenset(get_args(Template))

# Templates with no illustration slot: fully renderable from text alone.
TEXT_ONLY_TEMPLATES: Final[frozenset[str]] = frozenset(
    {"title_card", "bullet_points", "formula_or_code"}
)

LabelPosition = Literal["top", "left", "right", "bottom", "centre"]
LABEL_POSITIONS: Final[frozenset[str]] = frozenset(get_args(LabelPosition))

FormulaOrCodeKind = Literal["formula", "code"]
FORMULA_OR_CODE_KINDS: Final[frozenset[str]] = frozenset(get_args(FormulaOrCodeKind))

# --- character/count limits (a fixed contract - see the module docstring) ------------

TITLE_MAX_CHARS: Final = 60
SUBTITLE_MAX_CHARS: Final = 100

MIN_BULLETS: Final = 2
MAX_BULLETS: Final = 5
BULLET_MAX_CHARS: Final = 80

MIN_LABELS: Final = 1
MAX_LABELS: Final = 6
LABEL_MAX_CHARS: Final = 30

SPLIT_TEXT_MAX_CHARS: Final = 160
CAPTION_MAX_CHARS: Final = 80

FORMULA_OR_CODE_CONTENT_MAX_CHARS: Final = 200
FORMULA_OR_CODE_LABEL_MAX_CHARS: Final = 40

ILLUSTRATION_DESCRIPTION_MAX_CHARS: Final = 160
ILLUSTRATION_MAX_SUBJECT_LABELS: Final = 3
ILLUSTRATION_SUBJECT_LABEL_MAX_CHARS: Final = 30


class StoryboardContentError(Exception):
    """A scene's ``template`` or ``content`` does not satisfy the template's
    slot contract. Raised by :func:`validate_scene_content` and
    :func:`layout_for`; a caller in ``services.storyboard`` is expected to
    translate this into the LLM output being rejected (never silently
    trimmed to fit), the same "reject, don't coerce" discipline as
    :class:`~edutoon.domain.script_provenance.UnsourcedSegmentError`.
    """


def _require_mapping(content: object, *, what: str = "content") -> Mapping[str, object]:
    if not isinstance(content, Mapping):
        raise StoryboardContentError(f"{what} must be an object.")
    return content


def _require_str(content: Mapping[str, object], key: str, *, max_chars: int) -> str:
    value = content.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StoryboardContentError(f"{key!r} is required and must be a non-empty string.")
    if len(value) > max_chars:
        raise StoryboardContentError(f"{key!r} must be at most {max_chars} characters.")
    return value


def _optional_str(content: Mapping[str, object], key: str, *, max_chars: int) -> str | None:
    value = content.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoryboardContentError(f"{key!r} must be a string when given.")
    if len(value) > max_chars:
        raise StoryboardContentError(f"{key!r} must be at most {max_chars} characters.")
    return value


def validate_illustration(data: object) -> None:
    """Validate one bounded illustration specification (spec section 9): a
    required ``description`` (<= 160 chars) and an optional list of up to 3
    ``subject_labels`` (<= 30 chars each). No image, url or icon vocabulary
    field is ever accepted here - Phase 9 generates no image, and a caller
    must never invent one.
    """
    mapping = _require_mapping(data, what="illustration")
    _require_str(mapping, "description", max_chars=ILLUSTRATION_DESCRIPTION_MAX_CHARS)

    subject_labels = mapping.get("subject_labels", [])
    if subject_labels is None:
        subject_labels = []
    if not isinstance(subject_labels, list) or not all(
        isinstance(label, str) for label in subject_labels
    ):
        raise StoryboardContentError("illustration.subject_labels must be a list of strings.")
    if len(subject_labels) > ILLUSTRATION_MAX_SUBJECT_LABELS:
        raise StoryboardContentError(
            f"illustration.subject_labels allows at most {ILLUSTRATION_MAX_SUBJECT_LABELS} items."
        )
    for label in subject_labels:
        if len(label) > ILLUSTRATION_SUBJECT_LABEL_MAX_CHARS:
            raise StoryboardContentError(
                "Each illustration.subject_labels entry must be at most "
                f"{ILLUSTRATION_SUBJECT_LABEL_MAX_CHARS} characters."
            )


def _validate_title_card(content: Mapping[str, object]) -> None:
    _require_str(content, "title", max_chars=TITLE_MAX_CHARS)
    _optional_str(content, "subtitle", max_chars=SUBTITLE_MAX_CHARS)


def _validate_bullet_points(content: Mapping[str, object]) -> None:
    _optional_str(content, "title", max_chars=TITLE_MAX_CHARS)
    bullets = content.get("bullets")
    if not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets):
        raise StoryboardContentError("'bullets' is required and must be a list of strings.")
    if not (MIN_BULLETS <= len(bullets) <= MAX_BULLETS):
        raise StoryboardContentError(
            f"'bullets' must have between {MIN_BULLETS} and {MAX_BULLETS} items."
        )
    for bullet in bullets:
        if not bullet.strip():
            raise StoryboardContentError("Each bullet must be a non-empty string.")
        if len(bullet) > BULLET_MAX_CHARS:
            raise StoryboardContentError(
                f"Each bullet must be at most {BULLET_MAX_CHARS} characters."
            )


def _validate_labelled_diagram(content: Mapping[str, object]) -> None:
    validate_illustration(content.get("illustration"))

    labels = content.get("labels")
    if not isinstance(labels, list):
        raise StoryboardContentError("'labels' is required and must be a list.")
    if not (MIN_LABELS <= len(labels) <= MAX_LABELS):
        raise StoryboardContentError(
            f"'labels' must have between {MIN_LABELS} and {MAX_LABELS} items."
        )
    for label in labels:
        if not isinstance(label, Mapping):
            raise StoryboardContentError("Each label must be an object.")
        text = label.get("text")
        if not isinstance(text, str) or not text.strip():
            raise StoryboardContentError("Each label's 'text' is required and must be non-empty.")
        if len(text) > LABEL_MAX_CHARS:
            raise StoryboardContentError(
                f"Each label's text must be at most {LABEL_MAX_CHARS} characters."
            )
        position = label.get("position")
        if position not in LABEL_POSITIONS:
            raise StoryboardContentError(
                f"Each label's 'position' must be one of {sorted(LABEL_POSITIONS)}."
            )


def _validate_split_text_illustration(content: Mapping[str, object]) -> None:
    _require_str(content, "text", max_chars=SPLIT_TEXT_MAX_CHARS)
    validate_illustration(content.get("illustration"))


def _validate_full_bleed_illustration(content: Mapping[str, object]) -> None:
    validate_illustration(content.get("illustration"))
    _optional_str(content, "caption", max_chars=CAPTION_MAX_CHARS)


def _validate_formula_or_code(content: Mapping[str, object]) -> None:
    _require_str(content, "content", max_chars=FORMULA_OR_CODE_CONTENT_MAX_CHARS)
    _optional_str(content, "label", max_chars=FORMULA_OR_CODE_LABEL_MAX_CHARS)
    kind = content.get("kind")
    if kind not in FORMULA_OR_CODE_KINDS:
        raise StoryboardContentError(f"'kind' must be one of {sorted(FORMULA_OR_CODE_KINDS)}.")


_TEMPLATE_VALIDATORS: dict[str, object] = {
    "title_card": _validate_title_card,
    "bullet_points": _validate_bullet_points,
    "labelled_diagram": _validate_labelled_diagram,
    "split_text_illustration": _validate_split_text_illustration,
    "full_bleed_illustration": _validate_full_bleed_illustration,
    "formula_or_code": _validate_formula_or_code,
}


def validate_scene_content(template: str, content: object) -> None:
    """Validate ``content`` against ``template``'s slot contract.

    Raises :class:`StoryboardContentError` for an unknown template, a
    non-object ``content``, a missing required slot, an out-of-range item
    count, an over-length string, or an illustration slot on a template that
    disallows one. Content is orientation-independent (spec section 8): this
    never looks at orientation - see :func:`layout_for` for that.
    """
    validator = _TEMPLATE_VALIDATORS.get(template)
    if validator is None:
        raise StoryboardContentError(f"Unknown template {template!r}.")
    mapping = _require_mapping(content)
    validator(mapping)  # type: ignore[operator]


# --- orientation-aware layout (deterministic code, never stored) ---------------------


@dataclass(frozen=True, slots=True)
class LayoutSpec:
    """A deterministic description of how ``template`` is arranged for
    ``orientation`` - never renderer pixel coordinates (those belong to a
    future rendering phase), and never persisted: a caller asks for this at
    read/render time, from ``(template, orientation)`` alone. ``arrangement``
    is a distinct tag per orientation for every template, so a mutation that
    swapped a template's horizontal and vertical entries is caught by a
    dedicated equality test (spec section 24).
    """

    template: Template
    orientation: Orientation
    arrangement: str
    description: str


_LAYOUTS: Final[dict[tuple[str, str], LayoutSpec]] = {
    ("title_card", "horizontal"): LayoutSpec(
        template="title_card",
        orientation="horizontal",
        arrangement="title_card_centred_wide_margins",
        description="Centred, with generous horizontal margins either side of the title.",
    ),
    ("title_card", "vertical"): LayoutSpec(
        template="title_card",
        orientation="vertical",
        arrangement="title_card_centred_stacked",
        description="Centred; the title may wrap across lines, with the subtitle stacked below it.",
    ),
    ("bullet_points", "horizontal"): LayoutSpec(
        template="bullet_points",
        orientation="horizontal",
        arrangement="bullets_title_top_content_area",
        description="Title (if any) at the top; bullets fill the primary content area below it.",
    ),
    ("bullet_points", "vertical"): LayoutSpec(
        template="bullet_points",
        orientation="vertical",
        arrangement="bullets_full_width_wide_spacing",
        description="Bullets run the full width with increased line spacing between them.",
    ),
    ("labelled_diagram", "horizontal"): LayoutSpec(
        template="labelled_diagram",
        orientation="horizontal",
        arrangement="diagram_centred_labels_around",
        description="The diagram sits centred, with labels arranged around it on every side.",
    ),
    ("labelled_diagram", "vertical"): LayoutSpec(
        template="labelled_diagram",
        orientation="vertical",
        arrangement="diagram_above_labels_below",
        description="The diagram sits above; labels stack below it, avoiding a side-label layout.",
    ),
    ("split_text_illustration", "horizontal"): LayoutSpec(
        template="split_text_illustration",
        orientation="horizontal",
        arrangement="text_and_illustration_side_by_side",
        description="Text and illustration sit side by side.",
    ),
    ("split_text_illustration", "vertical"): LayoutSpec(
        template="split_text_illustration",
        orientation="vertical",
        arrangement="illustration_top_text_below",
        description="The illustration sits on top, with the text stacked below it.",
    ),
    ("full_bleed_illustration", "horizontal"): LayoutSpec(
        template="full_bleed_illustration",
        orientation="horizontal",
        arrangement="illustration_full_bleed_caption_safe_area",
        description="The illustration fills the frame; a caption, if any, sits in a safe area.",
    ),
    ("full_bleed_illustration", "vertical"): LayoutSpec(
        template="full_bleed_illustration",
        orientation="vertical",
        arrangement="illustration_full_bleed_caption_vertical_safe_area",
        description=(
            "The illustration fills the frame; a caption, if any, sits in a "
            "vertical-safe caption placement near the bottom."
        ),
    ),
    ("formula_or_code", "horizontal"): LayoutSpec(
        template="formula_or_code",
        orientation="horizontal",
        arrangement="formula_or_code_centred_wide_margins",
        description="Centred, with generous margins either side.",
    ),
    ("formula_or_code", "vertical"): LayoutSpec(
        template="formula_or_code",
        orientation="vertical",
        arrangement="formula_or_code_centred_safe_side_margins",
        description="Centred, with narrower safe side margins suited to a portrait frame.",
    ),
}


def layout_for(template: str, orientation: str) -> LayoutSpec:
    """The deterministic layout for ``(template, orientation)``.

    Raises :class:`StoryboardContentError` for an unknown template or
    orientation - the same "reject, don't guess" discipline as
    :func:`validate_scene_content`.
    """
    key = (template, orientation)
    layout = _LAYOUTS.get(key)
    if layout is None:
        if template not in TEMPLATES:
            raise StoryboardContentError(f"Unknown template {template!r}.")
        raise StoryboardContentError(f"Unknown orientation {orientation!r}.")
    return layout


def slot_field_names(template: str) -> Sequence[str]:
    """The field names ``template``'s ``content`` may carry - documentation
    and test support only; :func:`validate_scene_content` is the actual
    contract enforcement.
    """
    return {
        "title_card": ("title", "subtitle"),
        "bullet_points": ("title", "bullets"),
        "labelled_diagram": ("illustration", "labels"),
        "split_text_illustration": ("text", "illustration"),
        "full_bleed_illustration": ("illustration", "caption"),
        "formula_or_code": ("content", "label", "kind"),
    }[template]
