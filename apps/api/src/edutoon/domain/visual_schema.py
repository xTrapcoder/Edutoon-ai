"""The JSON Schema for a visual specification - pure domain logic (rule 1).

This is what the language model is shown (``providers.llm``'s ``submit_scenes`` tool) so it can
*produce* a visual. It is **generated from the very constants the runtime validator uses**
(``domain.visual``): change a limit there and the schema and the validator change together - a
test rebuilds both from patched constants and checks they still agree. Nothing here is a second
copy of a number.

**What the schema can and cannot say.** It is closed (``additionalProperties: false`` at every
level), bounded (string lengths, item counts, index ranges) and discriminated by ``type``. It
cannot express a rule that compares one part of the value with another. Those rules are enforced
by :func:`edutoon.domain.visual.parse_visual` alone, and are stated to the model in the schema's
own description (:func:`semantic_rules`) - a visual that breaks one is *dropped, never repaired*
(``services.storyboard``), so the model is told exactly what will get it dropped:

- a hierarchy's first node is its root (no ``parent``); every other node's ``parent`` is the
  index of an *earlier* node; at most 3 levels and 5 nodes on one level;
- ``focus`` must be the index of an item that exists;
- ``before_after`` needs exactly two columns;
- at most 400 characters of text in all, plain text only (no emoji or control characters, and
  only characters the video fonts can draw).

Text fields carry ``pattern: "\\S"`` (at least one non-blank character); JSON Schema has no
"not blank" keyword. The schema deliberately contains no coordinate, colour, font, file, URL,
expression or numeric-value field, because the vocabulary has none.
"""

from __future__ import annotations

from typing import Any

from edutoon.domain import visual as v


def _text(max_chars: int, description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "string",
        "minLength": 1,
        "maxLength": max_chars,
        "pattern": "\\S",
    }
    if description:
        schema["description"] = description
    return schema


def _index(highest: int, description: str) -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "maximum": highest, "description": description}


def _node(*, with_parent: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "label": _text(v.LABEL_MAX_CHARS),
        "detail": _text(v.DETAIL_MAX_CHARS),
    }
    if with_parent:
        properties["parent"] = _index(
            v.HIERARCHY_MAX_NODES - 2,
            "Index of an EARLIER node in nodes. Leave out for the first node (the root).",
        )
    return {
        "type": "object",
        "properties": properties,
        "required": ["label"],
        "additionalProperties": False,
    }


def _items(schema: dict[str, Any], low: int, high: int) -> dict[str, Any]:
    return {"type": "array", "minItems": low, "maxItems": high, "items": schema}


def _steps(kind: str, low: int, high: int, what: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"const": kind},
            "title": _text(v.TITLE_MAX_CHARS),
            "nodes": _items(_node(), low, high),
            "focus": _index(high - 1, f"Optional: index of the one {what} to emphasise."),
        },
        "required": ["type", "nodes"],
        "additionalProperties": False,
    }


def _hierarchy() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"const": "hierarchy"},
            "title": _text(v.TITLE_MAX_CHARS),
            "nodes": _items(_node(with_parent=True), v.HIERARCHY_MIN_NODES, v.HIERARCHY_MAX_NODES),
            "focus": _index(v.HIERARCHY_MAX_NODES - 1, "Optional: index of the node to stress."),
        },
        "required": ["type", "nodes"],
        "additionalProperties": False,
    }


def _comparison() -> dict[str, Any]:
    column = {
        "type": "object",
        "properties": {
            "heading": _text(v.HEADING_MAX_CHARS),
            "points": _items(
                _text(v.POINT_MAX_CHARS), v.COMPARISON_MIN_POINTS, v.COMPARISON_MAX_POINTS
            ),
        },
        "required": ["heading", "points"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "type": {"const": "comparison"},
            "title": _text(v.TITLE_MAX_CHARS),
            "relation": {"enum": list(v.RELATIONS)},
            "columns": _items(column, v.COMPARISON_MIN_COLUMNS, v.COMPARISON_MAX_COLUMNS),
        },
        "required": ["type", "relation", "columns"],
        "additionalProperties": False,
    }


def _components() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"const": "labelled_components"},
            "title": _text(v.TITLE_MAX_CHARS),
            "subject": _text(v.SUBJECT_MAX_CHARS, "The whole the parts belong to."),
            "form": {"enum": list(v.FORMS)},
            "parts": _items(_node(), v.COMPONENTS_MIN_PARTS, v.COMPONENTS_MAX_PARTS),
            "focus": _index(v.COMPONENTS_MAX_PARTS - 1, "Optional: index of the part to stress."),
        },
        "required": ["type", "subject", "form", "parts"],
        "additionalProperties": False,
    }


def semantic_rules() -> str:
    """The rules the schema cannot express, in words for the model. Built from the constants."""
    return (
        "Also required (a visual that breaks any of these is dropped, not repaired): in a "
        "hierarchy the first node is the root and has no parent, and every other node's parent "
        "is the index of an EARLIER node, with at most "
        f"{v.HIERARCHY_MAX_LEVELS} levels and {v.HIERARCHY_MAX_NODES_PER_LEVEL} nodes on one "
        "level; focus must be the index of an item that exists; before_after needs exactly 2 "
        f"columns; at most {v.TOTAL_TEXT_MAX_CHARS} characters of text in the whole visual; "
        "plain text only (no emoji or control characters). Every word, number, date and name "
        "must come from the narration."
    )


def build_visual_json_schema() -> dict[str, Any]:
    """The JSON Schema for ``content.illustration.visual``: one of the five forms."""
    return {
        "description": (
            "Optional diagram shown instead of a picture. Choose the form that fits the moment "
            "(process_flow: ordered steps; cycle: steps that loop; hierarchy: a tree; "
            "comparison: 2-3 columns; labelled_components: a whole and its parts). "
            "Structure and words only - no positions, sizes, colours, images or numbers as data. "
            + semantic_rules()
        ),
        "anyOf": [
            _steps("process_flow", v.PROCESS_FLOW_MIN_NODES, v.PROCESS_FLOW_MAX_NODES, "step"),
            _steps("cycle", v.CYCLE_MIN_NODES, v.CYCLE_MAX_NODES, "step"),
            _hierarchy(),
            _comparison(),
            _components(),
        ],
    }
