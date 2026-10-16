#!/usr/bin/env python3
"""Publish the heavy page media (video under src/treg/web/media/) to an R2 bucket and record each
copy in src/treg/web/media-offload.json, which `/media/...` redirects through when `site_media_url`
is set.

Each copy is keyed by its content (`ugc/tutorial.<digest>.mp4`) and stored as immutable, so an
edited file gets a new key instead of overwriting one an edge may have cached. A file is recorded
only after its upload succeeds. Old copies are left in place; nothing in the store is deleted.

Uploads go through `wrangler r2 object put`, so the operator's own Cloudflare login is the only
credential. Run after adding or editing a video, then commit the manifest with the media:

    uv run --frozen python scripts/offload_page_media.py --bucket <bucket> --prefix site/
    uv run --frozen python scripts/offload_page_media.py --bucket <bucket> --prefix site/ --dry-run
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
from treg import page_media  # noqa: E402

IMMUTABLE = "public, max-age=31536000, immutable"


def _write(manifest: dict[str, str]) -> None:
    page_media.MANIFEST.write_text(json.dumps(dict(sorted(manifest.items())), indent=2) + "\n",
                                   encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="", help="key prefix inside the bucket, e.g. site/")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = sorted(p for p in page_media.MEDIA_DIR.rglob("*")
                   if p.is_file() and p.suffix.lower() in page_media.OFFLOAD_SUFFIXES)
    current = {p.relative_to(page_media.MEDIA_DIR).as_posix(): p for p in files}
    manifest = {path: key for path, key in page_media.manifest().items() if path in current}

    for path, file in current.items():
        key = page_media.content_key(path, file.read_bytes())
        if manifest.get(path) == key:
            continue
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        print(f"upload {path} -> {args.prefix}{key} ({file.stat().st_size} bytes)")
        if args.dry_run:
            continue
        subprocess.run(["npx", "-y", "wrangler@4", "r2", "object", "put",
                        f"{args.bucket}/{args.prefix}{key}", "--file", str(file), "--remote",
                        "--content-type", ctype, "--cache-control", IMMUTABLE],
                       check=True, cwd=tempfile.gettempdir())  # wrangler leaves state in its cwd
        manifest[path] = key
        _write(manifest)

    if not args.dry_run:
        _write(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
