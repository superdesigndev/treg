#!/usr/bin/env python3
"""Write the loaded catalog as canonical JSON, or compare two such dumps.

    uv run python scripts/catalog_dump.py OUT.json [--catalog DIR]
    uv run python scripts/catalog_dump.py --compare BASE.json CANDIDATE.json

The dump is what readers consume after `catalog_store.load()`: every field of the loaded `Catalog`
(each served row by id, the discovery order as ids, provider facts, rates, taxonomy, aliases, routing
contracts and adapters) plus each row's priced cost view, with sorted keys. A dataclass field that
holds its default is left out, so a new field nobody sets yet leaves the dump unchanged, while a set
value that falls back to its default still shows as a missing key. It imports `treg` from
the running environment and reads that checkout's catalog, or the catalog directory `--catalog`
names. When a pull request changes loader, schema or validator source, CI dumps the base checkout's
catalog with the base and with the merge result's code, and fails on any byte difference, naming the
first differing path: the loader serves today's catalog unchanged (.github/workflows/replay.yml).
"""

from __future__ import annotations

import dataclasses
import json
import sys
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


def first_difference(old: object, new: object, at: str = "") -> str | None:
    """The dotted path of the first place two dumps differ, keys and indexes in sorted order."""
    if old == new:
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            if key not in old or key not in new:
                return f"{at}.{key}" if at else key
            if (found := first_difference(old[key], new[key], f"{at}.{key}" if at else key)) is not None:
                return found
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new)):
            if (found := first_difference(a, b, f"{at}.{i}" if at else str(i))) is not None:
                return found
    return at or "(root)"


def compare(base: dict, candidate: dict) -> list[str]:
    out = []
    if (path := first_difference(base, candidate)) is not None:
        out.append(f"first difference: {path}")
    for section in sorted(base.keys() | candidate.keys()):
        old, new = base.get(section), candidate.get(section)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            keys = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
            out.append(f"{section}: {len(keys)} differ: {', '.join(keys[:20])}{' ...' if len(keys) > 20 else ''}")
        else:
            out.append(f"{section}: differs")
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--compare":
        base, candidate = (Path(p).read_text(encoding="utf-8") for p in argv[1:])
        if base == candidate:
            print("The loaded catalog is byte-identical.")
            return 0
        for line in compare(json.loads(base), json.loads(candidate)) or ["the dumps differ in formatting only"]:
            print(line)
        return 1
    if len(argv) in (1, 3) and argv[1:2] in ([], ["--catalog"]):
        Path(argv[0]).write_text(dump(Path(argv[2]) if len(argv) == 3 else None), encoding="utf-8")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
