#!/usr/bin/env python3
"""Rewrite every parameter declaration into the one type vocabulary (catalog-schema.md, "Parameter
types"): JSON Schema's string, integer, number, boolean, array (with `items`) and object, `url`
and `email` as a `format`, and range keys that match the type.

    uv run python scripts/catalog_param_types.py            # dry run: the counts and every guess
    uv run python scripts/catalog_param_types.py --apply    # edit the files in place

It reads every endpoint row's `input` (query, path, header and body specs, nested properties and
items included), a file header's `queryParams`, and `contracts.yaml` (identity, filters, output and
row fields), and edits only what it rewrites, so comments and layout stay. The rewrite:

- `str` and `text` are string, `int` integer, `float` number, `bool` boolean, `dict` and `any`
  object, `url` string with `format: url`; `list` and `array[X]` are an array whose `items` is X.
- A union (`boolean|object`, `string_or_array`) is a type list, `[boolean, object]`, so every value
  its note documents stays valid.
- `other` or no type: the type an `anyOf` declares (its non-null members), else object when the
  example, default, test request or note shows a structure, else string.
- A bare array's `items` come from what the catalog shows of its values: the row's example,
  default, enum and test request, the same parameter of the provider's other rows, and a JSON
  sample in its note; a parameter `POLYMORPHIC` names takes several item types; else string.
- `minimum`/`maximum` are `min`/`max`; on a string `min`/`max` are `minLength`/`maxLength`, on an
  array `minItems`/`maxItems`; `format: uri` is `format: url`.
- An enum entry YAML reads as another type than the declared one is written as that type: `off`
  in a string enum is `"off"`, `'10'` in an integer enum is `10`.

A spec with keys the models refuse (a listed violation) is left alone and reported. Every inferred
type is printed, for a person to correct where the guess is wrong.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from treg.domain.catalog import params
from treg.domain.catalog.schema.files import _Loader

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"
TYPES = ("string", "integer", "number", "boolean", "array", "object")
WORDS = {"str": "string", "text": "string", "int": "integer", "float": "number", "bool": "boolean",
         "dict": "object", "any": "object", "list": "array", "file": "string"}
UNIONS = {"string_or_array": ["array", "string"]}
SPEC_KEYS = frozenset({
    "type", "required", "note", "example", "enum", "default", "min", "max", "minItems", "maxItems",
    "properties", "item_required_all", "item_required_any", "authorization_methods", "description",
    "examples", "minimum", "maximum", "minLength", "maxLength", "format", "items", "anyOf",
    "additionalProperties"})
# Item types no example shows in full: a DataForSEO filter is a flat condition (`["field", ">", 1]`)
# or conditions joined by `and`/`or` (`[[...], "and", [...]]`).
POLYMORPHIC = {("dataforseo", re.compile(r"(^|_)filters$")): ["array", "boolean", "number", "string"]}
# The contract lists whose items are not provider objects (an output) or strings (any other field).
CONTRACT_ITEMS = {("output", "web.map", "results"): ["object", "string"]}
WIDTH = 10**6


def json_type(value: object) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def one(types) -> str | list[str] | None:
    """The type several values share: integers among numbers are numbers; mixed types a list."""
    found = set(types)
    if "number" in found:
        found.discard("integer")
    if not found:
        return None
    return next(iter(found)) if len(found) == 1 else sorted(found)


def flow(value: object) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, sort_keys=False, width=WIDTH, allow_unicode=True)
    return text.removesuffix("\n...\n").strip()


def construct(node: yaml.Node) -> object:
    return _Loader("").construct_object(node, deep=True)


def word(raw: object) -> str | list[str] | None:
    """A spelling in the one vocabulary, or None when it names no type (`other`, empty)."""
    if isinstance(raw, list):
        out = [word(r) for r in raw]
        return None if any(o is None or isinstance(o, list) for o in out) else one(out)
    if not isinstance(raw, str) or not raw.strip() or raw.strip() == "other":
        return None
    raw = raw.strip()
    if raw in UNIONS:
        return UNIONS[raw]
    if "|" in raw:
        parts = [word(p) for p in raw.split("|")]
        return None if any(p is None or isinstance(p, list) for p in parts) else sorted(set(parts))
    if raw.startswith("array[") and raw.endswith("]"):
        return "array"
    return WORDS.get(raw, raw)


def inner(raw: str) -> dict:
    """`array[array[string]]` -> its items spec, `{type: array, items: {type: string}}`."""
    of = raw[len("array["):-1]
    spec: dict = {"type": word(of)}
    if spec["type"] == "array":
        spec["items"] = inner(of) if of.startswith("array[") else {"type": "string"}
    return spec


def items_of(values: list[object]) -> dict | None:
    """An items spec from sample arrays: the types of their members, nested arrays included."""
    members = [m for v in values if isinstance(v, list) for m in v if m is not None]
    found = one(json_type(m) for m in members)
    if found is None:
        return None
    spec: dict = {"type": found}
    if found == "array":
        spec["items"] = items_of([m for m in members if isinstance(m, list)]) or {"type": "string"}
    return spec


def note_items(spec: dict) -> dict | None:
    """The item type a JSON sample in the note shows (`"targets":[{"match_value": ...}]`)."""
    note = f"{spec.get('note') or ''} {spec.get('description') or ''}"
    for pattern, found in ((r"\bnested\b[^{}]*\{\s*\w+\s*,", {"type": "array", "items": {"type": "object"}}),
                           (r"\[\s*\{", {"type": "object"}), (r"\b[Oo]bjects\b", {"type": "object"}),
                           (r"\[\s*\[", {"type": "array", "items": {"type": "string"}}),
                           (r'\[\s*"', {"type": "string"}), (r"\[\s*-?\d", {"type": "number"}),
                           (r"\{\s*\w+\s*,[^{}]*\}", {"type": "object"}), (r"\bseparate object\b", {"type": "object"})):
        if re.search(pattern, note):
            return found
    return None


def structure(spec: dict, evidence: list[object]) -> str | None:
    if "properties" in spec:
        return "object"
    if "items" in spec:
        return "array"
    values = [spec.get("example"), spec.get("default"), *evidence]
    if any(isinstance(v, dict) for v in values):
        return "object"
    if any(isinstance(v, list) for v in values):
        return "array"
    note = f"{spec.get('note') or ''} {spec.get('description') or ''}"
    if re.search(r"\{[^{}]*:[^{}]*\}|\bJSON object\b", note):
        return "object"
    return None


def any_of(spec: dict) -> tuple[str | list[str], dict | None] | None:
    """The type the non-null `anyOf` members declare, and an array member's item type."""
    members = [m for m in spec.get("anyOf") or () if isinstance(m, dict) and m.get("type") != "null"]
    types = [m.get("type") for m in members if isinstance(m.get("type"), str)]
    if not types:
        return None
    items = next((m["items"] for m in members if m.get("type") == "array" and isinstance(m.get("items"), dict)), None)
    items = {"type": items["type"]} if items and isinstance(items.get("type"), str) else None
    return one(types), items


@dataclass
class Edit:
    start: int
    end: int
    text: str


@dataclass
class Report:
    types: Counter = field(default_factory=Counter)
    renames: Counter = field(default_factory=Counter)
    guesses: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    enums: list[str] = field(default_factory=list)
    widened: list[str] = field(default_factory=list)
    files: set[str] = field(default_factory=set)


class File:
    """One catalog file's text, its composed nodes, and the edits that rewrite it."""

    def __init__(self, path: Path, report: Report, samples: dict | None = None):
        self.path, self.report, self.samples = path, report, ({} if samples is None else samples)
        self.text = path.read_text(encoding="utf-8")
        self.root = yaml.compose(self.text, Loader=_Loader)
        self.provider = path.name.removesuffix(".yaml").removesuffix(".extended")
        self.edits: list[Edit] = []
        self.seen: set[int] = set()

    # -- reading ----------------------------------------------------------------------------------

    @staticmethod
    def mapping(node: yaml.Node | None) -> dict[str, tuple[yaml.Node, yaml.Node]]:
        if not isinstance(node, yaml.MappingNode):
            return {}
        return {k.value: (k, v) for k, v in node.value}

    def rows(self):
        """(row id, input pairs, test request) for every row, and the header's queryParams."""
        pairs = self.mapping(self.root)
        if "queryParams" in pairs:
            yield f"{self.path.name}:header", {"queryParams": pairs["queryParams"]}, {}
        endpoints = pairs.get("endpoints", (None, None))[1]
        for row in endpoints.value if isinstance(endpoints, yaml.SequenceNode) else ():
            if not isinstance(row, yaml.MappingNode):
                continue
            rp = self.mapping(row)
            test = construct(rp["test_request"][1]) if "test_request" in rp else {}
            yield (rp["id"][1].value if "id" in rp else "?"), self.mapping(rp.get("input", (None, None))[1]), \
                (test if isinstance(test, dict) else {})

    def locations(self, inp: dict, test: dict):
        """(where, mapping-of-specs node or whole spec node, test values, whole?) per location."""
        for loc in ("queryParams", "pathParams", "headers"):
            if loc in inp:
                yield loc, inp[loc][1], (test.get(loc) or (test.get("queryParams") if loc == "pathParams" else None)), False
        if "body" in inp:
            node, value = inp["body"][1], construct(inp["body"][1])
            body = test.get("body")
            first = body[0] if isinstance(body, list) and body and isinstance(body[0], dict) else body
            if isinstance(value, dict) and "properties" in value:
                yield "body", node, body, True
            elif isinstance(value, dict) and value and not all(isinstance(v, dict) for v in value.values()):
                yield "body", node, body, True
            else:
                yield "body", node, first, False

    def collect(self) -> None:
        """Sample values per (provider, location, parameter), for the other rows' items."""
        for _rid, inp, test in self.rows():
            for loc, node, values, whole in self.locations(inp, test):
                if whole:
                    continue
                kind = "body" if loc == "body" else "query"
                for name, (_k, v) in self.mapping(node).items():
                    spec = construct(v)
                    if not isinstance(spec, dict):
                        continue
                    bucket = self.samples.setdefault((self.provider, kind, name), [])
                    bucket += [x for x in (spec.get("example"), spec.get("default")) if x is not None]
                    if isinstance(values, dict) and values.get(name) is not None:
                        bucket.append(values[name])

    # -- rewriting --------------------------------------------------------------------------------

    def rewrite(self) -> str:
        if self.path.name == "contracts.yaml":
            self.contracts()
        else:
            for rid, inp, test in self.rows():
                for loc, node, values, whole in self.locations(inp, test):
                    kind = "body" if loc == "body" else "query"
                    if whole:
                        first = values[0] if isinstance(values, list) and values and isinstance(values[0], dict) else values
                        self.spec(node, f"{rid}:body", [] if values is None else [values], (kind, "body"),
                                  members=first if isinstance(first, dict) else {})
                        continue
                    for name, (_k, v) in self.mapping(node).items():
                        evidence = [values[name]] if isinstance(values, dict) and name in values else []
                        self.spec(v, f"{rid}:{loc}.{name}", evidence, (kind, name))
        return self.apply()

    def spec(self, node: yaml.Node, where: str, evidence: list[object], key: tuple[str, str] | None,
             contract: tuple[str, str, str] | None = None, members: dict | None = None) -> None:
        if not isinstance(node, yaml.MappingNode) or id(node) in self.seen:
            return
        self.seen.add(id(node))
        value = construct(node)
        if not isinstance(value, dict):
            return
        if set(value) - SPEC_KEYS:
            self.report.skipped.append(f"{where}: keys {sorted(set(value) - SPEC_KEYS)} the models refuse")
            return
        self.plan(node, value, where, evidence, key, contract)
        pairs = self.mapping(node)
        if "properties" in pairs:
            for name, (_k, v) in self.mapping(pairs["properties"][1]).items():
                given = [members[name]] if isinstance(members, dict) and name in members else []
                self.spec(v, f"{where}.{name}", given, (key[0], name) if members is not None and key else None)
        if "items" in pairs:
            self.spec(pairs["items"][1], f"{where}[]", [], None)

    def items_for(self, value: dict, where: str, evidence: list[object], key: tuple[str, str] | None,
                  contract: tuple[str, str, str] | None) -> dict:
        if contract is not None:
            section = contract[0]
            return {"type": CONTRACT_ITEMS.get(contract, "object" if section == "output" else "string")}
        name = key[1] if key else ""
        for (provider, pattern), types in POLYMORPHIC.items():
            if provider == self.provider and pattern.search(name):
                return {"type": types}
        own = [v for v in (value.get("example"), value.get("default"), *evidence) if v is not None]
        found = items_of(own)
        if found is None and value.get("enum"):
            entries = value["enum"]
            found = items_of(entries) if all(isinstance(e, list) for e in entries) \
                else {"type": one(json_type(e) for e in entries) or "string"}
        if found is None and own and all(isinstance(v, str) for v in own):
            found = {"type": "string"}  # a comma list or one value in a query
        if found is None and key is not None:
            found = items_of(self.samples.get((self.provider, *key), []))
            if found is not None:
                self.report.guesses.append(f"{where}: items {flow(found)} (the provider's other rows)")
        if found is None:
            found = note_items(value)
            if found is not None:
                self.report.guesses.append(f"{where}: items {flow(found)} (its note)")
        if found is None:
            found = {"type": "string"}
            self.report.fallbacks.append(f"{where}: items string (nothing shows its values)")
        return found

    def plan(self, node: yaml.MappingNode, value: dict, where: str, evidence: list[object],
             key: tuple[str, str] | None, contract: tuple[str, str, str] | None) -> None:
        raw = value.get("type")
        new = word(raw)
        items: dict | None = None
        fmt: str | None = None
        if raw == "url":
            new, fmt = "string", "url"
        if isinstance(raw, str) and raw.strip().startswith("array["):
            items = inner(raw.strip())
        if new is None:
            declared = any_of(value)
            if declared is not None:
                new, items = declared[0], (declared[1] if declared[0] == "array" else None)
                self.report.guesses.append(f"{where}: {raw or 'no type'} -> {flow(new)} (its anyOf)")
            else:
                new = structure(value, evidence) or "string"
                self.report.fallbacks.append(f"{where}: {raw or 'no type'} -> {new}")
        if new not in TYPES and not (isinstance(new, list) and set(new) <= set(TYPES)):
            raise SystemExit(f"{where}: no rewrite for type {raw!r}")
        new = self.held(new, value, where, evidence, key)
        if "items" in value or new != "array":
            items = None
        elif items is None:
            items = self.items_for(value, where, evidence, key, contract)
        if raw != new or items or fmt:
            self.report.types[f"{raw!r} -> {flow(new)}" + (", items" if items else "")
                              + (f", format {fmt}" if fmt else "")] += 1
        enum_text = ""
        if new == "array" and isinstance(value.get("enum"), list) and not any(isinstance(e, list) for e in value["enum"]):
            enum_text = self.enum_to_items(node, value, items, where)
        added = ([f"items: {flow(items)[:-1]}{enum_text}}}" if enum_text else f"items: {flow(items)}"] if items else []) \
            + ([f"format: {fmt}"] if fmt else [])
        new_text = flow(new)
        type_pair = self.pair(node, "type")
        if type_pair is None:
            self.insert_first(node, where, [f"type: {new_text}", *added])
        else:
            if raw != new:
                self.edits.append(Edit(type_pair[1].start_mark.index, type_pair[1].end_mark.index, new_text))
            self.insert_after(node, type_pair, added)
        self.ranges(node, value, new, where)
        if (fpair := self.pair(node, "format")) is not None and fpair[1].value == "uri":
            self.edits.append(Edit(fpair[1].start_mark.index, fpair[1].end_mark.index, "url"))
            self.report.renames["format uri -> url"] += 1
        self.enum(node, new, where)

    def held(self, new: str | list[str], value: dict, where: str, evidence: list[object],
             key: tuple[str, str] | None) -> str | list[str]:
        """The type, widened by the type of a test request value it would refuse: a verified
        request stays valid. An enum is never widened."""
        text = key is not None and key[0] == "query"
        for sample in evidence:
            if sample is None or isinstance(new, list) or (text and isinstance(sample, list)) or "enum" in value:
                continue
            if params.refusal({"type": new}, params.wire(sample) if text else sample, text=text) is not None:
                widened = one([new, json_type(sample)])
                self.report.widened.append(f"{where}: {new} -> {flow(widened)} (its test request)")
                new = widened
        return new

    @staticmethod
    def pair(node: yaml.MappingNode, key: str):
        return next(((k, v) for k, v in node.value if k.value == key), None)

    def ranges(self, node: yaml.MappingNode, value: dict, new: object, where: str) -> None:
        renames = {"integer": {"minimum": "min", "maximum": "max"}, "number": {"minimum": "min", "maximum": "max"},
                   "string": {"min": "minLength", "max": "maxLength"},
                   "array": {"min": "minItems", "max": "maxItems"}}.get(new if isinstance(new, str) else "", {})
        for k, _v in node.value:
            if k.value in renames:
                if renames[k.value] in value:
                    raise SystemExit(f"{where}: both {k.value} and {renames[k.value]}")
                self.edits.append(Edit(k.start_mark.index, k.end_mark.index, renames[k.value]))
                self.report.renames[f"{new} {k.value} -> {renames[k.value]}"] += 1

    def enum_to_items(self, node: yaml.MappingNode, value: dict, items: dict | None, where: str) -> str:
        """An enum of an array's items moves into `items`, keeping a YAML anchor an example aliases;
        an enum on the array names whole values. The `, enum: ...` text for new items."""
        pairs = list(node.value)
        index = next(i for i, (k, _v) in enumerate(pairs) if k.value == "enum")
        key, val = pairs[index]
        anchor = re.match(r"\s*:\s*(&[^\s,\[\]{}]+)", self.text[key.end_mark.index:])
        enum_text = f", enum: {anchor.group(1) + ' ' if anchor else ''}{flow(value['enum'])}"
        if node.flow_style:
            start = pairs[index - 1][1].end_mark.index if index else key.start_mark.index
            end = val.end_mark.index if index else pairs[index + 1][0].start_mark.index
            self.edits.append(Edit(start, end, ""))
        else:
            start = self.text.rfind("\n", 0, key.start_mark.index) + 1
            end = val.end_mark.index
            line = self.text.rfind("\n", 0, end) + 1
            end = line if not self.text[line:end].strip() else self.line_end(end) + 1
            self.edits.append(Edit(start, end, ""))
        self.report.renames["array enum -> items.enum"] += 1
        if items is not None:
            return enum_text
        inner_node = self.pair(node, "items")[1]
        if not isinstance(inner_node, yaml.MappingNode) or not inner_node.flow_style or "enum" in construct(inner_node):
            raise SystemExit(f"{where}: cannot move its enum into these items")
        at = inner_node.value[-1][1].end_mark.index
        self.edits.append(Edit(at, at, enum_text))
        return ""

    def enum(self, node: yaml.MappingNode, new: object, where: str) -> None:
        pair = self.pair(node, "enum")
        if pair is None or not isinstance(pair[1], yaml.SequenceNode) or new not in ("string", "integer", "number"):
            return
        for item in pair[1].value:
            if not isinstance(item, yaml.ScalarNode):
                continue
            entry = construct(item)
            if new == "string" and not isinstance(entry, str):
                text = '"' + item.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
            elif new != "string" and isinstance(entry, str) and re.fullmatch(r"-?\d+", entry):
                text = entry
            else:
                continue
            self.edits.append(Edit(item.start_mark.index, item.end_mark.index, text))
            self.report.enums.append(f"{where}: enum {item.value!r} -> {text}")

    def line_end(self, index: int) -> int:
        end = self.text.find("\n", index)
        return len(self.text) if end < 0 else end

    def insert_after(self, node: yaml.MappingNode, type_pair, added: list[str]) -> None:
        if not added:
            return
        key, val = type_pair
        if node.flow_style:
            self.edits.append(Edit(val.end_mark.index, val.end_mark.index, "".join(f", {a}" for a in added)))
            return
        at = self.line_end(val.end_mark.index)
        self.edits.append(Edit(at, at, "".join("\n" + " " * key.start_mark.column + a for a in added)))

    def insert_first(self, node: yaml.MappingNode, where: str, added: list[str]) -> None:
        if node.flow_style or not node.value:
            if self.text[node.start_mark.index] != "{":
                raise SystemExit(f"{where}: an empty block spec")
            at = node.start_mark.index + 1
            self.edits.append(Edit(at, at, ", ".join(added) + (", " if node.value else "")))
            return
        first = node.value[0][0]
        line = self.text.rfind("\n", 0, first.start_mark.index) + 1
        if self.text[line:first.start_mark.index].strip():
            raise SystemExit(f"{where}: its first key does not start a line")
        self.edits.append(Edit(line, line, "".join(" " * first.start_mark.column + a + "\n" for a in added)))

    def contract_field(self, node: yaml.Node, where: str, contract: tuple[str, str, str]) -> None:
        """One identity, filter, output or row field: a type word, or a spec mapping."""
        if isinstance(node, yaml.ScalarNode):
            raw = node.value
            new = word(raw)
            if raw == "url":
                text = "{type: string, format: url}"
            elif new == "array":
                text = flow({"type": "array", "items": self.items_for({}, where, [], None, contract)})
            elif new in TYPES:
                text = new
            else:
                raise SystemExit(f"{where}: no rewrite for type {raw!r}")
            if text != raw:
                self.report.types[f"{raw!r} -> {text}"] += 1
                self.edits.append(Edit(node.start_mark.index, node.end_mark.index, text))
            return
        if not isinstance(node, yaml.MappingNode):
            raise SystemExit(f"{where}: a field is a type word or a spec")
        self.seen.add(id(node))
        self.plan(node, construct(node), where, [], None, contract)

    def contracts(self) -> None:
        for cap, (_k, contract) in self.mapping(self.mapping(self.root).get("contracts", (None, None))[1]).items():
            parts = self.mapping(contract)
            if "identity" in parts and isinstance(parts["identity"][1], yaml.SequenceNode):
                for variant in parts["identity"][1].value:
                    for name, (_k2, v) in self.mapping(variant).items():
                        self.contract_field(v, f"{cap}.identity.{name}", ("identity", cap, name))
            for section in ("filters", "output"):
                for name, (_k2, v) in self.mapping(parts.get(section, (None, None))[1]).items():
                    self.contract_field(v, f"{cap}.{section}.{name}", (section, cap, name))
            row = self.mapping(parts.get("row", (None, None))[1])
            for name, (_k2, v) in self.mapping(row.get("fields", (None, None))[1]).items():
                self.contract_field(v, f"{cap}.row.{name}", ("row", cap, name))

    def apply(self) -> str:
        edits = sorted(self.edits, key=lambda e: (e.start, e.end))
        for a, b in zip(edits, edits[1:]):
            if b.start < a.end:
                raise SystemExit(f"{self.path.name}: overlapping edits at {a.start}-{a.end} and {b.start}-{b.end}")
        text = self.text
        for e in reversed(edits):
            text = text[:e.start] + e.text + text[e.end:]
        if text != self.text:
            self.report.files.add(self.path.name)
            try:
                yaml.load(text, Loader=_Loader)
            except yaml.YAMLError as exc:
                raise SystemExit(f"{self.path.name}: the rewrite does not parse: {exc}") from None
        return text


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    args = parser.parse_args(argv)
    report = Report()
    samples: dict = defaultdict(list)
    paths = sorted(args.catalog.glob("*.yaml"))
    for path in paths:
        if path.name != "contracts.yaml":
            File(path, report, samples).collect()
    outputs = {path: File(path, report, samples).rewrite() for path in paths}
    print("Type rewrites:")
    for k, n in report.types.most_common():
        print(f"  {n:6d}  {k}")
    print("Range and format keys:")
    for k, n in report.renames.most_common():
        print(f"  {n:6d}  {k}")
    print(f"Enum entries written as their type ({len(report.enums)}):")
    for line in report.enums:
        print(f"  {line}")
    print(f"Inferred from the catalog ({len(report.guesses)}):")
    for line in report.guesses:
        print(f"  {line}")
    print(f"Widened to hold the row's own test request ({len(report.widened)}):")
    for line in report.widened:
        print(f"  {line}")
    print(f"Fallbacks, nothing shows the type ({len(report.fallbacks)}):")
    for line in report.fallbacks:
        print(f"  {line}")
    for line in report.skipped:
        print(f"skipped {line}")
    print(f"{len(report.files)} files change")
    if args.apply:
        for path, text in outputs.items():
            if text != path.read_text(encoding="utf-8"):
                path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
