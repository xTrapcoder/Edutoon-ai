"""The visual JSON Schema handed to the language model, and its agreement with the validator.

The schema (``domain/visual_schema.py``) is generated from the same limit constants as
``domain.visual.parse_visual``. What is proved here, over the corpus in ``visual_corpus.py``:

- **schema-valid <=> validator-accepts** for every valid example and every *structural* invalid
  example (a rule the schema can state): both accept, or both reject;
- for the *semantic* examples (a rule that compares one part of a value with another - a
  ``parent`` that is not earlier, a ``focus`` beyond this list, the total-words budget,
  characters no font can draw...) the schema accepts and the validator rejects. That gap is exact,
  enumerated, and stated to the model in the schema's own description;
- soundness in both directions holds everywhere: whatever the validator accepts the schema
  accepts, and whatever the schema rejects the validator rejects;
- the schema really is *generated*: patch a limit and both change together.
"""

from __future__ import annotations

import json

import pytest

from edutoon.domain import visual as v
from edutoon.domain.visual_schema import build_visual_json_schema, semantic_rules
from tests import json_schema_subset as js
from tests.visual_corpus import invalid_examples, valid_examples

SCHEMA = build_visual_json_schema()


def _validator_accepts(value) -> bool:
    try:
        v.parse_visual(value)
    except v.VisualSpecError:
        return False
    return True


# --- the checker itself is honest --------------------------------------------------------------


def test_the_schema_uses_only_keywords_the_test_checker_implements():
    assert js.keywords_used(SCHEMA) <= js.SUPPORTED


def test_the_checker_refuses_a_keyword_it_does_not_implement():
    with pytest.raises(js.UnsupportedKeywordError):
        js.errors({"oneOf": []}, 1)


@pytest.mark.parametrize(
    ("schema", "instance", "ok"),
    [
        ({"type": "integer"}, 3, True),
        ({"type": "integer"}, True, False),
        ({"type": "integer"}, 3.0, False),
        ({"type": "string", "minLength": 1, "pattern": "\\S"}, " ", False),
        ({"type": "string", "minLength": 1, "pattern": "\\S"}, "a b", True),
        ({"type": "string", "maxLength": 3}, "abcd", False),
        ({"type": "array", "minItems": 2, "maxItems": 2}, [1], False),
        ({"type": "array", "items": {"type": "string"}}, ["a", 1], False),
        ({"const": 1}, True, False),
        ({"enum": ["a", "b"]}, "c", False),
        (
            {"type": "object", "properties": {"a": {}}, "additionalProperties": False},
            {"b": 1},
            False,
        ),
        ({"type": "object", "required": ["a"]}, {}, False),
        ({"type": "integer", "minimum": 0, "maximum": 5}, 6, False),
        ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, [], False),
        ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 4, True),
    ],
)
def test_the_checker_follows_standard_json_schema_semantics(schema, instance, ok):
    assert js.is_valid(schema, instance) is ok


# --- shape -------------------------------------------------------------------------------------


def _object_schemas(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.get("properties", {}).values():
            yield from _object_schemas(value)
        if "items" in schema:
            yield from _object_schemas(schema["items"])
        for alternative in schema.get("anyOf", []):
            yield from _object_schemas(alternative)


def test_every_object_in_the_schema_is_closed():
    objects = list(_object_schemas(SCHEMA))
    assert len(objects) == 10  # the five forms plus their node / column / part objects
    assert all(o.get("additionalProperties") is False for o in objects)


def test_the_schema_offers_exactly_the_five_forms_and_no_chart():
    forms = [alt["properties"]["type"]["const"] for alt in SCHEMA["anyOf"]]
    assert tuple(forms) == v.VISUAL_TYPES
    assert "chart" not in json.dumps(SCHEMA).lower().replace("no charts", "")


def test_the_schema_has_no_place_for_geometry_colour_fonts_paths_urls_code_or_values():
    names: set[str] = set()

    def collect(schema):
        if isinstance(schema, dict):
            names.update(schema.get("properties", {}))
            for value in schema.get("properties", {}).values():
                collect(value)
            collect(schema.get("items"))
            for alternative in schema.get("anyOf", []):
                collect(alternative)

    collect(SCHEMA)
    forbidden = {"x", "y", "width", "height", "colour", "color", "font", "src", "url", "path"}
    forbidden |= {"code", "expression", "style", "value", "values", "coordinates", "position"}
    assert names.isdisjoint(forbidden)
    assert names == {
        "type",
        "title",
        "nodes",
        "focus",
        "label",
        "detail",
        "parent",
        "relation",
        "columns",
        "heading",
        "points",
        "subject",
        "form",
        "parts",
    }


def test_structural_references_are_integer_indices_with_bounds():
    focus = SCHEMA["anyOf"][0]["properties"]["focus"]
    assert focus["type"] == "integer" and focus["minimum"] == 0
    assert focus["maximum"] == v.PROCESS_FLOW_MAX_NODES - 1
    parent = SCHEMA["anyOf"][2]["properties"]["nodes"]["items"]["properties"]["parent"]
    assert parent["type"] == "integer" and parent["maximum"] == v.HIERARCHY_MAX_NODES - 2


def test_the_schema_is_small_enough_to_send_with_every_storyboard_call():
    assert len(json.dumps(SCHEMA)) < 6_000


# --- the equivalence ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spec"), valid_examples(), ids=lambda x: x if isinstance(x, str) else ""
)
def test_every_valid_example_is_accepted_by_both_the_schema_and_the_validator(name, spec):
    assert js.errors(SCHEMA, spec) == []
    assert _validator_accepts(spec)


_STRUCTURAL = [(n, val) for n, c, val in invalid_examples() if c == "structural"]
_SEMANTIC = [(n, val) for n, c, val in invalid_examples() if c == "semantic"]


@pytest.mark.parametrize(("name", "value"), _STRUCTURAL, ids=[n for n, _ in _STRUCTURAL])
def test_a_structural_rule_is_rejected_by_both_the_schema_and_the_validator(name, value):
    assert js.errors(SCHEMA, value) != [], "the schema should reject this"
    assert not _validator_accepts(value), "the validator should reject this"


@pytest.mark.parametrize(("name", "value"), _SEMANTIC, ids=[n for n, _ in _SEMANTIC])
def test_a_semantic_rule_is_the_validators_alone_the_documented_gap(name, value):
    assert js.errors(SCHEMA, value) == [], "the schema cannot state this rule; it should accept"
    assert not _validator_accepts(value), "the validator must still reject it"


def _everything():
    return [s for _n, s in valid_examples()] + [val for _n, _c, val in invalid_examples()]


@pytest.mark.parametrize("value", _everything())
def test_whatever_the_validator_accepts_the_schema_accepts_and_whatever_the_schema_rejects_the_validator_rejects(  # noqa: E501
    value,
):
    schema_ok = js.is_valid(SCHEMA, value)
    validator_ok = _validator_accepts(value)
    if validator_ok:
        assert schema_ok
    if not schema_ok:
        assert not validator_ok


def test_the_corpus_is_large_and_covers_every_rule_category():
    assert len(_STRUCTURAL) > 100
    assert len(_SEMANTIC) >= 20
    assert len(valid_examples()) >= 20


# --- the schema is generated, not copied -------------------------------------------------------


def test_patching_a_limit_changes_the_schema_and_the_validator_together(monkeypatch):
    monkeypatch.setattr(v, "LABEL_MAX_CHARS", 10)
    monkeypatch.setattr(v, "PROCESS_FLOW_MAX_NODES", 4)
    monkeypatch.setattr(v, "TITLE_MAX_CHARS", 30)
    monkeypatch.setattr(v, "COMPARISON_MAX_POINTS", 3)
    schema = build_visual_json_schema()

    flow = schema["anyOf"][0]["properties"]
    assert flow["nodes"]["maxItems"] == 4
    assert flow["nodes"]["items"]["properties"]["label"]["maxLength"] == 10
    assert flow["title"]["maxLength"] == 30
    assert flow["focus"]["maximum"] == 3
    comparison = schema["anyOf"][3]["properties"]["columns"]["items"]["properties"]["points"]
    assert comparison["maxItems"] == 3

    for value in _everything():  # the examples are generated from the (patched) constants too
        schema_ok = js.is_valid(schema, value)
        validator_ok = _validator_accepts(value)
        if validator_ok:
            assert schema_ok
        if not schema_ok:
            assert not validator_ok

    five_steps = {"type": "process_flow", "nodes": [{"label": "a"} for _ in range(5)]}
    long_label = {"type": "process_flow", "nodes": [{"label": "x" * 11}, {"label": "y"}]}
    for value in (five_steps, long_label):
        assert not js.is_valid(schema, value) and not _validator_accepts(value)


def test_the_semantic_rules_are_stated_to_the_model_from_the_same_constants(monkeypatch):
    text = semantic_rules()
    for phrase in ("root", "EARLIER", "focus", "before_after", "exactly 2 columns", "emoji"):
        assert phrase in text
    assert f"{v.HIERARCHY_MAX_LEVELS} levels" in text
    assert f"{v.HIERARCHY_MAX_NODES_PER_LEVEL} nodes" in text
    assert f"{v.TOTAL_TEXT_MAX_CHARS} characters" in text
    assert semantic_rules() in build_visual_json_schema()["description"]
    monkeypatch.setattr(v, "TOTAL_TEXT_MAX_CHARS", 123)
    monkeypatch.setattr(v, "HIERARCHY_MAX_LEVELS", 2)
    assert "123 characters" in semantic_rules() and "2 levels" in semantic_rules()


def test_every_semantic_example_category_is_covered_by_a_stated_rule():
    """The gap is documented: each kind of semantic example maps to a sentence the model reads."""
    stated = semantic_rules()
    covered = {
        "focus-beyond-this-list": "focus must be the index of an item that exists",
        "before-after-needs-exactly-two": "before_after needs exactly 2",
        "too-many-words-in-all": "characters of text",
        "four-levels-deep": "levels",
        "six-on-one-level": "nodes on one level",
        "parent-comes-later": "EARLIER",
        "root-has-a-parent": "first node is the root",
        "child-has-no-parent": "every other node's parent",
        "characters/emoji-in-a-label": "emoji",
    }
    names = " ".join(name for name, _ in _SEMANTIC)
    for key, sentence in covered.items():
        assert key in names, f"the corpus lost the {key!r} example"
        assert sentence in stated
