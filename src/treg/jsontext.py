"""JSON text that always encodes as UTF-8, even when a parsed body carries a lone surrogate.

`json.loads` accepts an escaped unpaired UTF-16 surrogate (`"\\ud835"`, what a JavaScript upstream
emits when it truncates text in the middle of a pair, e.g. a LinkedIn post's mathematical-bold
letters) and returns a `str` that UTF-8 refuses to encode. Every `ensure_ascii=False` dump of such a
value then raises `UnicodeEncodeError` - after the upstream answered and, on a direct call, after the
caller was charged. Stdlib only: the lightweight CLI imports this module too.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The escaped form of any UTF-16 surrogate (U+D800-U+DFFF) inside JSON text. A paired escape also
# matches; it only costs a scrub pass that leaves the pair intact.
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F]")


def dumps(value: Any, **kwargs: Any) -> str:
    """`json.dumps` with non-ASCII text kept readable, unless the value holds a lone surrogate:
    then the surrogate is written as its `\\uXXXX` escape (still valid JSON, and the exact text the
    upstream sent) instead of raising."""
    text = json.dumps(value, ensure_ascii=False, **kwargs)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, **kwargs)
    return text


def dumps_bytes(value: Any, **kwargs: Any) -> bytes:
    """`dumps` encoded as UTF-8 in one pass, for a response or request body."""
    try:
        return json.dumps(value, ensure_ascii=False, **kwargs).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, **kwargs).encode("ascii")


def may_hold_surrogates(raw: bytes) -> bool:
    """Whether JSON text could decode to a string holding a surrogate - a cheap gate before
    `scrub_surrogates` walks a large body."""
    return _SURROGATE_ESCAPE.search(raw) is not None


def scrub_surrogates(value: Any) -> Any:
    """Replace every unpaired surrogate with U+FFFD, recursively through dicts and lists.

    Only for a serializer that cannot write an escape (pydantic, behind the MCP SDK): there the
    choice is a replacement character or no answer at all. Correctly paired text is unchanged."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if isinstance(value, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_surrogates(v) for v in value]
    return value
