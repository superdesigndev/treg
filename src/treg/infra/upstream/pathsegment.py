"""One caller value placed into one upstream path segment."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, unquote_to_bytes, urlsplit, urlunsplit

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


def fill(url: str, values: dict[str, str]) -> str:
    """`url` with each `{name}` path placeholder replaced by its value, as one encoded segment.

    Only whole segments of the path are filled, never the query, so a value cannot reach a part
    of the URL its binding did not name. A placeholder may arrive literal (`{name}`, a catalog
    path) or percent-encoded (`%7Bname%7D`, a caller's raw path). A path without the placeholder
    is left as it was."""
    if not values:
        return url
    split = urlsplit(url)
    segments = split.path.split("/")
    for i, segment in enumerate(segments):
        name = re.fullmatch(r"\{(\w+)\}", unquote(segment))
        if name and name.group(1) in values:
            value = values[name.group(1)]
            if is_dot_segment(value) or not decoded(value):
                raise ValueError(f"path credential {name.group(1)!r} is not one path segment")
            segments[i] = encode(value)
    return urlunsplit(split._replace(path="/".join(segments)))
