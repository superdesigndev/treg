#!/usr/bin/env python3
"""Fail when the catalog's shrink-only violation list gained an entry the base branch does not have.

    uv run python scripts/catalog_shrink_only.py --base origin/main

The list (src/treg/domain/catalog/schema/violations.yaml) names the existing violations the strict
catalog models still tolerate. An entry may only be removed; CI runs this on every pull request
(.github/workflows/catalog-added.yml).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from treg.domain.catalog.schema import violations  # noqa: E402

RELATIVE = violations.VIOLATIONS_FILE.relative_to(ROOT).as_posix()


def grown(base_text: str | None, head_text: str) -> list[tuple[str, ...]]:
    base = violations.entries(yaml.safe_load(base_text) or {}) if base_text is not None else set()
    return sorted(violations.entries(yaml.safe_load(head_text) or {}) - base)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="the git ref to compare with")
    args = parser.parse_args(argv)
    shown = subprocess.run(["git", "show", f"{args.base}:{RELATIVE}"], cwd=ROOT,
                           capture_output=True, text=True)
    if shown.returncode != 0:
        print(f"{RELATIVE} is new on this branch; nothing to compare")
        return 0
    added = grown(shown.stdout, violations.VIOLATIONS_FILE.read_text(encoding="utf-8"))
    for entry in added:
        print(f"{RELATIVE}: new entry {' '.join(entry)}; fix the data instead of listing it")
    return 1 if added else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
