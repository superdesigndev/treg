"""The corpus record and the replay output, both JSON lines."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

CLASSES = ("hit", "miss", "non2xx", "async_terminal")


def encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decode(value: str | None) -> bytes:
    return base64.b64decode(value) if value else b""


def read(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as lines:
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not row.get("rid") or not row.get("endpoint_id"):
                raise ValueError(f"{path}:{n}: a record needs `rid` and `endpoint_id`")
            yield row


def line(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def write(path: Path, rows: Iterable[dict]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(line(row) + "\n")
            count += 1
    return count
