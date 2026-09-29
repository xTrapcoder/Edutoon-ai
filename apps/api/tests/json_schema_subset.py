"""A small JSON Schema checker for the tests - just the keywords the visual schema uses.

The project has no ``jsonschema`` dependency and this does not add one. To keep a home-made
checker honest it **raises on any keyword it does not implement**, so a schema that starts using a
new keyword cannot be silently under-checked; ``keywords_used`` lets a test assert the exact
vocabulary a schema relies on.

Implemented (with the standard semantics): ``type`` (string, integer, object, array, number,
boolean, null), ``properties``, ``required``, ``additionalProperties`` (``false`` only), ``enum``,
``const``, ``minLength``, ``maxLength``, ``pattern`` (``re.search``), ``minItems``, ``maxItems``,
``items`` (one schema), ``minimum``, ``maximum``, ``anyOf``; and ``description`` as an annotation.
As in JSON Schema, a keyword about strings/arrays/objects/numbers applies only to an instance of
that type; a boolean is never an integer.
"""

from __future__ import annotations

import re
from typing import Any

SUPPORTED = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "items",
        "minimum",
        "maximum",
        "anyOf",
        "description",
    }
)


class UnsupportedKeywordError(Exception):
    pass


def keywords_used(schema: Any) -> set[str]:
    """Every schema keyword used anywhere in ``schema`` (property names are not keywords)."""
    used: set[str] = set()
    if not isinstance(schema, dict):
        return used
    for keyword, value in schema.items():
        used.add(keyword)
        if keyword == "properties":
            for sub in value.values():
                used |= keywords_used(sub)
        elif keyword == "items":
            used |= keywords_used(value)
        elif keyword == "anyOf":
            for sub in value:
                used |= keywords_used(sub)
    return used


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _is_type(instance: Any, name: str) -> bool:
    if name == "string":
        return isinstance(instance, str)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, int | float) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "null":
        return instance is None
    raise UnsupportedKeywordError(f"type {name!r}")


def errors(schema: dict[str, Any], instance: Any, path: str = "$") -> list[str]:
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise UnsupportedKeywordError(sorted(unknown)[0])
    found: list[str] = []

    if "type" in schema:
        wanted = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, name) for name in wanted):
            found.append(f"{path}: not of type {wanted}")
    if "const" in schema and not _same(instance, schema["const"]):
        found.append(f"{path}: not the constant")
    if "enum" in schema and not any(_same(instance, option) for option in schema["enum"]):
        found.append(f"{path}: not one of the allowed values")
    if "anyOf" in schema and not any(not errors(sub, instance, path) for sub in schema["anyOf"]):
        found.append(f"{path}: matches none of the alternatives")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            found.append(f"{path}: too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            found.append(f"{path}: too long")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            found.append(f"{path}: does not match the pattern")
    if isinstance(instance, int | float) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            found.append(f"{path}: below the minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            found.append(f"{path}: above the maximum")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            found.append(f"{path}: too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            found.append(f"{path}: too many items")
        if "items" in schema:
            for index, item in enumerate(instance):
                found.extend(errors(schema["items"], item, f"{path}[{index}]"))
    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                found.append(f"{path}: missing {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                found.extend(errors(properties[name], value, f"{path}.{name}"))
            elif schema.get("additionalProperties", True) is False:
                found.append(f"{path}: unknown field {name!r}")
    return found


def is_valid(schema: dict[str, Any], instance: Any) -> bool:
    return not errors(schema, instance)
