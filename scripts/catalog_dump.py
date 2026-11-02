#!/usr/bin/env python3
"""Write the loaded catalog as canonical JSON, or compare two such dumps.

    uv run python scripts/catalog_dump.py OUT.json
    uv run python scripts/catalog_dump.py --compare BASE.json CANDIDATE.json

The dump is what readers consume after `catalog_store.load()`: every field of the loaded `Catalog`
(each served row by id, the discovery order as ids, provider facts, rates, taxonomy, aliases, routing
contracts and adapters) plus each row's priced cost view, with sorted keys. It imports `treg` from
the running environment, so CI runs this script on a pull request's base checkout and on its merge
result and fails on any byte difference (.github/workflows/replay.yml).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    raise TypeError(f"{type(value).__name__} is not JSON")


def dump() -> str:
    from treg.domain.catalog import store

    cat = store.load()
    doc = {f.name: getattr(cat, f.name) for f in dataclasses.fields(cat) if f.init}
    doc["endpoints"] = [ep["id"] for ep in cat.endpoints]
    doc["cost_views"] = {eid: cat.cost_view(ep.get("cost"), ep.get("provider")) for eid, ep in cat.by_id.items()}
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, default=_plain) + "\n"


def compare(base: dict, candidate: dict) -> list[str]:
    out = []
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
    if len(argv) == 1:
        Path(argv[0]).write_text(dump(), encoding="utf-8")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
