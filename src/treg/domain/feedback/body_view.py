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


def _cut(value, items: int, chars: int, keys: int):
    if isinstance(value, dict):
        out = {k: _cut(v, items, chars, keys) for k, v in list(value.items())[:keys]}
        if len(value) > keys:
            out["_more_keys"] = len(value) - keys
        return out
    if isinstance(value, list):
        out = [_cut(v, items, chars, keys) for v in value[:items]]
        if len(value) > items:
            out.append({"_more": len(value) - items})
        return out
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + "…"
    return value


def shrink(text: str | None, budget: int) -> str:
    """`text` cut to at most `budget` characters, keeping a JSON answer's structure."""
    if not text:
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        plain = _SPACE.sub(" ", _TAG.sub(" ", text)).strip()
        return plain if len(plain) <= budget else plain[:budget - 1] + "…"
    out = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    for items, chars, keys in _STEPS:
        if len(out) <= budget:
            return out
        out = json.dumps(_cut(value, items, chars, keys), ensure_ascii=False, separators=(",", ":"))
    return out if len(out) <= budget else out[:budget - 1] + "…"
