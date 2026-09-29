"""A systematic corpus of visual specifications, generated from the domain's own limit constants.

Shared by ``test_domain_visual.py`` (the validator) and ``test_visual_schema.py`` (the JSON Schema
handed to the language model), so the two are checked against exactly the same examples.

Every invalid example carries a *category*:

- ``structural`` - a rule the JSON Schema can express (unknown key, wrong type, missing field,
  a count or length outside its bound, a bad enum value, an index outside its absolute range).
  The schema and the validator must **both** reject it.
- ``semantic`` - a rule that depends on other parts of the same value (a ``parent`` that does not
  come before its child, a ``focus`` beyond *this* list, a tree too deep, the total-words budget,
  ``before_after`` needing exactly two columns, characters no font can draw). JSON Schema cannot
  say these, so the schema accepts them and the validator alone rejects them - the documented,
  enumerated gap.
"""

from __future__ import annotations

import copy
from typing import Any

from edutoon.domain import visual as v

Spec = dict[str, Any]


def text(length: int, seed: str = "a") -> str:
    """A string of exactly ``length`` characters that is not blank."""
    return (seed * length)[:length]


def node(label: str = "Step", detail: str | None = None, parent: int | None = None) -> Spec:
    out: Spec = {"label": label}
    if detail is not None:
        out["detail"] = detail
    if parent is not None:
        out["parent"] = parent
    return out


def nodes(count: int, *, label_len: int = 6, detail_every: int = 0, detail_len: int = 20) -> list:
    return [
        node(
            text(label_len, chr(ord("a") + i % 26)),
            text(detail_len, "d") if detail_every and i % detail_every == 0 else None,
        )
        for i in range(count)
    ]


def flow(count: int = 3, **extra: Any) -> Spec:
    return {"type": "process_flow", "nodes": nodes(count), **extra}


def cycle(count: int = 4, **extra: Any) -> Spec:
    return {"type": "cycle", "nodes": nodes(count), **extra}


def tree(shape: list[int | None], **extra: Any) -> Spec:
    """A hierarchy whose i-th node has parent ``shape[i]`` (``None`` for the root)."""
    return {
        "type": "hierarchy",
        "nodes": [
            node(text(5, chr(ord("a") + i)), parent=parent) for i, parent in enumerate(shape)
        ],
        **extra,
    }


def comparison(columns: int = 2, points: int = 2, relation: str = "versus", **extra: Any) -> Spec:
    return {
        "type": "comparison",
        "relation": relation,
        "columns": [
            {
                "heading": text(8, chr(ord("A") + c)),
                "points": [text(12, chr(ord("a") + p)) for p in range(points)],
            }
            for c in range(columns)
        ],
        **extra,
    }


def components(parts: int = 3, form: str = "hub", **extra: Any) -> Spec:
    return {
        "type": "labelled_components",
        "subject": "The whole",
        "form": form,
        "parts": nodes(parts),
        **extra,
    }


# --- valid -------------------------------------------------------------------------------------


def valid_examples() -> list[tuple[str, Spec]]:
    biggest_flow = {
        "type": "process_flow",
        "title": text(v.TITLE_MAX_CHARS, "T"),
        "focus": v.PROCESS_FLOW_MAX_NODES - 1,
        "nodes": [
            node(text(v.LABEL_MAX_CHARS, "n"), text(v.DETAIL_MAX_CHARS, "d") if i < 2 else None)
            for i in range(v.PROCESS_FLOW_MAX_NODES)
        ],
    }
    widest_tree = tree([None, 0, 0, 0, 0, 1, 2, 3, 4])  # 1 + 4 + 4 nodes, three levels
    return [
        ("flow-min", flow(v.PROCESS_FLOW_MIN_NODES)),
        ("flow-max-labels-and-details", biggest_flow),
        ("flow-titled-with-focus", flow(4, title="How it works", focus=0)),
        ("flow-focus-last", flow(4, focus=3)),
        ("cycle-min", cycle(v.CYCLE_MIN_NODES)),
        ("cycle-max", cycle(v.CYCLE_MAX_NODES)),
        ("cycle-with-focus", cycle(4, focus=2)),
        ("tree-min", tree([None, 0])),
        ("tree-chain-three-levels", tree([None, 0, 1])),
        ("tree-widest", widest_tree),
        ("tree-focus", tree([None, 0, 0], focus=2)),
        ("comparison-versus-two", comparison(2, 2)),
        ("comparison-versus-three", comparison(3, 3)),
        ("comparison-before-after", comparison(2, 4, "before_after")),
        ("comparison-titled", comparison(2, 2, title="Old and new")),
        ("components-hub-min", components(v.COMPONENTS_MIN_PARTS)),
        ("components-hub-max", components(v.COMPONENTS_MAX_PARTS)),
        ("components-layers", components(4, "layers")),
        ("components-focus", components(3, focus=1)),
        (
            "components-subject-max",
            {**components(2), "subject": text(v.SUBJECT_MAX_CHARS, "S")},
        ),
    ]


# --- invalid -----------------------------------------------------------------------------------


def _with(spec: Spec, **changes: Any) -> Spec:
    out = copy.deepcopy(spec)
    for key, value in changes.items():
        if value is _DELETE:
            out.pop(key, None)
        else:
            out[key] = value
    return out


_DELETE = object()


def _node_field(spec: Spec, key: str, field: str, value: Any, index: int = 0) -> Spec:
    out = copy.deepcopy(spec)
    out[key][index][field] = value
    return out


def invalid_examples() -> list[tuple[str, str, Any]]:
    """``(name, category, value)`` - see the module docstring for the categories."""
    out: list[tuple[str, str, Any]] = []

    def add(name: str, category: str, value: Any) -> None:
        out.append((name, category, value))

    # not a visual at all
    for name, value in [
        ("a-list", []),
        ("a-string", "process_flow"),
        ("none", None),
        ("a-number", 7),
    ]:
        add(f"not-an-object/{name}", "structural", value)
    add("no-type", "structural", {"nodes": nodes(3)})
    add("unknown-type", "structural", {"type": "value_chart", "nodes": nodes(3)})
    add("type-not-a-string", "structural", {"type": 3, "nodes": nodes(3)})
    add("type-of-another-case", "structural", {"type": "Process_Flow", "nodes": nodes(3)})

    bases = {
        "process_flow": flow(3),
        "cycle": cycle(4),
        "hierarchy": tree([None, 0, 0]),
        "comparison": comparison(),
        "labelled_components": components(),
    }
    for kind, base in bases.items():
        add(f"{kind}/unknown-top-level-key", "structural", _with(base, colour="red"))
        add(f"{kind}/title-too-long", "structural", _with(base, title=text(v.TITLE_MAX_CHARS + 1)))
        add(f"{kind}/title-not-a-string", "structural", _with(base, title=5))
        add(f"{kind}/title-blank", "structural", _with(base, title="   "))
        add(f"{kind}/title-empty", "structural", _with(base, title=""))
        for coordinate in ("x", "y", "width", "colour", "font", "url", "src", "code"):
            add(f"{kind}/no-{coordinate}-field", "structural", _with(base, **{coordinate: 1}))

    # process_flow and cycle share the nodes shape
    for kind, low, high in (
        ("process_flow", v.PROCESS_FLOW_MIN_NODES, v.PROCESS_FLOW_MAX_NODES),
        ("cycle", v.CYCLE_MIN_NODES, v.CYCLE_MAX_NODES),
    ):
        base = bases[kind]
        add(f"{kind}/too-few-nodes", "structural", _with(base, nodes=nodes(low - 1)))
        add(f"{kind}/too-many-nodes", "structural", _with(base, nodes=nodes(high + 1)))
        add(f"{kind}/no-nodes-key", "structural", _with(base, nodes=_DELETE))
        add(f"{kind}/nodes-not-a-list", "structural", _with(base, nodes={"label": "a"}))
        add(f"{kind}/a-node-is-a-string", "structural", _with(base, nodes=["a", "b", "c", "d"]))
        add(f"{kind}/node-without-label", "structural", _with(base, nodes=[{}, {}, {}, {}]))
        add(f"{kind}/node-unknown-key", "structural", _node_field(base, "nodes", "icon", "leaf"))
        add(f"{kind}/node-has-parent", "structural", _node_field(base, "nodes", "parent", 0))
        add(
            f"{kind}/label-too-long",
            "structural",
            _node_field(base, "nodes", "label", text(v.LABEL_MAX_CHARS + 1)),
        )
        add(f"{kind}/label-blank", "structural", _node_field(base, "nodes", "label", "  "))
        add(f"{kind}/label-empty", "structural", _node_field(base, "nodes", "label", ""))
        add(f"{kind}/label-a-number", "structural", _node_field(base, "nodes", "label", 5))
        add(
            f"{kind}/detail-too-long",
            "structural",
            _node_field(base, "nodes", "detail", text(v.DETAIL_MAX_CHARS + 1)),
        )
        add(f"{kind}/detail-a-list", "structural", _node_field(base, "nodes", "detail", ["x"]))
        add(f"{kind}/focus-negative", "structural", _with(base, focus=-1))
        add(f"{kind}/focus-a-string", "structural", _with(base, focus="0"))
        add(f"{kind}/focus-a-bool", "structural", _with(base, focus=True))
        add(f"{kind}/focus-a-float", "structural", _with(base, focus=1.0))
        add(f"{kind}/focus-beyond-any-list", "structural", _with(base, focus=high))
        add(
            f"{kind}/focus-beyond-this-list",
            "semantic",
            _with(_with(base, nodes=nodes(low)), focus=low),
        )

    # hierarchy
    tree_base = bases["hierarchy"]
    add("hierarchy/too-few-nodes", "structural", tree([None]))
    add("hierarchy/too-many-nodes", "structural", tree([None, 0, 0, 0, 0, 1, 2, 3, 4, 4]))
    add(
        "hierarchy/parent-a-string", "structural", _node_field(tree_base, "nodes", "parent", "0", 1)
    )
    add("hierarchy/parent-a-bool", "structural", _node_field(tree_base, "nodes", "parent", True, 1))
    add("hierarchy/parent-negative", "structural", _node_field(tree_base, "nodes", "parent", -1, 1))
    add("hierarchy/parent-a-float", "structural", _node_field(tree_base, "nodes", "parent", 0.0, 1))
    add("hierarchy/root-has-a-parent", "semantic", _node_field(tree_base, "nodes", "parent", 0, 0))
    add("hierarchy/child-has-no-parent", "semantic", tree([None, 0, None]))
    add("hierarchy/parent-is-itself", "semantic", tree([None, 1, 0]))
    add("hierarchy/parent-comes-later", "semantic", tree([None, 2, 0]))
    add("hierarchy/parent-out-of-range", "semantic", tree([None, 0, 5]))
    add("hierarchy/four-levels-deep", "semantic", tree([None, 0, 1, 2]))
    add("hierarchy/six-on-one-level", "semantic", tree([None, 0, 0, 0, 0, 0, 0]))
    add("hierarchy/focus-beyond-this-list", "semantic", _with(tree_base, focus=5))

    # comparison
    cmp_base = bases["comparison"]
    add("comparison/no-relation", "structural", _with(cmp_base, relation=_DELETE))
    add("comparison/unknown-relation", "structural", _with(cmp_base, relation="against"))
    add("comparison/relation-a-number", "structural", _with(cmp_base, relation=1))
    add(
        "comparison/too-few-columns",
        "structural",
        _with(cmp_base, columns=comparison(1)["columns"]),
    )
    add(
        "comparison/too-many-columns",
        "structural",
        _with(cmp_base, columns=comparison(4)["columns"]),
    )
    add("comparison/no-columns-key", "structural", _with(cmp_base, columns=_DELETE))
    add("comparison/columns-not-a-list", "structural", _with(cmp_base, columns="a"))
    add(
        "comparison/too-few-points",
        "structural",
        _with(cmp_base, columns=comparison(2, 1)["columns"]),
    )
    add(
        "comparison/too-many-points",
        "structural",
        _with(cmp_base, columns=comparison(2, 5)["columns"]),
    )
    add("comparison/column-unknown-key", "structural", _column(cmp_base, "colour", "red"))
    add("comparison/column-no-heading", "structural", _column(cmp_base, "heading", _DELETE))
    add("comparison/column-no-points", "structural", _column(cmp_base, "points", _DELETE))
    add("comparison/heading-too-long", "structural", _column(cmp_base, "heading", text(29)))
    add("comparison/heading-blank", "structural", _column(cmp_base, "heading", " "))
    add("comparison/points-not-a-list", "structural", _column(cmp_base, "points", "a"))
    add(
        "comparison/point-too-long",
        "structural",
        _column(cmp_base, "points", [text(v.POINT_MAX_CHARS + 1), "ok"]),
    )
    add("comparison/point-a-number", "structural", _column(cmp_base, "points", [3, "ok"]))
    add("comparison/point-blank", "structural", _column(cmp_base, "points", ["  ", "ok"]))
    add("comparison/has-focus", "structural", _with(cmp_base, focus=0))
    add(
        "comparison/before-after-needs-exactly-two",
        "semantic",
        comparison(3, 2, "before_after"),
    )

    # labelled_components
    comp_base = bases["labelled_components"]
    add("components/no-subject", "structural", _with(comp_base, subject=_DELETE))
    add("components/no-form", "structural", _with(comp_base, form=_DELETE))
    add("components/unknown-form", "structural", _with(comp_base, form="ring"))
    add("components/no-parts", "structural", _with(comp_base, parts=_DELETE))
    add("components/subject-too-long", "structural", _with(comp_base, subject=text(29)))
    add("components/subject-blank", "structural", _with(comp_base, subject=" "))
    add("components/too-few-parts", "structural", _with(comp_base, parts=nodes(1)))
    add("components/too-many-parts", "structural", _with(comp_base, parts=nodes(7)))
    add("components/part-has-parent", "structural", _node_field(comp_base, "parts", "parent", 0))
    add(
        "components/part-label-too-long",
        "structural",
        _node_field(comp_base, "parts", "label", text(29)),
    )
    add("components/focus-beyond-any-list", "structural", _with(comp_base, focus=6))
    add("components/focus-beyond-this-list", "semantic", _with(comp_base, focus=3))

    # cross-field and character rules the schema cannot state
    over_budget = {
        "type": "process_flow",
        "nodes": [node(text(28, "q"), text(60, "w")) for _ in range(6)],  # 528 characters
    }
    add("budget/too-many-words-in-all", "semantic", over_budget)
    for name, char in [
        ("emoji", "\U0001f600"),
        ("newline", "\n"),
        ("tab", "\t"),
        ("bidi-override", "‮"),
        ("zero-width", "​"),
        ("nul", "\x00"),
        ("private-use", ""),
    ]:
        add(
            f"characters/{name}-in-a-label",
            "semantic",
            _node_field(flow(3), "nodes", "label", f"a{char}b"),
        )
    return out


def _column(spec: Spec, field: str, value: Any) -> Spec:
    out = copy.deepcopy(spec)
    if value is _DELETE:
        out["columns"][0].pop(field, None)
    else:
        out["columns"][0][field] = value
    return out
