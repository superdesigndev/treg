"""A vendor answer shortened for a judge to read: its shape and first items, never its full bytes.

A JSON answer keeps every level it has, with long strings cut and long lists kept to their first
items plus a count of the rest, so a judge can still see how many results there were, which fields
are empty and what the vendor flagged. The cuts tighten until the text fits the budget; anything else
(HTML, text) is stripped of tags and cut to the budget. Every mark a cut leaves is in `MARKERS`,
which the judge's question explains, so a cut is never read as the vendor's own data or format.
"""
from __future__ import annotations

import json
import re

from ... import jsontext

# treg's own cut, not an ellipsis: a vendor's preview may already end in "…".
CUT = "[treg:cut]"
MORE, MORE_KEYS, LIST, OBJECT, OMITTED = ("_treg_more", "_treg_more_keys", "_treg_list", "_treg_object",
                                          "_treg_omitted")
# What each mark a cut leaves means, as the judge is told (`review_codes.body_code_question`).
MARKERS = {
    CUT: "'[treg:cut]' at the end of a string: treg cut the string there (a plain '…' is the vendor's own)",
    MORE: '`{"_treg_more": n}` as a list\'s last item: n more items followed',
    MORE_KEYS: '`"_treg_more_keys": n` in an object: n more fields followed',
    LIST: '`{"_treg_list": n}` in place of a list: a list of n items was there',
    OBJECT: '`{"_treg_object": n}` in place of an object: an object with n fields was there',
    OMITTED: '`{"_treg_omitted": true}` as the whole answer: the answer was too long to show at all',
}

_STEPS = ((8, 300, 60), (5, 160, 40), (3, 80, 25), (2, 40, 15), (1, 24, 10))
_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_SPACE = re.compile(r"\s+")


def _cut(value, items: int, chars: int, keys: int, depth: int = 64):
    if isinstance(value, dict):
        if depth <= 0:
            return {OBJECT: len(value)}
        # Keys are never cut: two keys sharing a long prefix would collapse into one, and one value
        # would silently replace the other. A key that does not fit is dropped and counted.
        out = {k: _cut(v, items, chars, keys, depth - 1) for k, v in list(value.items())[:keys]}
        if len(value) > keys:
            out[MORE_KEYS] = len(value) - keys
        return out
    if isinstance(value, list):
        if depth <= 0:
            return {LIST: len(value)}
        out = [_cut(v, items, chars, keys, depth - 1) for v in value[:items]]
        if len(value) > items:
            out.append({MORE: len(value) - items})
        return out
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + CUT
    return value


def _dumps(value) -> str:
    # A lone UTF-16 surrogate (a vendor truncating text mid-pair) stays its `\\uXXXX` escape, so
    # the judge's request still encodes as UTF-8 (`jsontext`).
    return jsontext.dumps(value, separators=(",", ":"))


# The last resort for a JSON answer, so the smallest budget that always fits; lengths are characters.
_OMITTED_ALL = _dumps({OMITTED: True})
MIN_BUDGET = len(_OMITTED_ALL)


def shrink(text: str | None, budget: int) -> str:
    """`text` cut to at most `budget` characters (at least `MIN_BUDGET`), keeping a JSON answer's structure. A JSON answer
    always stays valid JSON: past the last cut, deeper levels are replaced by their counts
    (`LIST`, `OBJECT`) rather than the text being sliced, so a cut never reads as
    the vendor's malformed output."""
    if budget < MIN_BUDGET:
        raise ValueError(f"budget must be at least {MIN_BUDGET} characters")
    if not text:
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        plain = _SPACE.sub(" ", _TAG.sub(" ", text)).strip()
        return plain if len(plain) <= budget else plain[:budget - len(CUT)] + CUT
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
    return out if len(out) <= budget else _OMITTED_ALL
