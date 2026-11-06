#!/usr/bin/env python3
"""Rewrite catalog rows from today's spelling to the v2 spelling (catalog schema v2, section 4.4).

    uv run python scripts/catalog_v2.py ID [ID ...]              # dry-run: print each rewrite
    uv run python scripts/catalog_v2.py --provider SLUG          # every row of a provider
    uv run python scripts/catalog_v2.py --apply ID [ID ...]      # rewrite the files in place

The rewrite is mechanical: `cost` becomes `price`, `provenance`, `reserve`, `settle` and `bill`;
`expect`, the endpoint's `miss` and `not_found`, and the adapter's `miss` expression become the
row's outcome keys in the one path and predicate syntax. It writes today's behavior, never a wished
one. A row it cannot rewrite that way stays as it is and is listed with the reason; a rewrite that
reads some answer shape differently is printed with a warning, and the fixture replay is the proof.
Applying keeps every comment outside the replaced keys; the dry-run names any comment it would drop.

A provider whose endpoint-file header declares `defaults:` has its rows written against them: a key
equal to the default is left out, one the row lacks is written as `null`, and a `bill` default is
inherited whole, because it is the batch's translation of the Python branch it deletes.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from treg.domain.catalog.schema import CatalogSchemaError, merge, parse_row, read_yaml, spelling
from treg.domain.catalog.schema.v2 import cost_type

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"
# Where today's bills are decided in Python: a row named there, or its provider named without an
# endpoint on the same line, has a branch its provider's batch moves into data or a plugin.
BRANCH_SOURCES = (ROOT / "src/treg/application/call/settle.py", ROOT / "src/treg/application/call/resolve.py",
                  ROOT / "src/treg/domain/catalog/results.py")
PROVIDER_KEYS = ("call_fee", "call_fee_per", "contactout", "sumble", "enrichlayer", "octen_rates",
                 "tavily_rates", "bills_miss", "ceiling_param")
PLUGIN_SETTLES = ("quantity", "base", "modifiers", "terminal_presence")
# The 2xx error envelope today's result judge reads on every result-aware row (`results.classify`).
ENVELOPE = {"any": [{"path": "response.error", "op": "present"}, {"path": "response.errors", "op": "present"}]}
OUTCOME_KEYS = ("expect", "error", "miss", "not_found")
COST_KEYS = ("price", "provenance", "reserve", "settle", "bill")


class Refused(ValueError):
    """The row has no rewrite that keeps today's behavior."""


@dataclass
class Rewrite:
    endpoint_id: str
    keys: dict = field(default_factory=dict)      # the v2 keys to write, in order
    replaced: tuple[str, ...] = ()                # today's keys they replace
    adapter_miss: bool = False                    # the adapter's miss moved to the row
    warnings: list[str] = field(default_factory=list)


def money(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused(f"money {value!r} is not a number")
    return format(Decimal(repr(value)), "f")


def response_path(path: str) -> str:
    """`a.0.b`, `a[0].b`, `[0].b`, `.` and `details[k=v]` under `response`."""
    text = str(path).strip()
    if text in (".", ""):
        return "response"
    text = re.sub(r"\[(\d+)\]", r".\1", text).lstrip(".")
    return f"response.{text}"


def request_path(path: str) -> str:
    location, _, rest = str(path).partition(".")
    if location == "body":
        return f"body.{rest}"
    if location in ("queryParams", "pathParams"):
        return f"query.{rest}"
    raise Refused(f"request path {path!r}: only body, queryParams and pathParams are request evidence")


_LITERAL = re.compile(r"""^(?:'([^']*)'|"([^"]*)"|(-?\d+)|(-?\d+\.\d+)|(true|false))$""")
_PATH = re.compile(r"^(?:\.|(?:\[\d+\])?[A-Za-z_][\w-]*(?:\[\d+\])?(?:\.[A-Za-z_][\w-]*(?:\[\d+\])?)*|\[\d+\])$")


def predicate(expr: str, warnings: list[str]) -> dict | list:
    """An adapter expression's v2 predicate, by the section 4.4 table."""
    left, op, right = (part.strip() for part in re.split(r"(==|!=)", str(expr).strip(), maxsplit=1)) \
        if re.search(r"==|!=", str(expr)) else (str(expr), "", "")
    if op != "==":
        raise Refused(f"`{expr}` has no zero-diff predicate (the predicate has no negation or call)")
    if left.startswith("coalesce(") and left.endswith(")") and right == "[]":
        *names, last = (name.strip() for name in left[len("coalesce("):-1].split(","))
        if last != "[]" or not names or not all(_PATH.match(name) for name in names):
            raise Refused(f"`{expr}` has no zero-diff predicate")
        warnings.append(f"`{expr}` becomes absent-or-empty, which also reads {{}} as a miss")
        return _coalesced([response_path(name) for name in names])
    if not _PATH.match(left):
        raise Refused(f"`{expr}` has no zero-diff predicate")
    path = response_path(left)
    if right == "null":
        return {"path": path, "op": "absent"}
    if right == "[]":
        # present: today a null is not `[]`, so it stays a hit
        warnings.append(f"`{expr}` becomes present and `empty`, which also reads '' and {{}} as a miss")
        return [{"path": path, "op": "present"}, {"path": path, "op": "empty"}]
    if not (literal := _LITERAL.match(right)):
        raise Refused(f"`{expr}` has no zero-diff predicate")
    single, double, whole, fraction, flag = literal.groups()
    value = (single if single is not None else double if double is not None
             else int(whole) if whole is not None else float(fraction) if fraction is not None else flag == "true")
    return {"path": path, "op": "equals", "value": value}


def _coalesced(paths: list[str]) -> dict | list:
    """`coalesce(p1, ..., pn, []) == []`: today's coalesce skips null, '' and [], so it holds when
    every path is missing or one of those."""
    rules = [{"any": [{"path": path, "op": "absent"}, {"path": path, "op": "empty"}]} for path in paths]
    return rules[0] if len(rules) == 1 else rules


def _branches(provider: str, endpoint_id: str) -> str | None:
    for source in BRANCH_SOURCES:
        for line in source.read_text(encoding="utf-8").splitlines():
            if f'"{endpoint_id}"' in line:
                return f"{source.name} names {endpoint_id}"
            if (re.search(rf'provider\s*(==\s*"{re.escape(provider)}"|in\s*\(.*"{re.escape(provider)}")', line)
                    or re.search(rf'^\s*"{re.escape(provider)}":\s*\(', line)) \
                    and not re.search(r'"[a-z0-9_-]+\.[a-z0-9_.-]+"', line):
                return f"{source.name} decides {provider}'s bills in Python"
    return None


def _entities(row: dict) -> tuple[str, ...]:
    """The entity fields today's hold counts (`resolve._ENTITY_KEYS`) that the row's input declares."""
    keys = ("targets", "keywords", "domains", "urls", "target", "keyword", "domain", "url")
    found = []
    for location in ("queryParams", "body"):
        block = (row.get("input") or {}).get(location) or {}
        block = block.get("properties", block) if isinstance(block, dict) else {}
        found += [f"{'query' if location == 'queryParams' else 'body'}.{k}" for k in keys if k in block]
    return tuple(found)


def _price(cost: dict, warnings: list[str]) -> dict:
    out: dict = {}
    if cost.get("table") is not None:
        rows = []
        for row in cost["table"]:
            when = [{"path": request_path(k), "op": "equals", "value": v} for k, v in row["when"].items()]
            item = {"when": when, "value": money(row["value"])}
            if row.get("times"):
                item["times"] = request_path(row["times"])
            if row.get("times_min") is not None:
                item["times_min"] = row["times_min"]
            rows.append(item)
        out["table"] = rows
        out["fallback"] = {"value": money(cost["fallback"]["value"]), "note": cost["fallback"]["note"]}
    elif cost.get("value") is not None:
        out["value"] = money(cost["value"])
        if cost.get("fallback") is not None:
            # v2 has a fallback only beside a table; beside a value it is the same scalar.
            if money(cost["fallback"]["value"]) != out["value"]:
                raise Refused("cost.fallback beside a different cost.value has no v2 spelling")
            warnings.append("cost.fallback equals cost.value and is dropped; its note joins price.note")
    elif cost.get("type") == "free":
        out["value"] = "0"
    else:
        out["value"] = None
    for key in ("currency", "per", "round", "unit", "display", "note"):
        if key in cost:
            out[key] = cost[key] if key != "display" else {
                k: money(v) if k == "value" else v for k, v in cost["display"].items()}
        elif key == "round" and isinstance(cost.get("reserve"), dict) and "rows" in cost["reserve"] \
                and int(cost.get("per") or 1) > 1:
            # today's row hold rounds up to whole `per` blocks
            out["round"] = "up"
    if cost.get("table") is None and (fallback_note := (cost.get("fallback") or {}).get("note")):
        out["note"] = f"{out['note']} {fallback_note}" if out.get("note") else fallback_note
    if "table_min" in cost:
        warnings.append("cost.table_min is dropped; the loader derives it")
    return out


def rewrite(row: dict, *, provider: str, header_expect: dict | None = None, header_async: dict | None = None,
            adapter: dict | None = None, check_branches: bool = True, defaults: dict | None = None) -> Rewrite:
    """The v2 keys for one row in today's spelling. Raises Refused when there is no rewrite that
    keeps today's behavior."""
    endpoint_id = str(row["id"])
    out = Rewrite(endpoint_id)
    if row.get("cost") is None and defaults and not any(k in row for k in (*COST_KEYS, "observed_cost")):
        # An unpriced row stays unpriced: it opts out of the defaults that describe a price, and its
        # outcome keys in today's spelling move to v2 as a priced row's do.
        keys, kind, bill = {k: None for k in COST_KEYS if k in defaults and k != "bill"}, None, {}
        if spelling({k: row[k] for k in OUTCOME_KEYS if k in row}) == "v2":
            return _checked(row, out, keys, ("tier",) if "tier" in row else (), defaults)
    else:
        keys, kind, bill = _cost(row, provider, out, check_branches)
    expect = row.get("expect") if row.get("expect") is not None else header_expect
    if expect is not None:
        if expect.get("misses"):
            keys["expect"] = {"path": response_path(expect["json_path"]), "op": "in",
                              "value": [expect["equals"], *expect["misses"]]}
        else:
            keys["expect"] = {"path": response_path(expect["json_path"]), "op": "equals",
                              "value": expect["equals"]}
    miss = row.get("miss")
    adapter_miss = (adapter or {}).get("miss")
    if adapter_miss and not (adapter or {}).get("verified", True):
        raise Refused("its adapter is unverified, so nothing judges its miss today")
    status_miss = isinstance(miss, dict) and 400 <= int(miss.get("status") or 0) < 500
    if isinstance(miss, dict) and not status_miss:
        out.warnings.append(f"miss.status {miss.get('status')} is a 2xx and inert today; its means moves to note")
        if miss.get("means"):
            keys["note"] = f"{row['note']} {miss['means']}" if row.get("note") else miss["means"]
    if status_miss and adapter_miss:
        raise Refused("the row has a status miss and an adapter miss; one `miss` rule holds one")
    rules = []
    if expect is not None and expect.get("misses"):
        rules.append({"path": response_path(expect["json_path"]), "op": "in", "value": list(expect["misses"])})
    if status_miss:
        rule = {"status": [int(miss["status"])]}
        if miss.get("when"):
            rule["when"] = predicate(miss["when"], out.warnings)
        rule["means"] = miss["means"]
        keys["miss"] = rule
    elif adapter_miss:
        rules.append(predicate(adapter_miss, out.warnings))
        out.adapter_miss = True
    if rules:
        keys["miss"] = {"when": rules[0] if len(rules) == 1 else {"any": rules}}
    if adapter_miss and kind == "per_success" and expect is not None:
        # Today a failed `expect` is free while billing charges a 2xx error envelope unless the
        # adapter reads it as a miss. One `bill.error` cannot say both, so the envelope stays out of
        # the error rule: billing and routing read it as today, by the miss rule.
        out.warnings.append("the error envelope is not an error rule (a failed expect is free, an "
                            "envelope is charged unless it misses); stats and archive admission "
                            "stop calling a truthy `error` or `errors` an error")
    elif adapter_miss:
        # Today's result judge calls a 2xx carrying `error` or `errors` an error on this row; billing
        # charges it unless the adapter reads it as a miss.
        keys["error"] = {"when": ENVELOPE}
        out.warnings.append("the error envelope is `present`; today it is any truthy `error` or `errors`")
        if kind == "per_success" and not adapter_miss.strip().startswith("coalesce("):
            bill["error"] = "charged"
        elif kind == "per_success":
            out.warnings.append("an error envelope that also carries results is charged today; v2 judges "
                                "it an error, which is free")
    nf = row.get("not_found")
    if isinstance(nf, dict):
        rule = {"status": nf["status"] if isinstance(nf["status"], list) else [nf["status"]]}
        if nf.get("when"):
            rule["when"] = predicate(nf["when"], out.warnings)
        rule["means"] = nf["means"]
        keys["not_found"] = rule
    if bill:
        keys["bill"] = bill
    if defaults and "bill" in defaults:
        keys.pop("bill", None)
        bill = defaults["bill"]
    for key in (*COST_KEYS, *OUTCOME_KEYS):
        if key not in (defaults or {}) or key == "bill":
            continue
        if key not in keys:
            if key == "expect" and expect is None:
                # today the row inherits its header's expect, which the batch moves into defaults
                continue
            keys[key] = None
        elif keys[key] == defaults[key]:
            del keys[key]
    if row.get("async") is False and "async" in (defaults or {}):
        # today's opt-out of the header's async block; v2 opts out of the default with null
        keys["async"] = None
    elif row.get("async") is False and header_async is not None:
        raise Refused("its async: false opts out of the header's async block; the batch moves that block "
                      "into defaults")
    label = cost_type(keys.get("price"), keys.get("reserve"), keys.get("settle"), bill)
    if label != kind:
        out.warnings.append(f"the views will label it {label}; today they say {kind}")
    order = ("price", "provenance", "reserve", "settle", "bill", "expect", "error", "miss", "not_found", "note",
             "async")
    return _checked(row, out, {k: keys[k] for k in order if k in keys},
                    tuple(k for k in ("cost", "tier", *OUTCOME_KEYS, *(("note",) if "note" in keys else ()),
                                      *(("async",) if row.get("async") is False else ()))
                          if k in row), defaults)


def _cost(row: dict, provider: str, out: Rewrite, check_branches: bool) -> tuple[dict, str | None, dict]:
    """A priced row's v2 cost keys, today's `cost.type`, and the bill that type implies."""
    endpoint_id, cost = out.endpoint_id, row.get("cost")
    if not isinstance(cost, dict):
        raise Refused("the row has no cost to rewrite")
    if row.get("observed_cost") is not None:
        raise Refused("an observed_cost price is written by the ingester in its batch")
    if check_branches and (branch := _branches(provider, endpoint_id)):
        raise Refused(f"{branch}; its batch moves that rule into data or a plugin")
    if found := [k for k in PROVIDER_KEYS if k in cost]:
        raise Refused(f"cost.{found[0]} is a single-provider key: a plugin or its batch")
    kind = cost.get("type")
    if kind == "quota_rows":
        raise Refused("quota_rows has no v2 rewrite yet")
    settle_word = cost.get("settle")
    if isinstance(settle_word, str) and settle_word in PLUGIN_SETTLES:
        raise Refused(f"cost.settle: {settle_word} is a plugin or a batch decision")
    if cost.get("unit") in ("character", "utf8_byte"):
        raise Refused(f"a hold by {cost['unit']} is a plugin")
    if "rate_card" in cost:
        out.warnings.append("cost.rate_card is dropped: no code read it")
    keys: dict = {}
    keys["price"] = _price(cost, out.warnings)
    provenance = {k: cost[k] for k in ("source", "source_url", "checked", "confidence") if k in cost}
    if {"source", "checked", "confidence"} <= provenance.keys():
        keys["provenance"] = provenance
    elif provenance and keys["price"].get("value") == "0":
        out.warnings.append("a free row needs no provenance; the partial one is dropped")
    elif provenance:
        raise Refused("a priced row's provenance needs source, checked and confidence")
    per = int(cost.get("per") or 1)
    reserve = cost.get("reserve")
    if isinstance(reserve, dict) and "rows" in reserve:
        rows = reserve["rows"]
        keys["reserve"] = {"rows": {"param": request_path(rows["param"]), "default": rows["default"],
                                    "max": rows["max"]}}
    elif isinstance(reserve, dict) and "entities" in reserve:
        fields = _entities(row)
        if len(fields) > 1:
            raise Refused(f"today's hold takes the first of {', '.join(fields)}; v2 adds them up")
        out.warnings.append("today's hold also counts entity fields the input does not declare")
        if fields:
            keys["reserve"] = {"entities": {"from": fields[0]}}
    elif kind == "per_result" and cost.get("unit") != "call":
        raise Refused("today's per_result hold guesses the count from common parameter names; "
                      "a person picks its reserve")
    if cost.get("reported_charge"):
        reported = cost["reported_charge"]
        keys["settle"] = {"reported": {"path": response_path(reported["path"]), "unit": reported["unit"]}}
        out.warnings.append("today a 2xx whose body `status` is an integer of 400 or more and lacks the "
                            "charge is free; v2 judges it by its outcome (cost G9)")
    elif isinstance(settle_word, dict):
        if per > 1:
            raise Refused("today's count bills whole rows at the value, whatever `per` says")
        keys["settle"] = {"count": {"path": response_path(settle_word["count"]["path"])}}
        out.warnings.append("null items no longer count (cost G3)")
    elif settle_word == "usage":
        usage = cost["usage"]
        keys["settle"] = {"usage": {"unit": usage["unit"], **(
            {"terms": [{"path": response_path(t["path"]), "rate": money(t["rate"])} for t in usage["terms"]]}
            if usage.get("terms") else {"path": response_path(usage["path"])})}}
    bill: dict = {}
    if kind in ("per_call", "per_result"):
        # Today only a per_success row frees a miss or a failed `expect`; the others bill by their
        # settle form whatever the answer says.
        bill = {"miss": "charged", "error": "charged"}
    return keys, kind, bill


def _checked(row: dict, out: Rewrite, keys: dict, replaced: tuple[str, ...], defaults: dict | None) -> Rewrite:
    """The rewrite, once the row it leaves parses against its provider's defaults."""
    out.keys, out.replaced = keys, replaced
    try:
        parse_row(merge({**{k: v for k, v in row.items() if k not in replaced}, **keys}, defaults), out.endpoint_id)
    except CatalogSchemaError as exc:
        raise Refused(f"the rewritten row does not parse: {str(exc).removeprefix(f'{out.endpoint_id}: ')}") from None
    return out

WIDTH = 110


def _flow(value: object) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, sort_keys=False, width=10**6, allow_unicode=True)
    return text.removesuffix("\n...\n").strip()


def emit(key: str, value: object, indent: int) -> list[str]:
    """One key as YAML lines at `indent`: one flow line when it fits, else block style down to the
    first level that fits."""
    head = " " * indent + f"{key}: "
    flow = _flow(value)
    if len(head) + len(flow) <= WIDTH or not isinstance(value, (dict, list)) or not value:
        return [head + flow + "\n"]
    out = [" " * indent + f"{key}:\n"]
    if isinstance(value, dict):
        for k, v in value.items():
            out += emit(k, v, indent + 2)
        return out
    for item in value:
        flow = _flow(item)
        if indent + 4 + len(flow) <= WIDTH or not isinstance(item, dict):
            out.append(" " * (indent + 2) + "- " + flow + "\n")
            continue
        lines = [ln for k, v in item.items() for ln in emit(k, v, indent + 4)]
        out += [" " * (indent + 2) + "- " + lines[0].lstrip(), *lines[1:]]
    return out


def _compose(text: str) -> yaml.MappingNode:
    return yaml.compose(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def _rows(root: yaml.MappingNode) -> list[yaml.MappingNode]:
    for key, value in root.value:
        if key.value == "endpoints" and isinstance(value, yaml.SequenceNode):
            return [n for n in value.value if isinstance(n, yaml.MappingNode)]
    return []


def _extent(lines: list[str], key: yaml.Node) -> tuple[int, int]:
    """The lines of one block key: its own, then every deeper-indented or blank line, without the
    trailing blank and comment lines that belong to what follows."""
    start, column = key.start_mark.line, key.start_mark.column
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > column):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return start, end


def _nodes(node: yaml.Node) -> list[yaml.Node]:
    """Every node under `node`, an aliased one each time an alias names it."""
    children = (node.value if isinstance(node, yaml.SequenceNode)
                else [n for pair in node.value for n in pair] if isinstance(node, yaml.MappingNode) else [])
    return [node, *(n for child in children for n in _nodes(child))]


def anchored(text: str, rewrites: dict[str, Rewrite]) -> dict[str, str]:
    """The rewritten rows whose replaced keys define a YAML anchor that an alias elsewhere names:
    rewriting such a key would leave the alias undefined. Row id to the key."""
    lines = text.splitlines(keepends=True)
    root = _compose(text)
    uses: dict[int, int] = {}
    for node in _nodes(root):
        uses[id(node)] = uses.get(id(node), 0) + 1
    found = {}
    for row in _rows(root):
        row_id = next((v.value for k, v in row.value if k.value == "id"), None)
        change = rewrites.get(row_id)
        for key, value in row.value if change is not None else ():
            if key.value not in change.replaced:
                continue
            start, end = _extent(lines, key)
            if any(uses[id(n)] > 1 and start <= n.start_mark.line < end for n in _nodes(value)):
                found[row_id] = key.value
    return found


def apply_rows(text: str, rewrites: dict[str, Rewrite]) -> tuple[str, list[str]]:
    """The file with each rewritten row's replaced keys swapped for its v2 keys; and the comments
    the swap drops."""
    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, list[str]]] = []
    dropped: list[str] = []
    for node in _rows(_compose(text)):
        keys = {k.value: k for k, _ in node.value}
        if "id" not in keys or node.flow_style:
            continue
        row_id = next(v.value for k, v in node.value if k.value == "id")
        change = rewrites.get(row_id)
        if change is None:
            continue
        spans = [_extent(lines, keys[k]) for k in change.replaced if k in keys]
        indent = keys["id"].start_mark.column
        new = [part for k, v in change.keys.items() for part in emit(k, v, indent)]
        for n, (start, end) in enumerate(spans):
            dropped += [ln.strip() for ln in lines[start:end] if ln.strip().startswith("#") or " #" in ln]
            edits.append((start, end, new if n == 0 else []))
        if not spans:
            at = _extent(lines, list(keys.values())[-1])[1]
            edits.append((at, at, new))
    for start, end, new in sorted(edits, reverse=True):
        lines[start:end] = new
    return "".join(lines), dropped


def drop_adapter_misses(text: str, endpoint_ids: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    root = _compose(text)
    drops = []
    for key, value in root.value:
        if key.value != "adapters":
            continue
        for eid, adapter in value.value:
            if eid.value in endpoint_ids and isinstance(adapter, yaml.MappingNode):
                for k, _value in adapter.value:
                    if k.value == "miss":
                        drops.append(_extent(lines, k))
    for start, end in sorted(drops, reverse=True):
        del lines[start:end]
    return "".join(lines)


def withheld(directory: Path) -> set[Path]:
    """The endpoint files whose header `defaults:` the codemod loads without: those of a provider
    with a row still in today's spelling. A batch declares its defaults before the codemod rewrites
    its rows, and such a row cannot inherit them; a rewritten row needs them."""
    out = set()
    for path in directory.glob("*.yaml"):
        if "\ndefaults:" not in path.read_text(encoding="utf-8"):
            continue
        provider = read_yaml(path)["provider"]
        rows = [row for name in (f"{provider}.yaml", f"{provider}.extended.yaml")
                if (directory / name).exists() for row in read_yaml(directory / name).get("endpoints") or []]
        if any(spelling(row) == "legacy" for row in rows):
            out.add(path)
    return out


def _catalog_before_defaults():
    """The loaded catalog, with the `defaults:` of a provider still in today's spelling withheld."""
    from treg.domain.catalog import store

    held = withheld(CATALOG)
    with tempfile.TemporaryDirectory() as tmp:
        for path in CATALOG.iterdir():
            target = Path(tmp) / path.name
            if path in held:
                target.write_text(yaml.safe_dump({k: v for k, v in read_yaml(path).items() if k != "defaults"},
                                                 sort_keys=False, allow_unicode=True))
            else:
                target.symlink_to(path)
        return store.load(directory=Path(tmp))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--provider")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    from treg.domain.catalog import schema

    loaded = _catalog_before_defaults()
    adapters = {eid: {**a, "verified": loaded.adapters[eid].verified if eid in loaded.adapters else False}
                for eid, a in ((schema.read_yaml(CATALOG / "adapters.yaml") or {}).get("adapters") or {}).items()}
    wanted = set(args.ids)
    rewrites: dict[Path, dict[str, Rewrite]] = {}
    refused: list[tuple[str, str]] = []
    docs = [schema.read_endpoint_file(path) for path in sorted(CATALOG.glob("*.yaml"))
            if isinstance(doc := schema.read_yaml(path), dict) and isinstance(doc.get("endpoints"), list)]
    defaults = schema.provider_defaults(docs)
    for path, provider, doc in ((d.path, d.provider, d.raw) for d in docs):
        for row in doc["endpoints"]:
            if not (row.get("id") in wanted or provider == args.provider):
                continue
            try:
                change = rewrite(row, provider=provider, header_expect=doc.get("expect"),
                                 header_async=doc.get("async"), adapter=adapters.get(row["id"]), defaults=defaults.get(provider))
            except Refused as exc:
                refused.append((row["id"], str(exc)))
                continue
            rewrites.setdefault(path, {})[row["id"]] = change
    for path, changes in rewrites.items():
        for endpoint_id, key in anchored(path.read_text(), changes).items():
            del changes[endpoint_id]
            refused.append((endpoint_id, f"its {key} defines a YAML anchor that another row aliases"))
        text, dropped = apply_rows(path.read_text(), changes)
        for change in changes.values():
            print(f"== {change.endpoint_id} ({path.name}): replaces {', '.join(change.replaced)}")
            for k, v in change.keys.items():
                print("".join(emit(k, v, 2)), end="")
            for warning in change.warnings:
                print(f"   warning: {warning}")
            if change.adapter_miss:
                print("   adapters.yaml: the adapter's miss moves to the row")
        for comment in dropped:
            print(f"   comment dropped in {path.name}: {comment}")
        if args.apply:
            path.write_text(text)
    moved = {eid for changes in rewrites.values() for eid, c in changes.items() if c.adapter_miss}
    if args.apply and moved:
        adapters_path = CATALOG / "adapters.yaml"
        adapters_path.write_text(drop_adapter_misses(adapters_path.read_text(), moved))
    for endpoint_id, reason in refused:
        print(f"cannot rewrite {endpoint_id} zero-diff: {reason}")
    missing = wanted - {eid for changes in rewrites.values() for eid in changes} - {e for e, _ in refused}
    for endpoint_id in sorted(missing):
        print(f"no such row: {endpoint_id}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
