"""Uploaded pictures: what each kind accepts, what it is re-drawn into, and how it is named.

Pure rules. The bytes are decoded and re-drawn in `infra.imaging`, stored by `infra.media_store`,
sequenced by `application.images`. Every upload is re-drawn from its pixels, so nothing the uploader
hid in the file (metadata, a second format inside the first) survives; the stored file is named by
the SHA-256 of what was re-drawn, so a name never changes meaning and can be cached for good.
Every name starts with `img/`: the public bucket is shared, and treg writes and deletes only there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# What the file's first bytes must say, whatever its name or declared type claims.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
)


def sniff(data: bytes) -> str | None:
    """The real format from the leading bytes: png, jpeg, webp - or None (SVG, GIF, anything else)."""
    for magic, fmt in _SIGNATURES:
        if data.startswith(magic):
            return fmt
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@dataclass(frozen=True)
class Output:
    suffix: str        # "" or "-640": appended to the fingerprint in the file name
    size: int          # square: the side of the square; fit: the longest side (never enlarged)
    fmt: str           # png | webp
    square: bool       # True: fitted onto a transparent square; False: shape kept, never cropped


@dataclass(frozen=True)
class Kind:
    name: str
    outputs: tuple[Output, ...]
    min_long: int      # the longest side must be at least this
    min_short: int     # and the shortest side at least this
    max_bytes: int
    reviewed: bool     # True: public only once staff approve it; False: published when set

    @property
    def primary(self) -> Output:
        return self.outputs[0]


MAX_PIXELS = 40_000_000  # decompression-bomb guard: no upload may decode to more than this

KINDS: dict[str, Kind] = {k.name: k for k in (
    # Avatars are mostly photos: WebP is a fraction of PNG's size. Icons are logos: PNG stays sharp.
    Kind("user_avatar", (Output("", 256, "webp", square=True),), 64, 64, 2_000_000, reviewed=False),
    Kind("team_avatar", (Output("", 256, "webp", square=True),), 64, 64, 2_000_000, reviewed=False),
    Kind("showcase_icon", (Output("", 512, "png", square=True),), 96, 96, 2_000_000, reviewed=True),
    # Screenshots keep their shape, desktop or phone: a 1080x2400 phone screen becomes 576x1280.
    Kind("showcase_shot", (Output("", 1280, "webp", square=False), Output("-640", 640, "webp", square=False)),
         600, 200, 5_000_000, reviewed=True),
)}

PREFIX = "img/"
CONTENT_TYPES = {"png": "image/png", "webp": "image/webp"}
_KEY = re.compile(r"img/(?P<kind>[a-z_]{1,32})/(?P<sha>[0-9a-f]{64})(?P<suffix>-640)?\.(?P<ext>png|webp)")


class ImageRejected(Exception):
    """A refusal the uploader can act on; `reason` is a stable code, `detail` plain words."""

    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail
        super().__init__(reason)


def kind_of(name: str) -> Kind:
    kind = KINDS.get(name)
    if kind is None:
        raise ImageRejected("unknown_kind", "unknown picture kind")
    return kind


def key_for(kind: Kind, sha256: str, output: Output) -> str:
    return f"{PREFIX}{kind.name}/{sha256}{output.suffix}.{output.fmt}"


def keys_for(kind: Kind, sha256: str) -> list[str]:
    return [key_for(kind, sha256, o) for o in kind.outputs]


def parse_key(key: str) -> tuple[Kind, str] | None:
    """(kind, fingerprint) for a well-formed primary key of a known kind, else None."""
    m = _KEY.fullmatch(key or "")
    if m is None or m["suffix"] or m["kind"] not in KINDS:
        return None
    kind = KINDS[m["kind"]]
    if m["ext"] != kind.primary.fmt:
        return None
    return kind, m["sha"]


def is_stored_key(key: str) -> bool:
    """Any file name this system writes (a primary key or one of its variants)."""
    m = _KEY.fullmatch(key or "")
    return bool(m and m["kind"] in KINDS)


def check_upload(kind: Kind, data: bytes) -> str:
    """Refuse what no re-draw should even try: too big, empty, or not PNG/JPEG/WebP. Returns the
    sniffed format. Pixel-size rules are checked after decoding, in `check_dimensions`."""
    if not data:
        raise ImageRejected("empty", "the file is empty")
    if len(data) > kind.max_bytes:
        raise ImageRejected("too_large", f"the file is over {kind.max_bytes // 1_000_000} MB")
    fmt = sniff(data)
    if fmt is None:
        raise ImageRejected("unsupported_type", "use a PNG, JPEG or WebP image")
    return fmt


def check_dimensions(kind: Kind, width: int, height: int) -> None:
    if width * height > MAX_PIXELS:
        raise ImageRejected("too_many_pixels", "the image is too large to process")
    if max(width, height) < kind.min_long or min(width, height) < kind.min_short:
        if kind.min_long == kind.min_short:
            raise ImageRejected("too_small", f"use an image at least {kind.min_long}x{kind.min_long} px")
        raise ImageRejected("too_small", f"use an image at least {kind.min_long} px on its longest side "
                                         f"and {kind.min_short} px on its shortest")
