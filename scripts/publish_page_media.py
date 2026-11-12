#!/usr/bin/env python3
"""Publish page videos to the public media bucket and record each in src/treg/web/media-videos.json,
which `/media/...` redirects through. Videos are not committed (.gitignore); the manifest is.

To add or replace a video, put the file under src/treg/web/media/ at the path the page uses, run
this, and commit the manifest. Each copy is keyed by its content (`site/ugc/tutorial.<digest>.mp4`)
and stored as immutable, so an edited file gets a new key instead of overwriting one an edge may
have cached. A file is recorded only after its upload succeeds; nothing in the bucket is deleted.

Uploads go through `wrangler r2 object put`, so the operator's own Cloudflare login is the only
credential:

    uv run --frozen python scripts/publish_page_media.py --bucket <public bucket> --dry-run
    uv run --frozen python scripts/publish_page_media.py --bucket <public bucket>
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from treg import page_media

IMMUTABLE = "public, max-age=31536000, immutable"


def _write(manifest: dict[str, str]) -> None:
    page_media.MANIFEST.write_text(json.dumps(dict(sorted(manifest.items())), indent=2) + "\n",
                                   encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = page_media.manifest()
    files = sorted(p for p in page_media.MEDIA_DIR.rglob("*")
                   if p.is_file() and p.suffix.lower() in page_media.VIDEO_SUFFIXES)
    for file in files:
        path = file.relative_to(page_media.MEDIA_DIR).as_posix()
        key = page_media.content_key(path, file.read_bytes())
        if manifest.get(path) == key:
            continue
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        print(f"upload {path} -> {key} ({file.stat().st_size} bytes)")
        if args.dry_run:
            continue
        subprocess.run(["npx", "-y", "wrangler@4", "r2", "object", "put", f"{args.bucket}/{key}",
                        "--file", str(file), "--remote", "--content-type", ctype,
                        "--cache-control", IMMUTABLE],
                       check=True, cwd=tempfile.gettempdir())  # wrangler leaves state in its cwd
        manifest[path] = key
        _write(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
