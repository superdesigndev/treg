"""The reference language of the JSON road — complete, and deliberately small.

| form                      | means                                              |
|---------------------------|----------------------------------------------------|
| `$input.<field>`          | what the caller sent, after the manifest checked it |
| `$<step>.<path>`          | a field of an earlier step's answer, any depth      |
| `$<step>.<path>[0].<f>`   | one item of a list                                  |
| `$<step>[]`               | every answer of a repeated step, as a list          |
| `$<step>.length`          | how many answers a repeated step gave (or a list's) |
| `$0.<path>`               | the same by position, kept as an alias              |
| `$<as>.<field>`           | inside a repeat, the current item                   |

There is nothing else: no arithmetic, no condition, no function. A string that is exactly one
reference resolves to the value itself, whatever its type; a string with references inside text
resolves to text (`"$company.data.name: $verify.length leads"`). Keeping this list short is
what makes a review possible and what keeps the graph derivable from the file alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_REF = re.compile(r"\$(?P<root>[a-z][a-z0-9_]*|\d+)(?P<path>(?:\.[a-z0-9_]+|\[\d*\])*)")
_SEG = re.compile(r"\.([a-z0-9_]+)|\[(\d*)\]")


class RefError(ValueError):
    """A reference that cannot be read: unknown root, bad path, wrong shape."""


@dataclass(frozen=True)
class Ref:
    root: str                       # "input", a step name, an `as` name, or a digit string
    path: tuple[str | int | None, ...]   # str = field, int = index, None = `[]` (whole list)
    text: str                       # the reference as written

    @property
    def is_positional(self) -> bool:
        return self.root.isdigit()


def parse(text: str) -> Ref:
    """One reference, the whole string. Raises RefError on anything else."""
    m = _REF.fullmatch(text)
    if not m:
        raise RefError(f"{text!r} is not a reference")
    return Ref(root=m.group("root"), path=_segments(m.group("path")), text=text)


def _segments(path: str) -> tuple[str | int | None, ...]:
    out: list[str | int | None] = []
    for field, index in _SEG.findall(path):
        if field:
            out.append(field)
        elif index == "":
            out.append(None)
        else:
            out.append(int(index))
    return tuple(out)


def find(text: str) -> list[Ref]:
    """Every reference inside a string, in order (a template may hold several)."""
    return [Ref(root=m.group("root"), path=_segments(m.group("path")), text=m.group(0))
            for m in _REF.finditer(text)]


def roots_in(value: Any) -> set[str]:
    """Every reference root named anywhere in a value (a step's input object, a for_each, a
    skip_if_empty): the graph reads its edges from this."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(r.root for r in find(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= roots_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= roots_in(v)
    return found


def read(ref: Ref, scope: dict[str, Any], positions: dict[str, str] | None = None) -> Any:
    """Read one reference against a scope: `{"input": {...}, "<step>": answer | [answers],
    "<as>": item}`. A positional root (`$0`) is mapped through `positions` (digit → step name).
    A missing field reads as None (a missing answer is data, never a crash); an unknown ROOT is
    an error, because the graph should have refused it at load."""
    root = ref.root
    if ref.is_positional:
        if not positions or root not in positions:
            raise RefError(f"${root} names no step")
        root = positions[root]
    if root not in scope:
        raise RefError(f"${root} is not an input, a step, or a repeat item")
    cur: Any = scope[root]
    segs = list(ref.path)
    while segs:
        seg = segs.pop(0)
        if seg is None:                       # `[]` — the whole list, as is
            if not isinstance(cur, list):
                cur = [] if cur is None else [cur]
            continue
        if seg == "length" and not segs:      # trailing `.length`
            return len(cur) if isinstance(cur, (list, str, dict)) else 0
        if isinstance(seg, int):
            cur = cur[seg] if isinstance(cur, list) and -len(cur) <= seg < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            cur = None
        if cur is None:
            return None
    return cur


def resolve(value: Any, scope: dict[str, Any], positions: dict[str, str] | None = None) -> Any:
    """Resolve every reference inside a value. A string that IS one reference becomes the value
    it names (any type); a string with references among text becomes text; dicts and lists
    resolve their members; everything else passes through."""
    if isinstance(value, str):
        refs = find(value)
        if not refs:
            return value
        if len(refs) == 1 and refs[0].text == value:
            return read(refs[0], scope, positions)
        out = value
        for r in refs:
            v = read(r, scope, positions)
            out = out.replace(r.text, "" if v is None else (v if isinstance(v, str) else _plain(v)), 1)
        return out
    if isinstance(value, dict):
        return {k: resolve(v, scope, positions) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope, positions) for v in value]
    return value


def _plain(v: Any) -> str:
    import json
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def is_empty(v: Any) -> bool:
    """The one condition of the JSON road: skip a step when the value it reads is empty."""
    return v is None or v == "" or v == [] or v == {} or v is False
