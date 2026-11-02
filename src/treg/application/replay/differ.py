"""Join two replays of one corpus by `rid` and report what changed, per endpoint and class.

The report prints counts, field names and amounts only. A value is shown when it is a number, a
boolean, null or a short code; any other string is reduced to its field name.
"""

from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field

from .engine import GROUPS

# Fields whose change alone is listed apart from a change in money or outcome.
REASON_FIELDS = frozenset({"settle.reason", "outcome.reason", "routing.why",
                           "reserve.basis_digest", "adapter.core_digest"})
_CODE = re.compile(r"[A-Za-z0-9_.:\-]{1,48}")


@dataclass(frozen=True)
class Declaration:
    """One expected difference: on an endpoint or a provider, a field may change, optionally only
    from or to a value, optionally only where the candidate's other fields hold given values."""

    reason: str
    endpoint: str | None = None
    provider: str | None = None
    field: str = "*"
    from_value: object = None
    to_value: object = None
    has_from: bool = False
    has_to: bool = False
    where: dict = dataclass_field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict, n: int) -> "Declaration":
        if not isinstance(raw, dict):
            raise ValueError(f"declaration {n}: expected a mapping")
        unknown = set(raw) - {"endpoint", "provider", "field", "from", "to", "where", "reason"}
        if unknown:
            raise ValueError(f"declaration {n}: unknown keys {sorted(unknown)}")
        if bool(raw.get("endpoint")) == bool(raw.get("provider")):
            raise ValueError(f"declaration {n}: name exactly one of `endpoint` or `provider`")
        if not raw.get("reason"):
            raise ValueError(f"declaration {n}: `reason` is required")
        where = raw.get("where") or {}
        if not isinstance(where, dict):
            raise ValueError(f"declaration {n}: `where` maps fields to values")
        return cls(reason=str(raw["reason"]), endpoint=raw.get("endpoint"),
                   provider=raw.get("provider"), field=str(raw.get("field") or "*"),
                   from_value=_value(raw.get("from")), to_value=_value(raw.get("to")),
                   has_from="from" in raw, has_to="to" in raw,
                   where={k: _value(v) for k, v in where.items()})

    def matches(self, change: "Change") -> bool:
        if self.endpoint is not None and not fnmatch.fnmatchcase(change.endpoint_id, self.endpoint):
            return False
        if self.provider is not None and change.provider != self.provider:
            return False
        if not fnmatch.fnmatchcase(change.field, self.field):
            return False
        if self.has_from and change.old != self.from_value:
            return False
        if self.has_to and change.new != self.to_value:
            return False
        return all(change.candidate.get(k) == v for k, v in self.where.items())


def _value(item: object) -> object:
    return tuple(item) if isinstance(item, list) else item


@dataclass(frozen=True)
class Change:
    rid: str
    endpoint_id: str
    provider: str | None
    cls: str | None
    field: str
    old: object
    new: object
    candidate: dict


@dataclass
class Report:
    records: int = 0
    changes: list[tuple[Change, Declaration | None]] = dataclass_field(default_factory=list)
    records_per_key: dict[tuple[str, str], int] = dataclass_field(
        default_factory=lambda: defaultdict(int))
    missing_in_candidate: int = 0
    missing_in_baseline: int = 0
    untested_declarations: list[Declaration] = dataclass_field(default_factory=list)
    touched: dict[str, dict[str, int]] = dataclass_field(default_factory=dict)

    @property
    def undeclared(self) -> list[Change]:
        return [change for change, declared in self.changes if declared is None]

    @property
    def passed(self) -> bool:
        return not self.undeclared and not self.missing_in_candidate and not self.missing_in_baseline


def flatten(line: dict) -> dict[str, object]:
    """`group.field` -> value for every group; a group that did not apply is one None field."""
    out: dict[str, object] = {"error": line.get("error")}
    for group in GROUPS:
        value = line.get(group)
        if not isinstance(value, dict):
            out[group] = value
            continue
        for key, item in value.items():
            out[f"{group}.{key}"] = _value(item)
    return out


def parse_declarations(raw: object) -> list[Declaration]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("the declared differences are a list of mappings")
    return [Declaration.parse(item, n) for n, item in enumerate(raw, 1)]


def compare(baseline: dict[str, dict], candidate: dict[str, dict],
            declarations: list[Declaration], *,
            base_fingerprints: dict[str, str] | None = None,
            candidate_fingerprints: dict[str, str] | None = None) -> Report:
    report = Report()
    report.missing_in_candidate = len(baseline.keys() - candidate.keys())
    report.missing_in_baseline = len(candidate.keys() - baseline.keys())
    used: set[int] = set()
    for rid in sorted(baseline.keys() & candidate.keys()):
        old_line, new_line = baseline[rid], candidate[rid]
        report.records += 1
        endpoint_id = new_line.get("endpoint_id") or old_line.get("endpoint_id")
        cls = new_line.get("class") or old_line.get("class")
        report.records_per_key[(endpoint_id, cls)] += 1
        old, new = flatten(old_line), flatten(new_line)
        for name in sorted(old.keys() | new.keys()):
            before, after = old.get(name), new.get(name)
            if before == after and type(before) is type(after):
                continue
            change = Change(rid, endpoint_id, new_line.get("provider") or old_line.get("provider"),
                            cls, name, before, after, new)
            matched = [n for n, d in enumerate(declarations) if d.matches(change)]
            used.update(matched)
            report.changes.append((change, declarations[matched[0]] if matched else None))
    report.untested_declarations = [d for n, d in enumerate(declarations) if n not in used]
    if base_fingerprints is not None and candidate_fingerprints is not None:
        touched = {e for e in base_fingerprints.keys() | candidate_fingerprints.keys()
                   if base_fingerprints.get(e) != candidate_fingerprints.get(e)}
        counts: dict[str, dict[str, int]] = {e: {} for e in touched}
        for line in candidate.values():
            if line.get("endpoint_id") in counts:
                per = counts[line["endpoint_id"]]
                per[line.get("class")] = per.get(line.get("class"), 0) + 1
        report.touched = counts
    return report


def _shown(value: object) -> str | None:
    if value is None or isinstance(value, (bool, int, float)):
        return "null" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    if isinstance(value, tuple) and all(isinstance(v, str) and _CODE.fullmatch(v) for v in value):
        return "[" + ",".join(value) + "]"
    return None


def _field_summary(changes: list[Change]) -> str:
    by_field: dict[str, set[tuple[str | None, str | None]]] = defaultdict(set)
    for change in changes:
        by_field[change.field].add((_shown(change.old), _shown(change.new)))
    parts = []
    for name, moves in sorted(by_field.items()):
        if len(moves) == 1:
            old, new = next(iter(moves))
            parts.append(f"{name} {old} -> {new}" if old is not None and new is not None else name)
        else:
            parts.append(f"{name} ({len(moves)} transitions)")
    return "; ".join(parts)


def render(report: Report) -> str:
    """The per-endpoint table plus the declaration and coverage sections, as Markdown."""
    by_key: dict[tuple[str, str], list[tuple[Change, Declaration | None]]] = defaultdict(list)
    for change, declared in report.changes:
        by_key[(change.endpoint_id, change.cls)].append((change, declared))
    records = report.records_per_key
    changed_rids = {change.rid for change, _ in report.changes}
    lines = [
        f"Replay diff: {report.records} records, {len(changed_rids)} changed, "
        f"{len({c.rid for c in report.undeclared})} with an undeclared difference.",
        "",
    ]
    if report.missing_in_candidate or report.missing_in_baseline:
        lines += [f"Records missing from the candidate: {report.missing_in_candidate}; "
                  f"from the baseline: {report.missing_in_baseline}.", ""]
    substantive = {k: v for k, v in by_key.items()
                   if any(c.field not in REASON_FIELDS for c, _ in v)}
    reason_only = {k: v for k, v in by_key.items() if k not in substantive}
    for title, table in (("Changed records", substantive), ("Reason-code-only changes", reason_only)):
        if not table:
            continue
        lines += [f"### {title}", "",
                  "| endpoint | class | records | changed | fields (old -> new) | declared |",
                  "|---|---|---:|---:|---|---|"]
        for (endpoint_id, cls), rows in sorted(table.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            changed = len({c.rid for c, _ in rows})
            reasons = sorted({d.reason for _, d in rows if d is not None})
            undeclared = any(d is None for _, d in rows)
            declared = ("NO" + (f" (partly {', '.join(reasons)})" if reasons else "")
                        if undeclared else ", ".join(reasons))
            lines.append(f"| {endpoint_id} | {cls} | {records.get((endpoint_id, cls), 0)} | "
                         f"{changed} | {_field_summary([c for c, _ in rows])} | {declared} |")
        lines.append("")
    if report.untested_declarations:
        lines += ["### Declarations no record exercised", ""]
        lines += [f"- {d.endpoint or 'provider ' + str(d.provider)} `{d.field}`: {d.reason}"
                  for d in report.untested_declarations]
        lines.append("")
    if report.touched:
        untested = sorted(e for e, per in report.touched.items() if not per)
        lines += [f"### Touched endpoints: {len(report.touched)}, {len(untested)} without a record", ""]
        for endpoint_id in sorted(report.touched):
            per = report.touched[endpoint_id]
            counts = ", ".join(f"{cls} {n}" for cls, n in sorted(per.items(), key=lambda kv: str(kv[0])))
            lines.append(f"- {endpoint_id}: {counts or 'untested'}")
        lines.append("")
    return "\n".join(lines)
