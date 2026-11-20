#!/usr/bin/env python3
"""Write the loaded catalog as canonical JSON, or compare two such dumps.

    uv run python scripts/catalog_dump.py OUT.json [--catalog DIR]
    uv run python scripts/catalog_dump.py --compare BASE.json CANDIDATE.json [--declared-in PR_BODY.md]

The dump is what readers consume after `catalog_store.load()`: every field of the loaded `Catalog`
(each served row by id, the discovery order as ids, provider facts, rates, taxonomy, aliases, routing
contracts and adapters) plus each row's priced cost view, with sorted keys. A dataclass field that
holds its default is left out, so a new field nobody sets yet leaves the dump unchanged, while a set
value that falls back to its default still shows as a missing key. It imports `treg` from
the running environment and reads that checkout's catalog, or the catalog directory `--catalog`
names. When a pull request changes loader, schema or validator source, CI dumps the base checkout's
catalog with the base and with the merge result's code, and fails on any byte difference, naming the
first differing path: the loader serves today's catalog unchanged (.github/workflows/replay.yml).
A pull request that means to change how existing rows are served lists the dotted paths it changes,
as globs, one per line in a fenced `dump-declared` block of its body (`--declared-in`); a difference
at a declared path passes, any other still fails.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path


def _default(f: dataclasses.Field) -> object:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return dataclasses.MISSING


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # nested dataclasses come back through this hook from json.dumps
        return {f.name: v for f in dataclasses.fields(value)
                if (v := getattr(value, f.name)) != _default(f)}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    raise TypeError(f"{type(value).__name__} is not JSON")


def dump(directory: Path | None = None) -> str:
    from treg.domain.catalog import store

    cat = store.load(directory=directory)
    doc = {f.name: getattr(cat, f.name) for f in dataclasses.fields(cat) if f.init}
    doc["endpoints"] = [ep["id"] for ep in cat.endpoints]
    doc["cost_views"] = {eid: cat.cost_view(ep.get("cost"), ep.get("provider")) for eid, ep in cat.by_id.items()}
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, default=_plain) + "\n"


def differences(old: object, new: object, at: str = ""):
    """Every dotted path at which two dumps differ, keys and indexes in sorted order."""
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            path = f"{at}.{key}" if at else key
            if key not in old or key not in new:
                yield path
            else:
                yield from differences(old[key], new[key], path)
        return
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new)):
            yield from differences(a, b, f"{at}.{i}" if at else str(i))
        return
    yield at or "(root)"


def first_difference(old: object, new: object, at: str = "") -> str | None:
    """The dotted path of the first place two dumps differ."""
    return next(differences(old, new, at), None)


def declared_paths(body: str) -> list[str]:
    """The globs in a pull request body's fenced `dump-declared` blocks; `#` starts a reason."""
    return [glob for block in re.findall(r"```dump-declared[^\n]*\n(.*?)```", body, re.S)
            for line in block.splitlines() if (glob := line.split("#", 1)[0].strip())]


def compare(base: dict, candidate: dict, declared: list[str] = ()) -> list[str]:
    def undeclared(paths) -> list[str]:
        return [p for p in paths if not any(fnmatchcase(p, glob) for glob in declared)]

    paths = list(differences(base, candidate))
    out = [f"declared {glob}: {sum(fnmatchcase(p, glob) for p in paths)} difference(s)" for glob in declared]
    left = undeclared(paths)
    if left:
        out.append(f"first difference: {left[0]}")
    missing = object()
    for section in sorted(base.keys() | candidate.keys()):
        old, new = base.get(section, missing), candidate.get(section, missing)
        if not undeclared(differences(old, new, section)):
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            keys = sorted(k for k in old.keys() | new.keys()
                          if undeclared(differences(old.get(k, missing), new.get(k, missing), f"{section}.{k}")))
            out.append(f"{section}: {len(keys)} differ: {', '.join(keys[:20])}{' ...' if len(keys) > 20 else ''}")
        else:
            out.append(f"{section}: differs")
    return out


def main(argv: list[str]) -> int:
    if len(argv) in (3, 5) and argv[0] == "--compare" and argv[3:4] in ([], ["--declared-in"]):
        base, candidate = (Path(p).read_text(encoding="utf-8") for p in argv[1:3])
        if base == candidate:
            print("The loaded catalog is byte-identical.")
            return 0
        declared = declared_paths(Path(argv[4]).read_text(encoding="utf-8")) if len(argv) == 5 else []
        old, new = json.loads(base), json.loads(candidate)
        lines = compare(old, new, declared)
        if old == new:
            lines.append("the dumps differ in formatting only")
        elif not any(line.startswith("first difference: ") for line in lines):
            print("\n".join(lines + ["The loaded catalog differs only at the declared paths."]))
            return 0
        print("\n".join(lines))
        return 1
    if len(argv) in (1, 3) and argv[1:2] in ([], ["--catalog"]):
        Path(argv[0]).write_text(dump(Path(argv[2]) if len(argv) == 3 else None), encoding="utf-8")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
