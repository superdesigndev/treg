#!/usr/bin/env python3
"""One-time backfill of each catalog tool's `added:` date from git history.

    uv run python scripts/catalog_backfill_added.py              # dry run: report only
    uv run python scripts/catalog_backfill_added.py --apply      # write the dates

A tool's date is the first day its id appears on main: the script walks the first-parent history
of `--ref` (default origin/main), oldest first, re-reads every catalog file each merge changed, and
records the merge day (UTC) on which each id is first present anywhere in the catalog. Lines of main
that a "merge main into <branch>" brought onto that history are walked too (`lines_of_main`), and an
id takes the earliest day any of them had it. Moving a
row between files (an `.extended.yaml` promoted to core) therefore keeps its date.

It prints a report for a human to read before applying: the biggest single-day jumps, possible
renames (an id leaves and a new id with the same provider, method and path arrives within
`--window` days either side), ids removed and later re-added, and any current row it could not date or place.
It never guesses at a rename: a reviewed one is passed as `--same-tool OLD=NEW`, and NEW then
takes OLD's date. All dates are UTC days.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog_added import CATALOG, insert_added  # noqa: E402

ROOT = CATALOG.parent.parent.parent
PREFIX = "src/treg/catalog/"
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def tools(text: str) -> dict[str, tuple[str, str, str]]:
    """id -> (provider, METHOD, path) for every tool row in one catalog file."""
    try:
        doc = yaml.load(text, Loader=_LOADER)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("endpoints"), list):
        return {}
    provider = str(doc.get("provider") or "")
    out = {}
    for row in doc["endpoints"]:
        if isinstance(row, dict) and row.get("id"):
            out[str(row["id"])] = (provider, str(row.get("method") or "").upper(), str(row.get("path") or ""))
    return out


# A branch that merged main into itself and was then fast-forwarded onto main puts ITS line on
# main's first-parent history: main's own merges of that period sit behind the second parent of
# each "merge main into <branch>" commit, and a first-parent walk alone would date their tools to
# the day the branch next merged main. Those second parents are main as it was, so their
# first-parent lines are walked too, and a tool takes the earliest day any main line had it.
MAIN_MERGED_IN = re.compile(r"\bmain'? into\b", re.IGNORECASE)


def _days(shas: list[str]) -> list[tuple[str, date]]:
    if not shas:
        return []
    out = []
    for line in git("show", "-s", "--format=%H %ct", *shas).splitlines():
        sha, ts = line.split()
        out.append((sha, datetime.fromtimestamp(int(ts), UTC).date()))
    return out


def lines_of_main(ref: str) -> list[tuple[str | None, list[tuple[str, date]]]]:
    """[(base, commits oldest first)]: main's first-parent line from the root, then each line of
    main that a "merge main into <branch>" on it brought in, from where it left the line before."""
    out = [(None, _days(git("rev-list", "--first-parent", "--reverse", ref).split()))]
    for line in git("log", "--first-parent", "--merges", "--format=%H%x09%s", ref).splitlines():
        sha, _, subject = line.partition("\t")
        if not MAIN_MERGED_IN.search(subject):
            continue
        parents = git("rev-list", "--parents", "-n1", sha).split()
        side = git("rev-list", "--first-parent", "--reverse", parents[2], "--not", parents[1]).split()
        if side:
            base = (git("rev-list", "--parents", "-n1", side[0]).split()[1:2] or [None])[0]
            out.append((base, _days(side)))
    return out


def changed(sha: str) -> list[str]:
    parent = git("rev-list", "--parents", "-n1", sha).split()[1:2]
    base = parent[0] if parent else git("hash-object", "-t", "tree", "/dev/null").strip()
    names = git("diff", "--name-only", "--no-renames", base, sha, "--", PREFIX).splitlines()
    return [n for n in names if n.endswith(".yaml") and "/" not in n[len(PREFIX):]]


def _tree(sha: str) -> dict[str, dict[str, tuple[str, str, str]]]:
    names = git("ls-tree", "--name-only", f"{sha}:{PREFIX.rstrip('/')}").splitlines() if sha else []
    return {PREFIX + n: tools(git("show", f"{sha}:{PREFIX}{n}")) for n in names if n.endswith(".yaml")}


def walk_line(base: str | None, commits: list[tuple[str, date]]):
    """One line of main: the first day each id is present, the add/remove events, the routes."""
    per_file = _tree(base) if base else {}
    present: set[str] = {i for rows in per_file.values() for i in rows}
    first: dict[str, date] = {}
    seen_at: dict[str, tuple[str, str, str]] = {}
    events: list[tuple[date, str, str]] = []  # (day, "add"|"remove", id)
    for sha, day in commits:
        names = changed(sha)
        if not names:
            continue
        for name in names:
            try:
                text = git("show", f"{sha}:{name}")
            except subprocess.CalledProcessError:
                text = ""  # deleted in this commit
            per_file[name] = tools(text)
        now: dict[str, tuple[str, str, str]] = {}
        for rows in per_file.values():
            now.update(rows)
        for row_id in now.keys() - present:
            first.setdefault(row_id, day)
            events.append((day, "add", row_id))
        for row_id in present - now.keys():
            events.append((day, "remove", row_id))
        seen_at.update(now)
        present = set(now)
    return first, seen_at, events


def walk(ref: str):
    """The earliest day each id was on any line of main. Add/remove events are the first-parent
    line's own (the line the renames and re-adds are read from)."""
    lines = lines_of_main(ref)
    first, seen_at, events = walk_line(*lines[0])
    earlier = 0
    for base, commits in lines[1:]:
        side_first, side_seen, _ = walk_line(base, commits)
        for row_id, day in side_first.items():
            if row_id not in first or day < first[row_id]:
                earlier += row_id in first
                first[row_id] = day
            seen_at.setdefault(row_id, side_seen[row_id])
    print(f"lines of main walked: {len(lines)} ({len(lines) - 1} brought in by merges of main into a branch); "
          f"{earlier} ids dated earlier by them\n")
    return first, seen_at, events, None


def report(first, seen_at, events, current_ids, unplaced, window: int, top: int) -> None:
    dated = [first[i] for i in current_ids if i in first]
    print(f"current tool rows: {len(current_ids)}; dated from history: {len(dated)}")
    print(f"distinct dates: {len(set(dated))}; earliest {min(dated)}; latest {max(dated)}\n")

    print(f"biggest single-day jumps (current tools by added date, top {top}):")
    for day, n in Counter(dated).most_common(top):
        print(f"  {day}  {n}")

    removed = defaultdict(list)
    added = defaultdict(list)
    for day, kind, row_id in events:
        (removed if kind == "remove" else added)[row_id].append(day)

    print(f"\npossible renames (same provider+method+path, new id within {window} days either side, dated later):")
    by_route = defaultdict(list)
    for row_id, day in first.items():
        by_route[seen_at[row_id]].append((day, row_id))
    renames = []
    for old, days in removed.items():
        route = seen_at[old]
        if not route[2]:
            continue
        for gone in days:
            for born, new in by_route[route]:
                if new != old and new in current_ids and abs((born - gone).days) <= window and born > first[old]:
                    renames.append((gone, old, born, new, first[old]))
    for gone, old, born, new, old_first in sorted(set(renames)):
        print(f"  {old} (added {old_first}, gone {gone}) -> {new} (dated {born})")
    if not renames:
        print("  none")

    print("\nids removed and later re-added (keep their FIRST date):")
    back = [(i, removed[i], added[i]) for i in removed if len(added[i]) > 1]
    for row_id, gone, came in sorted(back):
        state = "present now" if row_id in current_ids else "absent now"
        print(f"  {row_id}: added {came[0]}, removed {', '.join(map(str, gone))}, "
              f"re-added {', '.join(map(str, came[1:]))} ({state})")
    if not back:
        print("  none")

    print("\nrows that could not be dated or placed:")
    for item in unplaced:
        print(f"  {item}")
    if not unplaced:
        print("  none")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="origin/main", help="the main branch to read (default origin/main)")
    parser.add_argument("--apply", action="store_true", help="write the dates into the catalog files")
    parser.add_argument("--window", type=int, default=14, help="rename window in days (default 14)")
    parser.add_argument("--top", type=int, default=10, help="how many big days to list (default 10)")
    parser.add_argument("--sample", nargs="*", default=[], help="ids to show before/after")
    parser.add_argument("--same-tool", action="append", default=[], metavar="OLD=NEW",
                        help="a reviewed rename: NEW keeps OLD's date (repeatable)")
    args = parser.parse_args(argv)

    first, seen_at, events, _ = walk(args.ref)
    for pair in args.same_tool:
        old, _, new = pair.partition("=")
        if old not in first or not new:
            parser.error(f"--same-tool {pair}: {old!r} never appeared on {args.ref}")
        first[new] = first[old]
        print(f"same tool: {new} takes {old}'s date, {first[old]}")
    dates = {i: d.isoformat() for i, d in first.items()}
    current_ids: set[str] = set()
    unplaced: list[str] = []
    out: dict[Path, str] = {}
    for path in sorted(CATALOG.glob("*.yaml")):
        text = path.read_text()
        ids = tools(text)
        current_ids |= ids.keys()
        new, placed, skipped = insert_added(text, dates)
        unplaced += [f"{path.name}: {i} ({'no history' if i not in dates else 'layout'})" for i in skipped]
        if placed:
            out[path] = new
    report(first, seen_at, events, current_ids, unplaced, args.window, args.top)

    for want in args.sample:
        for path, new in out.items():
            old = path.read_text()
            if f"id: {want}\n" not in old:
                continue
            for label, text in (("before", old), ("after", new)):
                start = text.index(f"id: {want}\n")
                block = text[text.rindex("\n", 0, start) + 1:].split("\n")
                print(f"\n--- {want} {label} ({path.name})")
                print("\n".join(block[: 1 + next((n for n, ln in enumerate(block[1:], 1)
                                                  if ln.lstrip().startswith("- ")), len(block)) - 1][:40]))

    if args.apply:
        for path, new in out.items():
            path.write_text(new)
        print(f"\nwrote added dates into {len(out)} files")
    else:
        print(f"\ndry run: {len(out)} files would change; re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
