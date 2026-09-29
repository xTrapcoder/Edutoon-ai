"""The procedural visual specification - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services).

**What a visual is.** A scene built on one of the three illustrated templates (``labelled_diagram``,
``split_text_illustration``, ``full_bleed_illustration``) may carry one *visual* at
``content["illustration"]["visual"]``: a small, closed description of **what** to show - which of
five diagram forms, and its words - never **how** to draw it. There is no coordinate, size, colour,
font, arrow, file, URL or expression anywhere in it; geometry is always computed by the renderer
(``domain.visual_layout``), separately for each orientation. That is what makes it safe for a
language model to produce: it can choose from a vocabulary and fill in text, and nothing it writes
can become a drawing instruction, executable code or an unbounded input.

The five forms (the whole vocabulary - there is no sixth):

- ``process_flow`` - 2-6 ordered steps (``nodes``), joined by arrows in order;
- ``cycle`` - 3-6 steps that loop back to the first;
- ``hierarchy`` - up to 9 nodes in a tree at most 3 levels deep; each node but the root names its
  ``parent`` by index, and a parent always comes *before* its children, so a tree can never
  contain a cycle;
- ``comparison`` - 2-3 ``columns`` each with a ``heading`` and 2-4 ``points``, related as
  ``versus`` or ``before_after`` (exactly two columns);
- ``labelled_components`` - a ``subject`` and 2-6 ``parts``, shown as a ``hub`` with leader lines
  or as stacked ``layers``.

There is deliberately **no chart form**: a bar or a value would be a numeric datum, and numbers in
scene text must be traceable to the narration (``domain.storyboard_provenance``). Every structural
reference here is an integer *index* (``parent``, ``focus``), never a string id, so the provenance
guard - which reads text and numbers - can tell structure from content.

**Never coerced.** :func:`parse_visual` accepts a spec exactly as written or raises
:class:`VisualSpecError`; it trims nothing, truncates nothing and fills nothing in. The caller
(``services.storyboard``) drops a visual that fails - the scene is kept and drawn by the
deterministic fallback - and counts it, so a reviewer is told (Phase 11, ``dropped_visual_count``).

Every limit below is a single named constant shared by this validator, the JSON Schema handed to
the language model (``domain.visual_schema``) and the prompt, so the three cannot drift apart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, get_args

from edutoon.domain.text_glyphs import is_drawable

# --- vocabulary --------------------------------------------------------------------------------

VisualType = Literal["process_flow", "cycle", "hierarchy", "comparison", "labelled_components"]
VISUAL_TYPES: Final[tuple[str, ...]] = get_args(VisualType)

Relation = Literal["versus", "before_after"]
RELATIONS: Final[tuple[str, ...]] = get_args(Relation)

Form = Literal["hub", "layers"]
FORMS: Final[tuple[str, ...]] = get_args(Form)

# The enumerated string fields of a visual and their allowed values. The provenance guard reads
# free text; these are structure, so it skips them (and only them, and only when the value is one
# of the allowed ones - anything else is scanned like any other text).
ENUM_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "type": VISUAL_TYPES,
    "relation": RELATIONS,
    "form": FORMS,
}
# Integer fields that are indices into the visual's own lists, not data.
STRUCTURAL_INDEX_FIELDS: Final[frozenset[str]] = frozenset({"parent", "focus"})

# --- limits (a fixed contract; the tests pin every boundary) -----------------------------------

TITLE_MAX_CHARS: Final = 60
LABEL_MAX_CHARS: Final = 28  # a step, node, part or component name
DETAIL_MAX_CHARS: Final = 60
HEADING_MAX_CHARS: Final = 28  # a comparison column's heading
POINT_MAX_CHARS: Final = 40  # one line under a comparison heading
SUBJECT_MAX_CHARS: Final = 28  # the whole a labelled_components visual is about

PROCESS_FLOW_MIN_NODES: Final = 2
PROCESS_FLOW_MAX_NODES: Final = 6
CYCLE_MIN_NODES: Final = 3
CYCLE_MAX_NODES: Final = 6
HIERARCHY_MIN_NODES: Final = 2
HIERARCHY_MAX_NODES: Final = 9
HIERARCHY_MAX_LEVELS: Final = 3  # the root is level 1
HIERARCHY_MAX_NODES_PER_LEVEL: Final = 5
COMPARISON_MIN_COLUMNS: Final = 2
COMPARISON_MAX_COLUMNS: Final = 3
COMPARISON_MIN_POINTS: Final = 2
COMPARISON_MAX_POINTS: Final = 4
COMPONENTS_MIN_PARTS: Final = 2
COMPONENTS_MAX_PARTS: Final = 6

# One idea per scene: all the words of one visual together, and its serialised size.
TOTAL_TEXT_MAX_CHARS: Final = 400
MAX_SERIALISED_BYTES: Final = 2048

# --- the parsed model --------------------------------------------------------------------------


class VisualSpecError(Exception):
    """A visual specification is not valid. The message names the field and the rule, never the
    offending text; it is for logs and tests, and is not shown to a project's owner."""


@dataclass(frozen=True, slots=True)
class VisualNode:
    label: str
    detail: str | None = None
    parent: int | None = None  # hierarchy only: index of the parent node (always < own index)


@dataclass(frozen=True, slots=True)
class ProcessFlow:
    nodes: tuple[VisualNode, ...]
    title: str | None = None
    focus: int | None = None
    type: str = "process_flow"


@dataclass(frozen=True, slots=True)
class Cycle:
    nodes: tuple[VisualNode, ...]
    title: str | None = None
    focus: int | None = None
    type: str = "cycle"


@dataclass(frozen=True, slots=True)
class Hierarchy:
    nodes: tuple[VisualNode, ...]
    title: str | None = None
    focus: int | None = None
    type: str = "hierarchy"

    def level_of(self, index: int) -> int:
        """0 for the root, 1 for its children, ..."""
        level = 0
        parent = self.nodes[index].parent
        while parent is not None:
            level += 1
            parent = self.nodes[parent].parent
        return level


@dataclass(frozen=True, slots=True)
class ComparisonColumn:
    heading: str
    points: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Comparison:
    columns: tuple[ComparisonColumn, ...]
    relation: str
    title: str | None = None
    type: str = "comparison"


@dataclass(frozen=True, slots=True)
class LabelledComponents:
    subject: str
    parts: tuple[VisualNode, ...]
    form: str
    title: str | None = None
    focus: int | None = None
    type: str = "labelled_components"


Visual = ProcessFlow | Cycle | Hierarchy | Comparison | LabelledComponents

# --- region capacity ---------------------------------------------------------------------------
# The illustrated templates give a visual a region: ``labelled_diagram`` and
# ``full_bleed_illustration`` the whole safe area ("full"), ``split_text_illustration`` about half
# of it ("half", with the text taking the rest). A visual too big for its region is rejected up
# front - at storyboard time, where it is counted and shown to the reviewer - rather than being
# discovered as a silent layout overflow at render time (which still falls back, as a backstop).

Region = Literal["full", "half"]
TEMPLATE_REGION: Final[dict[str, Region]] = {
    "labelled_diagram": "full",
    "full_bleed_illustration": "full",
    "split_text_illustration": "half",
}
HALF_REGION_MAX_NODES: Final = {
    "process_flow": 4,
    "cycle": 4,
    "hierarchy": 5,
    "labelled_components": 4,
}
HALF_REGION_MAX_COLUMNS: Final = 2
HALF_REGION_MAX_POINTS: Final = 3


def fits_region(visual: Visual, region: Region) -> bool:
    """Whether ``visual`` is small enough for ``region`` (see the notes above)."""
    if region == "full":
        return True
    if isinstance(visual, Comparison):
        return (
            len(visual.columns) <= HALF_REGION_MAX_COLUMNS
            and max(len(column.points) for column in visual.columns) <= HALF_REGION_MAX_POINTS
        )
    items = visual.parts if isinstance(visual, LabelledComponents) else visual.nodes
    return len(items) <= HALF_REGION_MAX_NODES[visual.type]


def validate_visual_for_template(template: str, visual: Visual) -> None:
    """Raise :class:`VisualSpecError` if ``visual`` cannot go on ``template`` (a template with no
    illustration slot, or a visual too big for the template's region)."""
    region = TEMPLATE_REGION.get(template)
    if region is None:
        raise VisualSpecError(f"The {template!r} template has no place for a visual.")
    if not fits_region(visual, region):
        raise VisualSpecError(f"This {visual.type} is too large for a {template!r} scene.")


# --- validation --------------------------------------------------------------------------------


class _Budget:
    """Counts the characters of every free-text field of one visual against the total cap."""

    __slots__ = ("used",)

    def __init__(self) -> None:
        self.used = 0

    def spend(self, amount: int, path: str) -> None:
        self.used += amount
        if self.used > TOTAL_TEXT_MAX_CHARS:
            raise VisualSpecError(
                f"{path}: the visual has more than {TOTAL_TEXT_MAX_CHARS} characters of text."
            )


def _shown(key: object) -> str:
    return repr(key)[:30]


def _object(
    raw: object, path: str, *, required: frozenset[str], optional: frozenset[str]
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise VisualSpecError(f"{path} must be an object.")
    unknown = [key for key in raw if key not in required and key not in optional]
    if unknown:
        raise VisualSpecError(f"{path} has an unknown field {_shown(unknown[0])}.")
    missing = sorted(required - set(raw))
    if missing:
        raise VisualSpecError(f"{path} is missing {missing[0]!r}.")
    return raw


def _text(value: object, path: str, *, max_chars: int, budget: _Budget) -> str:
    if not isinstance(value, str):
        raise VisualSpecError(f"{path} must be a string.")
    if not value.strip():
        raise VisualSpecError(f"{path} must not be blank.")
    if len(value) > max_chars:
        raise VisualSpecError(f"{path} must be at most {max_chars} characters.")
    if not is_drawable(value):
        raise VisualSpecError(f"{path} contains a character that cannot be drawn.")
    budget.spend(len(value), path)
    return value


def _optional_text(
    raw: Mapping[str, object], key: str, path: str, *, max_chars: int, budget: _Budget
) -> str | None:
    if key not in raw:
        return None
    return _text(raw[key], f"{path}.{key}", max_chars=max_chars, budget=budget)


def _index(value: object, path: str, *, below: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualSpecError(f"{path} must be an integer.")
    if not 0 <= value < below:
        raise VisualSpecError(f"{path} must be an index of one of its {below} items.")
    return value


def _enum(raw: Mapping[str, object], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or value not in ENUM_FIELDS[key]:
        raise VisualSpecError(f"{path}.{key} must be one of {list(ENUM_FIELDS[key])}.")
    return value


def _list(value: object, path: str, *, min_items: int, max_items: int) -> list[object]:
    if not isinstance(value, list):
        raise VisualSpecError(f"{path} must be a list.")
    if not min_items <= len(value) <= max_items:
        raise VisualSpecError(f"{path} must have between {min_items} and {max_items} items.")
    return value


def _nodes(
    value: object,
    path: str,
    *,
    min_items: int,
    max_items: int,
    budget: _Budget,
    with_parent: bool = False,
) -> tuple[VisualNode, ...]:
    items = _list(value, path, min_items=min_items, max_items=max_items)
    optional = frozenset({"detail", "parent"} if with_parent else {"detail"})
    nodes: list[VisualNode] = []
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        node = _object(item, where, required=frozenset({"label"}), optional=optional)
        label = _text(node["label"], f"{where}.label", max_chars=LABEL_MAX_CHARS, budget=budget)
        detail = _optional_text(node, "detail", where, max_chars=DETAIL_MAX_CHARS, budget=budget)
        parent: int | None = None
        if with_parent:
            if index == 0:
                if "parent" in node:
                    raise VisualSpecError(f"{where} is the root and must not have a parent.")
            else:
                if "parent" not in node:
                    raise VisualSpecError(f"{where} needs a parent.")
                parent = _index(node["parent"], f"{where}.parent", below=index)
        nodes.append(VisualNode(label=label, detail=detail, parent=parent))
    return tuple(nodes)


def _focus(raw: Mapping[str, object], path: str, count: int) -> int | None:
    if "focus" not in raw:
        return None
    return _index(raw["focus"], f"{path}.focus", below=count)


def _check_tree(nodes: tuple[VisualNode, ...], path: str) -> None:
    levels: list[int] = []
    for node in nodes:
        levels.append(0 if node.parent is None else levels[node.parent] + 1)
    if max(levels) + 1 > HIERARCHY_MAX_LEVELS:
        raise VisualSpecError(f"{path} may be at most {HIERARCHY_MAX_LEVELS} levels deep.")
    for level in set(levels):
        if levels.count(level) > HIERARCHY_MAX_NODES_PER_LEVEL:
            raise VisualSpecError(
                f"{path} may have at most {HIERARCHY_MAX_NODES_PER_LEVEL} nodes on one level."
            )


_COMMON_OPTIONAL = frozenset({"title"})


def parse_visual(raw: object, *, path: str = "visual") -> Visual:
    """Validate ``raw`` (a decoded JSON object) and return it as a typed :class:`Visual`.

    Raises :class:`VisualSpecError` for anything that is not exactly a valid visual: an unknown
    field at any level, a wrong type, a count or length outside its bound, a blank or undrawable
    string, an index out of range, a tree that is too deep or wide, or a visual whose words or
    serialised form exceed the total caps. Nothing is repaired.
    """
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise VisualSpecError(f"{path} is not plain JSON.") from exc
    if len(encoded.encode("utf-8")) > MAX_SERIALISED_BYTES:
        raise VisualSpecError(f"{path} is larger than {MAX_SERIALISED_BYTES} bytes.")

    if not isinstance(raw, Mapping):
        raise VisualSpecError(f"{path} must be an object.")
    visual_type = raw.get("type")
    if not isinstance(visual_type, str) or visual_type not in VISUAL_TYPES:
        raise VisualSpecError(f"{path}.type must be one of {list(VISUAL_TYPES)}.")

    budget = _Budget()

    if visual_type in ("process_flow", "cycle"):
        body = _object(
            raw,
            path,
            required=frozenset({"type", "nodes"}),
            optional=_COMMON_OPTIONAL | {"focus"},
        )
        title = _optional_text(body, "title", path, max_chars=TITLE_MAX_CHARS, budget=budget)
        low, high = (
            (PROCESS_FLOW_MIN_NODES, PROCESS_FLOW_MAX_NODES)
            if visual_type == "process_flow"
            else (CYCLE_MIN_NODES, CYCLE_MAX_NODES)
        )
        nodes = _nodes(body["nodes"], f"{path}.nodes", min_items=low, max_items=high, budget=budget)
        focus = _focus(body, path, len(nodes))
        if visual_type == "process_flow":
            return ProcessFlow(nodes=nodes, title=title, focus=focus)
        return Cycle(nodes=nodes, title=title, focus=focus)

    if visual_type == "hierarchy":
        body = _object(
            raw,
            path,
            required=frozenset({"type", "nodes"}),
            optional=_COMMON_OPTIONAL | {"focus"},
        )
        title = _optional_text(body, "title", path, max_chars=TITLE_MAX_CHARS, budget=budget)
        nodes = _nodes(
            body["nodes"],
            f"{path}.nodes",
            min_items=HIERARCHY_MIN_NODES,
            max_items=HIERARCHY_MAX_NODES,
            budget=budget,
            with_parent=True,
        )
        _check_tree(nodes, f"{path}.nodes")
        return Hierarchy(nodes=nodes, title=title, focus=_focus(body, path, len(nodes)))

    if visual_type == "comparison":
        body = _object(
            raw,
            path,
            required=frozenset({"type", "relation", "columns"}),
            optional=_COMMON_OPTIONAL,
        )
        title = _optional_text(body, "title", path, max_chars=TITLE_MAX_CHARS, budget=budget)
        relation = _enum(body, "relation", path)
        items = _list(
            body["columns"],
            f"{path}.columns",
            min_items=COMPARISON_MIN_COLUMNS,
            max_items=COMPARISON_MAX_COLUMNS,
        )
        if relation == "before_after" and len(items) != 2:
            raise VisualSpecError(f"{path}.columns must have exactly 2 items for before_after.")
        columns: list[ComparisonColumn] = []
        for index, item in enumerate(items):
            where = f"{path}.columns[{index}]"
            column = _object(
                item, where, required=frozenset({"heading", "points"}), optional=frozenset()
            )
            heading = _text(
                column["heading"], f"{where}.heading", max_chars=HEADING_MAX_CHARS, budget=budget
            )
            point_items = _list(
                column["points"],
                f"{where}.points",
                min_items=COMPARISON_MIN_POINTS,
                max_items=COMPARISON_MAX_POINTS,
            )
            points = tuple(
                _text(point, f"{where}.points[{n}]", max_chars=POINT_MAX_CHARS, budget=budget)
                for n, point in enumerate(point_items)
            )
            columns.append(ComparisonColumn(heading=heading, points=points))
        return Comparison(columns=tuple(columns), relation=relation, title=title)

    # labelled_components
    body = _object(
        raw,
        path,
        required=frozenset({"type", "subject", "form", "parts"}),
        optional=_COMMON_OPTIONAL | {"focus"},
    )
    title = _optional_text(body, "title", path, max_chars=TITLE_MAX_CHARS, budget=budget)
    subject = _text(body["subject"], f"{path}.subject", max_chars=SUBJECT_MAX_CHARS, budget=budget)
    form = _enum(body, "form", path)
    parts = _nodes(
        body["parts"],
        f"{path}.parts",
        min_items=COMPONENTS_MIN_PARTS,
        max_items=COMPONENTS_MAX_PARTS,
        budget=budget,
    )
    return LabelledComponents(
        subject=subject,
        parts=parts,
        form=form,
        title=title,
        focus=_focus(body, path, len(parts)),
    )


# --- reading a visual out of a scene's content -------------------------------------------------


def raw_visual_of(content: Mapping[str, object]) -> object | None:
    """The unvalidated ``content["illustration"]["visual"]`` of a scene, or ``None`` if the scene
    has none. Never raises."""
    illustration = content.get("illustration")
    if isinstance(illustration, Mapping):
        return illustration.get("visual")
    return None


def visual_of(content: Mapping[str, object]) -> Visual | None:
    """The scene's visual, parsed - or ``None`` when it has none or it is not valid (the caller
    then draws the deterministic fallback). Never raises."""
    raw = raw_visual_of(content)
    if raw is None:
        return None
    try:
        return parse_visual(raw)
    except VisualSpecError:
        return None


def visual_problem(template: str, content: Mapping[str, object]) -> str | None:
    """Why the visual in ``content`` cannot be used on ``template`` - a message safe for logs
    (it names the field and the rule, never the text) - or ``None`` if there is no visual, or it
    is valid and fits."""
    illustration = content.get("illustration")
    if not isinstance(illustration, Mapping) or "visual" not in illustration:
        return None
    try:
        validate_visual_for_template(template, parse_visual(illustration["visual"]))
    except VisualSpecError as exc:
        return str(exc)
    return None


def without_invalid_visual(
    template: str, content: Mapping[str, object]
) -> tuple[dict[str, object], bool]:
    """``content`` with its visual removed - and ``illustration.visual_dropped`` set - if that
    visual is not valid for ``template``; otherwise ``content`` unchanged. The second value says
    whether a visual was dropped.

    A visual is dropped when it fails :func:`parse_visual`, or does not fit the template's region
    (:func:`validate_visual_for_template`). The scene itself is never touched otherwise: the
    illustration's description and labels stay, and it is drawn by the deterministic fallback. A
    ``visual_dropped`` flag the model supplied itself is discarded - only this function sets it.
    """
    result = dict(content)
    illustration = result.get("illustration")
    if not isinstance(illustration, Mapping):
        return result, False
    cleaned = {key: value for key, value in illustration.items() if key != "visual_dropped"}
    if visual_problem(template, {"illustration": cleaned}) is None:
        result["illustration"] = cleaned
        return result, False
    del cleaned["visual"]
    cleaned["visual_dropped"] = True
    result["illustration"] = cleaned
    return result, True
