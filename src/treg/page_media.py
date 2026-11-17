"""Page videos: kept in the public media bucket, not in Git, and named here.

The manifest maps a path under `/media/` (`ugc/tutorial.mp4`) to the bucket key holding its bytes
(`site/ugc/tutorial.<digest>.mp4`). `scripts/publish_page_media.py` is its only writer: it uploads
a copy before recording it, so an entry names bytes that exist. The key changes whenever the bytes
do, so the copy is cached for good and an edited video never reaches a viewer under an old name.
Stdlib only: the publish script imports it.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

MEDIA_DIR = Path(__file__).parent / "web" / "media"
MANIFEST = Path(__file__).parent / "web" / "media-videos.json"
VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov"})
# The public bucket's prefix for page videos; picture housekeeping never touches it.
PREFIX = "site/"
DIGEST_CHARS = 16


def content_key(path: str, body: bytes) -> str:
    """`ugc/tutorial.mp4` → `site/ugc/tutorial.<digest>.mp4`: the extension stays last, so a
    fetcher that infers the type from the URL still can."""
    digest = hashlib.sha256(body).hexdigest()[:DIGEST_CHARS]
    rel = Path(path)
    return PREFIX + rel.with_name(f"{rel.stem}.{digest}{rel.suffix}").as_posix()


@lru_cache(maxsize=1)
def manifest() -> dict[str, str]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))
