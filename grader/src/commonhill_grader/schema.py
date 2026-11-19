"""Checking a worker's outputs against a JSON Schema: the subset below, enough to describe an
output exactly, in the standard library.

Supported keywords: `type` (a name or a list of names: null, boolean, integer, number, string,
array, object), `enum`, `const`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`,
`minLength`, `maxLength`, `items`, `minItems`, `maxItems`, `properties`, `required`,
`additionalProperties` (true, false or a schema), plus `title` and `description`. The schema is
checked when the Checker is made: any other keyword, or a keyword with a value of the wrong type
or shape, is a ValueError naming its path, so a typo can't silently accept everything.

JSON semantics: booleans are not numbers, an integer is a number without a fractional part, and
numbers are equal when mathematically equal (1 and 1.0 match the same `enum`/`const`; `true`
matches neither). Integers stay integers: bounds compare exactly, at any size.

Cost: a check is linear in the size of the value. `enum` and `const` are compiled once: the
options that fail the rest of their node's schema (descendants included) are dropped, and the
rest kept as a set of canonical encodings. A node with `enum` or `const` is then decided by one
encoding of the value and a lookup, without descending into it, so no part of an output is
encoded twice, however deeply such nodes nest. The Client checks outputs of a response it has
already capped in size (`max_bytes`).
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import cast

TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})
BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
LENGTHS = ("minLength", "maxLength", "minItems", "maxItems")
KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "items",
        "properties",
        "required",
        "additionalProperties",
        "title",
        "description",
    }
    | set(BOUNDS)
    | set(LENGTHS)
)


def canonical(value: object) -> str:
    """One encoding per JSON value, equal exactly when the values are equal by JSON Schema's
    rules: object keys sorted, integral numbers written as integers. ValueError for anything that
    isn't a JSON value (including non-finite numbers)."""
    out: list[str] = []
    _encode(value, out)
    return "".join(out)


def _encode(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("not a finite number")
        out.append(str(int(value)) if value.is_integer() else repr(value))
    elif isinstance(value, str):
        out.append(json.dumps(value))
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(cast(list[object], value)):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    elif isinstance(value, dict):
        fields = cast(dict[object, object], value)
        if not all(isinstance(key, str) for key in fields):
            raise ValueError("object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(cast(dict[str, object], fields))):
            if i:
                out.append(",")
            out.append(json.dumps(key) + ":")
            _encode(fields[key], out)
        out.append("}")
    else:
        raise ValueError(f"not a JSON value: {type(value).__name__}")


def _type_of(value: object) -> set[str]:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number", "integer"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    if isinstance(value, dict):
        return {"object"}
    return set()


@dataclass(frozen=True)
class _Node:
    types: frozenset[str] | None = None
    allowed: frozenset[str] | None = None  # canonical encodings from enum and const
    bounds: tuple[tuple[str, int | float], ...] = ()
    lengths: dict[str, int] = field(default_factory=dict[str, int])
    items: "_Node | None" = None
    properties: dict[str, "_Node"] = field(default_factory=dict[str, "_Node"])
    required: tuple[str, ...] = ()
    additional: "bool | _Node" = True


def _compile(schema: object, path: str) -> _Node:
    if not isinstance(schema, Mapping):
        raise ValueError(f"schema at {path} must be an object")
    schema = cast(Mapping[str, object], schema)
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError(f"schema at {path}: unsupported keywords {sorted(unknown)}")

    def bad(keyword: str, wanted: str) -> ValueError:
        return ValueError(f"schema at {path}: {keyword} must be {wanted}")

    for keyword in ("title", "description"):
        if keyword in schema and not isinstance(schema[keyword], str):
            raise bad(keyword, "a string")
    types = None
    if "type" in schema:
        kind = schema["type"]
        names = [kind] if isinstance(kind, str) else kind
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) and n in TYPES for n in cast(list[object], names))
        ):
            raise bad("type", f"one of {sorted(TYPES)} or a non-empty list of them")
        types = frozenset(cast(list[str], names))
    options: list[object] | None = None
    try:
        if "enum" in schema:
            listed = schema["enum"]
            if not isinstance(listed, list) or not listed:
                raise bad("enum", "a non-empty array")
            options = cast(list[object], listed)
            for option in options:
                canonical(option)
        if "const" in schema:
            const = schema["const"]
            encoded = canonical(const)
            options = [const] if options is None else [o for o in options if canonical(o) == encoded]
    except ValueError as exc:
        if str(exc).startswith("schema at"):
            raise
        raise ValueError(f"schema at {path}: enum and const must hold JSON values ({exc})") from None
    bounds: list[tuple[str, int | float]] = []
    for keyword in BOUNDS:
        if keyword in schema:
            bound = schema[keyword]
            if (
                isinstance(bound, bool)
                or not isinstance(bound, int | float)
                or (isinstance(bound, float) and not math.isfinite(bound))
            ):
                raise bad(keyword, "a finite number")
            bounds.append((keyword, bound))
    lengths: dict[str, int] = {}
    for keyword in LENGTHS:
        if keyword in schema:
            length = schema[keyword]
            if isinstance(length, bool) or not isinstance(length, int) or length < 0:
                raise bad(keyword, "an integer >= 0")
            lengths[keyword] = length
    items = _compile(schema["items"], f"{path}[]") if "items" in schema else None
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise bad("properties", "an object of schemas")
    compiled = {
        str(name): _compile(sub, f"{path}.{name}")
        for name, sub in cast(Mapping[object, object], properties).items()
    }
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(n, str) for n in cast(list[object], required)):
        raise bad("required", "an array of strings")
    extra = schema.get("additionalProperties", True)
    if not isinstance(extra, bool):
        extra = _compile(extra, f"{path}.*")
    node = _Node(
        types=types,
        bounds=tuple(bounds),
        lengths=lengths,
        items=items,
        properties=compiled,
        required=tuple(cast(list[str], required)),
        additional=extra,
    )
    if options is None:
        return node
    # An option the rest of the node refuses can never match; the ones left decide alone.
    return replace(node, allowed=frozenset(canonical(o) for o in options if _check(node, o, path) is None))


def _outside(value: int | float, keyword: str, bound: int | float) -> bool:
    # Python compares int and float exactly, at any magnitude: no conversion, no rounding.
    if keyword == "minimum":
        return value < bound
    if keyword == "maximum":
        return value > bound
    if keyword == "exclusiveMinimum":
        return value <= bound
    return value >= bound


def _check(node: _Node, value: object, path: str) -> str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return f"{path}: not a finite number"
    kinds = _type_of(value)
    if not kinds:
        return f"{path}: not a JSON value"
    if node.types is not None and not node.types & kinds:
        return f"{path}: expected {' or '.join(sorted(node.types))}"
    if node.allowed is not None:
        # The allowed options already satisfy everything else here: membership decides.
        try:
            encoded = canonical(value)
        except ValueError:
            return f"{path}: not a finite JSON value"
        return None if encoded in node.allowed else f"{path}: not one of the allowed values"
    if "number" in kinds:
        number = cast(int | float, value)
        for keyword, bound in node.bounds:
            if _outside(number, keyword, bound):
                return f"{path}: outside {keyword} {bound}"
    lengths = node.lengths
    if isinstance(value, str):
        if len(value) < lengths.get("minLength", 0):
            return f"{path}: shorter than minLength"
        if "maxLength" in lengths and len(value) > lengths["maxLength"]:
            return f"{path}: longer than maxLength"
    if isinstance(value, list):
        items = cast(list[object], value)
        if len(items) < lengths.get("minItems", 0):
            return f"{path}: fewer than minItems"
        if "maxItems" in lengths and len(items) > lengths["maxItems"]:
            return f"{path}: more than maxItems"
        if node.items is not None:
            for i, item in enumerate(items):
                if (problem := _check(node.items, item, f"{path}[{i}]")) is not None:
                    return problem
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        for name in node.required:
            if name not in fields:
                return f"{path}.{name}: missing"
        for name, item in fields.items():
            sub = node.properties.get(name)
            if sub is None and node.additional is False:
                return f"{path}: unexpected property"
            if sub is None and isinstance(node.additional, _Node):
                sub = node.additional
            if sub is not None and (problem := _check(sub, item, f"{path}.{name}")) is not None:
                return problem
    return None


class Checker:
    """`Checker(schema)(value)` is None if `value` fits, else why not (naming the path, never
    the value). ValueError, naming the path, for a schema outside the supported subset."""

    def __init__(self, schema: Mapping[str, object]) -> None:
        self._root = _compile(schema, "$")
        self.schema = schema

    def __call__(self, value: object) -> str | None:
        return _check(self._root, value, "$")
