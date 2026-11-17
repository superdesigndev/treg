"""The provider's own charge in an answer: the member a row's settlement reads it from, and the
answer without that member. Pure: no session, no network; reads only the row it is given."""

from __future__ import annotations

import json
from json.decoder import scanstring

from .schema.paths import Path, parse_path
from .schema.v2 import is_v2

_WS = " \t\n\r"
_DECODER = json.JSONDecoder()


def path_of(endpoint: dict | None) -> str | None:
    """The answer path a row settles the provider's reported charge from, in the v2 path syntax:
    its `settle.reported.path`, or for a row in today's spelling its `cost.reported_charge.path`
    under `response`, the move `scripts/catalog_v2.py` makes. None when the row reads none, or
    reads it from a header."""
    if not endpoint:
        return None
    if is_v2(endpoint):
        return ((endpoint.get("settle") or {}).get("reported") or {}).get("path")
    path = ((endpoint.get("cost") or {}).get("reported_charge") or {}).get("path")
    return f"response.{path}" if path else None


def without(path: str | None, body: bytes) -> bytes:
    """`body` without the member `path` names, every other byte as it was; `body` itself when
    there is nothing to remove: no path, a path that does not end in a member name, an answer that
    is not a UTF-8 JSON document, or one where the path selects nothing. A path selecting several
    objects loses the member from each, and an object naming the member twice loses both."""
    if not path or not body:
        return body
    try:
        parsed = parse_path(path)
    except ValueError:
        return body
    last = parsed.steps[-1] if parsed.steps else None
    if parsed.location != "response" or last is None or last.name is None or last.filter is not None:
        return body
    try:
        text = body.decode("utf-8")
        json.loads(text)
    except (ValueError, RecursionError):
        return body
    edits = [edit for start in _holders(text, parsed) if (edit := _drop(text, start, last.name))]
    if not edits:
        return body
    for start, end, kept in sorted(edits, reverse=True):
        text = text[:start] + kept + text[end:]
    return text.encode("utf-8")


def _skip(text: str, at: int) -> int:
    while at < len(text) and text[at] in _WS:
        at += 1
    return at


def _members(text: str, start: int) -> tuple[list[tuple[str, int, int, int]], int]:
    """(key, member start, value start, value end) for each member of the object at `start`, and
    where the object ends. `text` is already known to be one valid JSON document."""
    members, at = [], _skip(text, start + 1)
    if text[at] == "}":
        return members, at + 1
    while True:
        key, after = scanstring(text, at + 1)
        value_at = _skip(text, _skip(text, after) + 1)
        _, value_end = _DECODER.scan_once(text, value_at)
        members.append((key, at, value_at, value_end))
        at = _skip(text, value_end)
        if text[at] == "}":
            return members, at + 1
        at = _skip(text, at + 1)


def _elements(text: str, start: int) -> list[tuple[int, object]]:
    """(start, value) for each element of the array at `start`."""
    elements, at = [], _skip(text, start + 1)
    if text[at] == "]":
        return elements
    while True:
        value, end = _DECODER.scan_once(text, at)
        elements.append((at, value))
        at = _skip(text, end)
        if text[at] == "]":
            return elements
        at = _skip(text, at + 1)


def _matching(text: str, starts: list[int], rule: tuple[str, str]) -> list[int]:
    key, value = rule
    return [at for start in starts if text[start] == "[" for at, item in _elements(text, start)
            if isinstance(item, dict) and str(item.get(key)) == value]


def _holders(text: str, path: Path) -> list[int]:
    """Where each object holding the path's last member starts: every step but the last, walked
    as `paths.select` walks them (a repeated key reads its last occurrence)."""
    starts = [_skip(text, 0)]
    if path.filter is not None:
        starts = _matching(text, starts, path.filter)
    for step in path.steps[:-1]:
        if step.every:
            starts = [at for start in starts if text[start] == "[" for at, _ in _elements(text, start)]
        elif step.index is not None:
            starts = [elements[step.index][0] for start in starts if text[start] == "["
                      and step.index < len(elements := _elements(text, start))]
        else:
            found = []
            for start in starts:
                if text[start] == "{":
                    named = [value_at for key, _, value_at, _ in _members(text, start)[0] if key == step.name]
                    found += named[-1:]
            starts = _matching(text, found, step.filter) if step.filter is not None else found
    return [start for start in starts if text[start] == "{"]


def _drop(text: str, start: int, name: str) -> tuple[int, int, str] | None:
    """The object at `start` rewritten without every member named `name`, as (start, end, text):
    kept members, the separator that followed each and the object's own leading and trailing
    whitespace stay as they were. None when it has no such member."""
    members, end = _members(text, start)
    kept = [i for i, (key, *_) in enumerate(members) if key != name]
    if len(kept) == len(members):
        return None
    if not kept:
        return start, end, "{}"
    pieces = [text[start:members[0][1]]]
    for n, i in enumerate(kept):
        pieces.append(text[members[i][1]:members[i][3]])
        if n + 1 < len(kept):
            pieces.append(text[members[i][3]:members[i + 1][1]])
    pieces.append(text[members[-1][3]:end])
    return start, end, "".join(pieces)
