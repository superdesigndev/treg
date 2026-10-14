#!/usr/bin/env python3
"""Give every catalog tool row without an `added:` date today's date, and guard existing dates.

    uv run python scripts/catalog_added.py                    # write today's UTC date where missing
    uv run python scripts/catalog_added.py --check            # list rows with none; exit 1 if any
    uv run python scripts/catalog_added.py --base origin/main # fail if an existing date changed

`added` is the UTC day a tool first became available on main (docs/context/architecture/catalog.md).
It never changes afterwards. `--base` compares every tool id across the whole catalog with the same
id on the base branch, so a row that moves between files keeps its date. CI runs it on every pull
request (.github/workflows/catalog-added.yml); a deliberate change passes only with the
`added-date-change` label, or `--allow-change` when running it by hand.

This helper never touches an existing date. It inserts one line per row as text, next to
`verified:` when the row has one and after `id:` otherwise, so hand-written comments and layout
survive; the YAML is never re-dumped.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"


def _rows(text: str) -> list[yaml.MappingNode]:
    """The endpoint mapping nodes of one catalog file, in file order; [] for a non-tool file."""
    root = yaml.compose(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(root, yaml.MappingNode):
        return []
    for key, value in root.value:
        if key.value == "endpoints" and isinstance(value, yaml.SequenceNode):
            return [n for n in value.value if isinstance(n, yaml.MappingNode)]
    return []


def row_ids(text: str) -> list[str]:
    out = []
    for node in _rows(text):
        for key, value in node.value:
            if key.value == "id" and isinstance(value, yaml.ScalarNode) and value.value:
                out.append(value.value)
    return out


def insert_added(text: str, dates: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """Insert `added: '<date>'` into every row that has none and an entry in `dates`.

    Returns the new text, the ids that got a date, and the ids that could not be placed (a row
    with no `added`, no entry in `dates`, or a layout the line insert cannot handle safely).
    """
    lines = text.splitlines(keepends=True)
    inserts: list[tuple[int, str]] = []
    placed: list[str] = []
    unplaced: list[str] = []
    for node in _rows(text):
        keys = {k.value: (k, v) for k, v in node.value}
        if "id" not in keys or "added" in keys:
            continue
        row_id = keys["id"][1].value
        when = dates.get(row_id)
        if node.flow_style or when is None:
            unplaced.append(row_id)
            continue
        key, value = keys.get("verified") or keys["id"]
        # A one-line scalar ends on its key's line; anything else is not a shape this inserts into.
        if not isinstance(value, yaml.ScalarNode) or value.end_mark.line != key.start_mark.line:
            unplaced.append(row_id)
            continue
        eol = "\r\n" if lines[key.start_mark.line].endswith("\r\n") else "\n"
        inserts.append((key.start_mark.line + 1, " " * key.start_mark.column + f"added: '{when}'{eol}"))
        placed.append(row_id)
    for at, line in sorted(inserts, reverse=True):
        lines.insert(at, line)
    return "".join(lines), placed, unplaced


def missing(directory: Path | None = None) -> dict[Path, list[str]]:
    out = {}
    for path in sorted((directory or CATALOG).glob("*.yaml")):
        _, _, none = insert_added(path.read_text(), {})
        if none:
            out[path] = none
    return out


def _dates(texts: dict[str, str]) -> dict[str, tuple[str, str]]:
    """id -> (file name, added) over the given catalog files; rows without a date are left out."""
    out = {}
    for name, text in texts.items():
        for node in _rows(text):
            keys = {k.value: v for k, v in node.value}
            if "id" in keys and isinstance(keys.get("added"), yaml.ScalarNode):
                out[keys["id"].value] = (name, keys["added"].value)
    return out


def changed_dates(base: str, directory: Path | None = None) -> list[str]:
    """One line per tool whose `added` differs from the same id on `base`."""
    directory = directory or CATALOG
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True,
                              text=True).stdout
    prefix = directory.relative_to(ROOT).as_posix()
    names = [n for n in git("ls-tree", "--name-only", f"{base}:{prefix}").splitlines()
             if n.endswith(".yaml")]
    before = _dates({n: git("show", f"{base}:{prefix}/{n}") for n in names})
    after = _dates({p.name: p.read_text() for p in sorted(directory.glob("*.yaml"))})
    out = []
    for row_id, (old_file, was) in sorted(before.items()):
        if row_id in after and after[row_id][1] != was:
            new_file, now = after[row_id]
            out.append(f"{row_id}: added {was} on {base} ({old_file}), now {now} ({new_file})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only list rows with no added date")
    parser.add_argument("--base", metavar="REF",
                        help="fail if a tool's added date differs from the same id on REF")
    parser.add_argument("--allow-change", action="store_true",
                        help="with --base: report changed dates but pass (the added-date-change label)")
    args = parser.parse_args(argv)
    if args.base:
        changes = changed_dates(args.base)
        for line in changes:
            print(line)
        if not changes:
            print(f"no existing added date changed against {args.base}")
            return 0
        if args.allow_change:
            print(f"{len(changes)} added date(s) changed; allowed (added-date-change)")
            return 0
        print(f"{len(changes)} added date(s) changed. An existing tool keeps the day it reached "
              "main. If the change is deliberate (a wrong backfill, a renamed tool given its old "
              "date), add the `added-date-change` label to the pull request.")
        return 1
    todo = missing()
    if args.check:
        for path, ids in todo.items():
            for row_id in ids:
                print(f"{path.name}: {row_id} has no added date")
        return 1 if todo else 0
    today = datetime.now(UTC).date().isoformat()
    for path, ids in todo.items():
        new, placed, unplaced = insert_added(path.read_text(), dict.fromkeys(ids, today))
        path.write_text(new)
        for row_id in placed:
            print(f"{path.name}: {row_id} added {today}")
        for row_id in unplaced:
            print(f"{path.name}: {row_id} NOT placed; add `added: '{today}'` by hand", file=sys.stderr)
    if not todo:
        print("every tool row already has an added date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
