"""A vendor answer shortened for a judge to read: its shape and first items, never its full bytes.

A JSON answer keeps every level it has, with long strings cut and long lists kept to their first
items plus a count of the rest (`{"_more": n}`), so a judge can still see how many results there
were, which fields are empty and what the vendor flagged. The cuts tighten until the text fits the
budget; anything else (HTML, text) is stripped of tags and cut to the budget.
"""
from __future__ import annotations

import json
import re

_STEPS = ((8, 300, 60), (5, 160, 40), (3, 80, 25), (2, 40, 15), (1, 24, 10))
_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_SPACE = re.compile(r"\s+")


def _cut(value, items: int, chars: int, keys: int, depth: int = 64):
    if isinstance(value, dict):
        if depth <= 0:
            return {"_keys": len(value)}
        out = {k[:chars]: _cut(v, items, chars, keys, depth - 1) for k, v in list(value.items())[:keys]}
        if len(value) > keys:
            out["_more_keys"] = len(value) - keys
        return out
    if isinstance(value, list):
        if depth <= 0:
            return {"_items": len(value)}
        out = [_cut(v, items, chars, keys, depth - 1) for v in value[:items]]
        if len(value) > items:
            out.append({"_more": len(value) - items})
        return out
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + "…"
    return value


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def shrink(text: str | None, budget: int) -> str:
    """`text` cut to at most `budget` characters, keeping a JSON answer's structure. A JSON answer
    always stays valid JSON: past the last cut, deeper levels are replaced by their counts
    (`{"_items": n}`, `{"_keys": n}`) rather than the text being sliced, so a cut never reads as
    the vendor's malformed output."""
    if budget < 1:
        raise ValueError("budget must be a positive number of characters")
    if not text:
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        plain = _SPACE.sub(" ", _TAG.sub(" ", text)).strip()
        return plain if len(plain) <= budget else plain[:budget - 1] + "…"
    out = _dumps(value)
    for items, chars, keys in _STEPS:
        if len(out) <= budget:
            return out
        out = _dumps(_cut(value, items, chars, keys))
    items, chars, keys = _STEPS[-1]
    for depth in range(8, -1, -1):
        if len(out) <= budget:
            return out
        out = _dumps(_cut(value, items, chars, keys, depth))
    return out if len(out) <= budget else _dumps({"_omitted_bytes": len(text)})
