"""`python -m treg.application.replay fixtures|run|diff` - see docs/context/architecture/replay.md."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from . import differ, engine, fixtures, records

_FENCE = re.compile(r"^```replay-declared[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _declarations(args) -> list[differ.Declaration]:
    raw: list = []
    if args.declared:
        raw += yaml.safe_load(Path(args.declared).read_text(encoding="utf-8")) or []
    if args.declared_in:
        for block in _FENCE.findall(Path(args.declared_in).read_text(encoding="utf-8")):
            raw += yaml.safe_load(block) or []
    return differ.parse_declarations(raw)


def _by_rid(path: Path) -> dict[str, dict]:
    return {line["rid"]: line for line in records.read(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m treg.application.replay")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("fixtures", help="write the fixture corpus for this checkout")
    make.add_argument("out", type=Path)
    run = commands.add_parser("run", help="replay a corpus against this checkout")
    run.add_argument("corpus", type=Path)
    run.add_argument("out", type=Path)
    run.add_argument("--fingerprints", type=Path,
                     help="also write this checkout's per-endpoint catalog digests")
    diff = commands.add_parser("diff", help="compare two replays of one corpus")
    diff.add_argument("baseline", type=Path)
    diff.add_argument("candidate", type=Path)
    diff.add_argument("--declared", help="a YAML list of declared differences")
    diff.add_argument("--declared-in", help="Markdown whose ```replay-declared blocks declare them")
    diff.add_argument("--base-fingerprints", type=Path)
    diff.add_argument("--candidate-fingerprints", type=Path)
    diff.add_argument("--report", type=Path, help="also write the report here")
    args = parser.parse_args(argv)

    if args.command == "fixtures":
        count = records.write(args.out, fixtures.generate())
        print(f"{count} fixture records -> {args.out}")
        return 0
    if args.command == "run":
        count = records.write(args.out, (engine.replay(r) for r in records.read(args.corpus)))
        if args.fingerprints:
            args.fingerprints.write_text(json.dumps(engine.fingerprints(), sort_keys=True))
        print(f"{count} records replayed -> {args.out}")
        return 0
    fingerprints = [json.loads(p.read_text()) if p else None
                    for p in (args.base_fingerprints, args.candidate_fingerprints)]
    report = differ.compare(_by_rid(args.baseline), _by_rid(args.candidate), _declarations(args),
                            base_fingerprints=fingerprints[0],
                            candidate_fingerprints=fingerprints[1])
    text = differ.render(report)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
