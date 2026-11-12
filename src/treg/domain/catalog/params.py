"""The one check of a declared request parameter: its type, enum and range, on every credential
tier and every road (a catalog row's query, path and body; a routed call's identity and filters).
Pure: no session, no network; reads only the spec and the value it is given, and never puts the
value in what it returns, since a caller may have pasted a credential into the wrong field."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

TYPES = ("string", "integer", "number", "boolean", "array", "object")
FORMATS = ("url", "email")
# The range keys each type reads; any other range key on a spec is a validator error.
RANGES = {"integer": ("min", "max"), "number": ("min", "max"), "string": ("minLength", "maxLength"),
          "array": ("minItems", "maxItems")}
# The whole-number reading the money path's count rule uses (`call.cost._whole`): a JSON integer,
# or a string of ASCII digits.
_WHOLE = re.compile(r"-?[0-9]+")
_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_ENUM_LISTED = 10
_DIGITS_MAX = 1000
_NAMES = {"string": "a string", "integer": "a whole number", "number": "a number",
          "boolean": "true or false", "array": "an array", "object": "a JSON object"}


def types_of(spec: Mapping | None) -> tuple[str, ...]:
    """The declared type words: one, or a union's list."""
    declared = (spec or {}).get("type")
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, list):
        return tuple(t for t in declared if isinstance(t, str))
    return ()


def takes_list(spec: Mapping | None) -> bool:
    return "array" in types_of(spec)


def sent(value: object) -> bool:
    """A null or an empty string is a parameter not sent."""
    return value is not None and value != ""


def _same(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def wire(value: object) -> str:
    """A value as it is spelled in a query string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _read(kind: str, value: object, text: bool) -> tuple[bool, object]:
    """(fits, the value as that type). `text` reads a query, path or form string by the type."""
    if text and isinstance(value, str):
        if kind == "string":
            return True, value
        if kind == "integer":
            return (True, _whole(value)) if _WHOLE.fullmatch(value) else (False, None)
        if kind == "number":
            return (True, float(value)) if _NUMBER.fullmatch(value) else (False, None)
        if kind == "boolean":
            return (True, value.lower() == "true") if value.lower() in ("true", "false") else (False, None)
        if kind in ("array", "object"):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if kind == "object":
                return (True, parsed) if isinstance(parsed, dict) else (False, None)
            # A value that is not a JSON array is one item, or a list in an encoding of its own
            # (comma, pipe): it is relayed as sent and its items are not read.
            return True, parsed if isinstance(parsed, list) else None
        return False, None
    if kind == "string":
        return isinstance(value, str), value
    if kind == "integer":
        if isinstance(value, str) and _WHOLE.fullmatch(value):
            return True, _whole(value)
        return isinstance(value, int) and not isinstance(value, bool), value
    if kind == "number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value)), value
    if kind == "boolean":
        return isinstance(value, bool), value
    if kind == "array":
        return isinstance(value, list), value
    if kind == "object":
        return isinstance(value, dict), value
    return False, None


def _whole(digits: str) -> int | float:
    """ASCII digits as an integer; one too long to convert compares as an infinity."""
    sign, significant = ("-" if digits.startswith("-") else ""), digits.lstrip("-").lstrip("0")
    if len(significant) > _DIGITS_MAX:
        return -math.inf if sign else math.inf
    return int(sign + (significant or "0"))


def _in_enum(spec: Mapping, value: object, raw: object) -> bool:
    """An entry equal to the value, or spelled as the value was sent or as the value is written in
    a query (an enum of `"10"` holds the integer 10)."""
    scalar = not isinstance(value, (dict, list))
    return any(_same(value, e) or (isinstance(raw, str) and raw == wire(e))
               or (scalar and isinstance(e, str) and e == wire(value)) for e in spec["enum"])


def _enum_reason(spec: Mapping) -> str:
    entries = spec["enum"]
    if len(entries) <= _ENUM_LISTED:
        return "must be one of " + ", ".join(wire(e) for e in entries)
    return "must be one of the declared values"


def _range_reason(kind: str, spec: Mapping, value: object) -> str | None:
    low_key, high_key = RANGES.get(kind, (None, None))
    if low_key is None or (kind == "array" and not isinstance(value, list)):
        return None
    size = len(value) if kind in ("string", "array") else value
    words = {"string": ("be at least {} characters", "be at most {} characters"),
             "array": ("have at least {} items", "have at most {} items")}.get(kind, ("be at least {}", "be at most {}"))
    low, high = spec.get(low_key), spec.get(high_key)
    if _number(low) and size < low:
        return "must " + words[0].format(_show(low))
    if _number(high) and size > high:
        return "must " + words[1].format(_show(high))
    return None


def _show(bound: int | float) -> str:
    return str(int(bound)) if float(bound).is_integer() else str(bound)


def _number(bound: object) -> bool:
    return isinstance(bound, (int, float)) and not isinstance(bound, bool)


def refusal(spec: Mapping | None, value: object, *, text: bool = False) -> str | None:
    """None when `value` meets `spec`, else why not, in words that name the declared rule and never
    the value. A null or empty value is not sent and meets an optional spec; a required one checks
    it as sent. `text` reads a value that arrived as a string (a query, path or form value) by the
    declared type first: ASCII digits for an integer, a decimal for a number, true or false, JSON
    for an object or an array; a list of such strings is a repeated query parameter. In a JSON
    body an integer may also be ASCII digits, as the count rule reads it. An enum on an array
    names whole values; `items` holds what each item must be. A spec without a type word of the
    vocabulary declares nothing to check."""
    if not isinstance(spec, Mapping):
        return None
    if not sent(value) and not spec.get("required"):
        return None
    kinds = types_of(spec)
    if not kinds or any(k not in TYPES for k in kinds):
        return None
    repeated = text and isinstance(value, list)
    if repeated and "array" not in kinds:
        return "must be sent once"
    if repeated:
        kind, read = "array", value
    else:
        for kind in kinds:
            fits, read = _read(kind, value, text)
            if fits:
                break
        else:
            return "must be " + " or ".join(_NAMES.get(k, k) for k in kinds)
    if spec.get("enum") is not None and read is not None and not _in_enum(spec, read, value):
        return _enum_reason(spec)
    if kind == "array" and isinstance(spec.get("items"), Mapping):
        for index, item in enumerate(read if isinstance(read, list) else ()):
            if sent(item) and (reason := refusal({**spec["items"], "required": False}, item, text=repeated)):
                return f"item {index} {reason}"
    return _range_reason(kind, spec, read)


_BOUNDS = {"min": ("integer", "number"), "max": ("integer", "number"), "minLength": ("string",),
           "maxLength": ("string",), "minItems": ("array",), "maxItems": ("array",)}


def holds(kind: str, entry: object) -> bool:
    """A value the catalog writes (an enum entry, a pinned value) is a value of the type as
    written, with no reading of a string."""
    return _read(kind, entry, False)[0] and not (kind != "string" and isinstance(entry, str))


def declaration_errors(spec: object, at: str) -> list[str]:
    """Why a parameter declaration is outside the one vocabulary, nested properties and items
    included: a type word or a list of them, `items` on an array, range keys of its type, a
    `format` on a string, and enum entries its type can hold."""
    if not isinstance(spec, Mapping):
        return [f"{at}: a parameter spec is a mapping"]
    errors: list[str] = []
    declared, kinds = spec.get("type"), types_of(spec)
    if isinstance(declared, list) and (len(kinds) != len(declared) or len(set(kinds)) != len(kinds) or len(kinds) < 2):
        errors.append(f"{at}: a type list names two or more distinct type words")
    elif not isinstance(declared, (str, list)):
        errors.append(f"{at}: type is one of {', '.join(TYPES)}")
    if unknown := [k for k in kinds if k not in TYPES]:
        errors.append(f"{at}: type {', '.join(unknown)} is none of {', '.join(TYPES)}"
                      + ("; url and email are a format of a string" if {"url", "email"} & set(unknown) else ""))
    if declared == "array" and not isinstance(spec.get("items"), Mapping):
        errors.append(f"{at}: an array declares its items")
    if "items" in spec and "array" not in kinds:
        errors.append(f"{at}: items belongs to an array")
    for word in ("minimum", "maximum"):
        if word in spec:
            errors.append(f"{at}: {word} is written {word[:3]}")
    for key, owners in _BOUNDS.items():
        if key not in spec:
            continue
        if not set(owners) & set(kinds):
            errors.append(f"{at}: {key} bounds {' or '.join(owners)}: min/max bound a number, "
                          "minLength/maxLength a string, minItems/maxItems an array")
        elif not _number(spec[key]) or (key not in ("min", "max")
                                        and (not isinstance(spec[key], int) or spec[key] < 0)):
            errors.append(f"{at}: {key} is {'a number' if key in ('min', 'max') else 'a whole number of 0 or more'}")
    for low, high in (("min", "max"), ("minLength", "maxLength"), ("minItems", "maxItems")):
        if _number(spec.get(low)) and _number(spec.get(high)) and spec[low] > spec[high]:
            errors.append(f"{at}: {low} is above {high}")
    if "format" in spec and (spec["format"] not in FORMATS or "string" not in kinds):
        errors.append(f"{at}: format is one of {', '.join(FORMATS)}, on a string")
    entries = spec.get("enum")
    if entries is not None:
        if not isinstance(entries, list) or not entries:
            errors.append(f"{at}: enum lists one or more values")
        elif bad := [e for e in entries if kinds and not any(holds(k, e) for k in kinds)]:
            errors.append(f"{at}: enum entries {', '.join(wire(e) for e in bad[:3])} are not "
                          + " or ".join(_NAMES.get(k, k) for k in kinds)
                          + ("; an enum of each item goes in items" if "array" in kinds else ""))
    properties = spec.get("properties")
    for name, child in properties.items() if isinstance(properties, Mapping) else ():
        errors += declaration_errors(child, f"{at}.{name}")
    if isinstance(spec.get("items"), Mapping):
        errors += declaration_errors(spec["items"], f"{at}[]")
    return errors
