#!/usr/bin/env python3
"""Write the catalog's JSON Schemas for editors: an endpoint file (rows in either spelling) and a
provider manifest, each field tagged with its kind (`x-kind`) and credential placement `x-gated`.

    uv run python scripts/catalog_schema.py OUT_DIR

Point an editor's YAML language server at them, for example with a first line
`# yaml-language-server: $schema=<OUT_DIR>/endpoint-file.schema.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from treg.domain.catalog.schema.export import json_schemas  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    for name, schema in json_schemas().items():
        (out / name).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(out / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
