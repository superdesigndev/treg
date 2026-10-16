"""Page media offloaded to an object store: which files under `web/media/` have a published copy,
and the content-addressed key that copy lives under.

The manifest maps a path under `web/media/` to `<stem>.<digest>.<ext>`. `scripts/offload_page_media.py`
is its only writer: it uploads each copy before recording it, so an entry names bytes that exist.
A copy's key changes whenever its bytes do, so the store can cache it forever, and a file absent
from the manifest is simply served by this process. Stdlib only: the offload script imports it.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

MEDIA_DIR = Path(__file__).parent / "web" / "media"
MANIFEST = Path(__file__).parent / "web" / "media-offload.json"
# Video is where the bytes are; posters, scripts and images stay with the page.
OFFLOAD_SUFFIXES = frozenset({".mp4", ".webm", ".mov"})
DIGEST_CHARS = 16


def content_key(path: str, body: bytes) -> str:
    """`ugc/tutorial.mp4` → `ugc/tutorial.<digest>.mp4`: the extension stays last, so a fetcher that
    infers the type from the URL still can."""
    digest = hashlib.sha256(body).hexdigest()[:DIGEST_CHARS]
    rel = Path(path)
    return str(rel.with_name(f"{rel.stem}.{digest}{rel.suffix}"))


@lru_cache(maxsize=1)
def manifest() -> dict[str, str]:
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))
