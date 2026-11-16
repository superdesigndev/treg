"""One caller value placed into one upstream path segment."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote_to_bytes

_VALID_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def decoded(value) -> bytes:
    """The bytes a value means: a value holding a real %HH escape is taken as already encoded."""
    text = str(value)
    return unquote_to_bytes(text) if _VALID_PERCENT_ESCAPE_RE.search(text) else text.encode()


def is_dot_segment(value) -> bool:
    """RFC 3986 section 5.2.4 removes `.` and `..` segments, so neither can be one value."""
    return decoded(value) in (b".", b"..")


def encode(value) -> str:
    """`value` as exactly one path segment. `@` is a legal pchar (RFC 3986) and some email-path
    APIs validate it before percent-decoding, so it stays literal; every delimiter is escaped."""
    return quote(decoded(value), safe="@")
