"""Import aggregate-only verification data into the configured database after migrating.

Usage: uv run python scripts/import_arena_verification.py /private/path/aggregate.json
Use --check to validate without a database write. Files must remain outside the checkout.
This does not import raw contact evidence or run paid verification.
"""
import argparse
import asyncio
import json
from pathlib import Path

from treg.application.arena_verification_insights import build_snapshot, publish_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.file.stat().st_size > 2_000_000:
        parser.error("Aggregate exceeds size limit")
    try:
        data = json.loads(args.file.read_text())
        payload = build_snapshot(data)
    except (ValueError, TypeError):
        parser.error("Invalid aggregate. Check schema, endpoint/input pairs, counts and dates; raw evidence is not accepted.")
    if args.check:
        print(f"Valid aggregate: {len(payload['rows'])} rows; no database changes")
    else:
        changed = asyncio.run(publish_snapshot(data))
        print("Aggregate published" if changed else "Already imported; unchanged")


if __name__ == "__main__":
    main()
