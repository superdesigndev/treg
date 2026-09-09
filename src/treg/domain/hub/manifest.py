"""The hub manifest validator — pure rules over one `recipe.json`, no I/O.

A hub tool is a tool a maker publishes on treg, made of other tools (docs/HUB-DECISIONS.md). Its
manifest is the reviewer's whole view of it: what it takes, which tools it may reach, what it
returns, what it costs. Every refusal here names the FIELD and the RULE, because the maker is
usually an agent that must fix the file without a person reading a stack trace.

Rules that came from Crawl4AI's recipes (the same idea, live for months; `LESSONS.md` there):
an input is required by having NO default (never a `required` key); every non-secret input carries
an `example` or a `default`, so the first run and the gallery both work; an `int` input names a
`max`, or the runner cannot clamp it; exactly one of `steps` | `script`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The ceilings of version one (HUB-DECISIONS round 2 q6). A manifest may declare smaller limits,
# never larger.
MAX_STEPS = 20
MAX_WALL_S = 120
MAX_INPUTS = 20
MAX_USES = 50
MAX_OUTPUT_FIELDS = 50
MAX_SUMMARY = 200
MAX_README = 4000
MAX_PRICE_USD = 100.0
MAX_COST_USD = 100.0

INPUT_TYPES = ("string", "int", "float", "bool", "list", "object")
INPUT_KEYS = frozenset({"type", "default", "max", "min", "secret", "example", "note"})
STEP_KEYS = frozenset({"name", "call", "input", "for_each", "as", "skip_if_empty", "writes"})
LIMIT_KEYS = frozenset({"steps", "wall_s", "cost_usd"})
MANIFEST_KEYS = frozenset({
    "name", "version", "summary", "writes", "inputs", "uses", "limits", "price_usd",
    "steps", "script", "output",
})

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")        # the tool's name: `leads-db`
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")        # input names, step names, output fields
_CATALOG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$")
SCRIPT_FILE = "run.js"


class ManifestError(ValueError):
    """One refused manifest: `field` (a dotted path into the file) and `rule` (what it broke)."""

    def __init__(self, field: str, rule: str) -> None:
        super().__init__(f"{field}: {rule}")
        self.field = field
        self.rule = rule


@dataclass(frozen=True)
class Validated:
    """A manifest that passed: normalized values the store and the runner rely on."""

    name: str
    kind: str                 # "steps" | "script"
    summary: str
    writes: bool
    inputs: dict[str, dict[str, Any]]
    uses: list[str]
    limits: dict[str, Any]    # steps, wall_s, cost_usd (cost_usd may be None)
    price_micro: int
    steps: list[dict[str, Any]] | None
    script: str | None
    output: dict[str, Any]
    manifest: dict[str, Any]  # the normalized file, what gets stored


def _fail(field: str, rule: str) -> ManifestError:
    return ManifestError(field, rule)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool))


def validate(
    raw: Any,
    *,
    catalog_ids: set[str],
    own_tools: set[str],
    hub_ids: frozenset[str] | set[str] = frozenset(),
) -> Validated:
    """Validate one manifest. `catalog_ids` and `own_tools` are the two universes `uses` may
    name; `hub_ids` are refused by name (a hub tool may not use a hub tool, depth one)."""
    if not isinstance(raw, dict):
        raise _fail("manifest", "must be a JSON object")
    unknown = sorted(set(raw) - MANIFEST_KEYS)
    if unknown:
        raise _fail(unknown[0], "unknown field (allowed: " + ", ".join(sorted(MANIFEST_KEYS)) + ")")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _fail("name", "2-40 characters, lowercase letters, digits and dashes, starting with a letter")

    if "version" in raw and not (_is_int(raw["version"]) and raw["version"] >= 1):
        raise _fail("version", "a positive integer; treg assigns it on publish, you may omit it")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not (1 <= len(summary.strip()) <= MAX_SUMMARY):
        raise _fail("summary", f"required, 1-{MAX_SUMMARY} characters; an agent reads this first")
    summary = summary.strip()

    writes = raw.get("writes", False)
    if not isinstance(writes, bool):
        raise _fail("writes", "true or false")

    inputs = _validate_inputs(raw.get("inputs", {}))
    uses = _validate_uses(raw.get("uses"), catalog_ids, own_tools, hub_ids)
    limits = _validate_limits(raw.get("limits", {}))
    price_micro = _validate_price(raw.get("price_usd", 0))

    has_steps, has_script = "steps" in raw, "script" in raw
    if has_steps == has_script:
        raise _fail("steps", "exactly one of `steps` or `script` (found "
                    + ("both" if has_steps else "neither") + ")")

    if has_script:
        kind = "script"
        script = raw["script"]
        if script != SCRIPT_FILE:
            raise _fail("script", f"must be {SCRIPT_FILE!r}, the file beside the manifest")
        steps = None
        output = _validate_output_script(raw.get("output"))
    else:
        kind = "steps"
        script = None
        steps = _validate_steps(raw["steps"], uses, limits["steps"])
        output = _validate_output_steps(raw.get("output"))

    manifest = {
        "name": name, "summary": summary, "writes": writes, "inputs": inputs, "uses": uses,
        "limits": limits, "price_usd": price_micro / 1_000_000,
        **({"steps": steps} if steps is not None else {"script": script}),
        "output": output,
    }
    return Validated(name=name, kind=kind, summary=summary, writes=writes, inputs=inputs,
                     uses=uses, limits=limits, price_micro=price_micro, steps=steps,
                     script=script, output=output, manifest=manifest)


def _validate_inputs(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise _fail("inputs", "an object of input name → spec")
    if len(raw) > MAX_INPUTS:
        raise _fail("inputs", f"at most {MAX_INPUTS} inputs")
    out: dict[str, dict[str, Any]] = {}
    for key, spec in raw.items():
        path = f"inputs.{key}"
        if not isinstance(key, str) or not _IDENT_RE.match(key):
            raise _fail(path, "input names are 1-32 characters, lowercase letters, digits and underscores")
        if not isinstance(spec, dict):
            raise _fail(path, "an object with at least `type`")
        if "required" in spec:
            raise _fail(f"{path}.required", "not a key: an input is required by having no `default`")
        extra = sorted(set(spec) - INPUT_KEYS)
        if extra:
            raise _fail(f"{path}.{extra[0]}", "unknown key (allowed: " + ", ".join(sorted(INPUT_KEYS)) + ")")
        typ = spec.get("type")
        if typ not in INPUT_TYPES:
            raise _fail(f"{path}.type", "one of " + ", ".join(INPUT_TYPES))
        secret = spec.get("secret", False)
        if not isinstance(secret, bool):
            raise _fail(f"{path}.secret", "true or false")
        if secret and typ != "string":
            raise _fail(f"{path}.secret", "a secret input must be a string")
        if secret and "default" in spec:
            raise _fail(f"{path}.default", "a secret input has no default")
        if not secret and "example" not in spec and "default" not in spec:
            raise _fail(path, "needs an `example` or a `default`, so the first run works")
        if typ == "int":
            if "max" not in spec:
                raise _fail(f"{path}.max", "an int input needs a `max`, or the runner cannot clamp it")
            if not _is_int(spec["max"]) or spec["max"] < 1:
                raise _fail(f"{path}.max", "a positive integer")
            if "min" in spec and (not _is_int(spec["min"]) or spec["min"] > spec["max"]):
                raise _fail(f"{path}.min", "an integer no larger than `max`")
            if "default" in spec and (not _is_int(spec["default"])
                                      or not spec.get("min", 0) <= spec["default"] <= spec["max"]):
                raise _fail(f"{path}.default", "an integer within min..max")
        elif "max" in spec or "min" in spec:
            raise _fail(f"{path}.max", "`min`/`max` apply to int inputs only")
        if "note" in spec and not isinstance(spec["note"], str):
            raise _fail(f"{path}.note", "a string")
        out[key] = {k: spec[k] for k in INPUT_KEYS if k in spec}
        out[key].setdefault("secret", False)
    return out


def _validate_uses(raw: Any, catalog_ids: set[str], own_tools: set[str],
                   hub_ids: frozenset[str] | set[str]) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise _fail("uses", "a non-empty list of the tools this recipe may call")
    if len(raw) > MAX_USES:
        raise _fail("uses", f"at most {MAX_USES} entries")
    seen: list[str] = []
    for i, entry in enumerate(raw):
        path = f"uses[{i}]"
        if not isinstance(entry, str) or not entry:
            raise _fail(path, "a catalog id or one of your own tool names")
        if entry in hub_ids:
            raise _fail(path, f"{entry!r} is a hub tool; a hub tool may not use a hub tool")
        if entry in catalog_ids or entry in own_tools:
            if entry not in seen:
                seen.append(entry)
            continue
        if _CATALOG_ID_RE.match(entry):
            raise _fail(path, f"{entry!r} is not a catalog id (catalog_search finds valid ids)")
        raise _fail(path, f"{entry!r} is not one of your team's tools (register it first: treg tool add)")
    return seen


def _validate_limits(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _fail("limits", "an object with steps, wall_s, cost_usd")
    extra = sorted(set(raw) - LIMIT_KEYS)
    if extra:
        raise _fail(f"limits.{extra[0]}", "unknown key (allowed: steps, wall_s, cost_usd)")
    steps = raw.get("steps", MAX_STEPS)
    if not _is_int(steps) or not 1 <= steps <= MAX_STEPS:
        raise _fail("limits.steps", f"an integer 1-{MAX_STEPS}")
    wall = raw.get("wall_s", MAX_WALL_S)
    if not _is_int(wall) or not 1 <= wall <= MAX_WALL_S:
        raise _fail("limits.wall_s", f"an integer 1-{MAX_WALL_S} seconds")
    cost = raw.get("cost_usd")
    if cost is not None and (not _is_number(cost) or not 0 < cost <= MAX_COST_USD):
        raise _fail("limits.cost_usd", f"a number above 0 and at most {MAX_COST_USD}")
    return {"steps": steps, "wall_s": wall, "cost_usd": cost}


def _validate_price(raw: Any) -> int:
    if not _is_number(raw) or raw < 0 or raw > MAX_PRICE_USD:
        raise _fail("price_usd", f"a number from 0 to {MAX_PRICE_USD} (dollars per successful run; 0 = free)")
    micro = round(float(raw) * 1_000_000)
    if abs(micro - float(raw) * 1_000_000) > 1e-6:
        raise _fail("price_usd", "at most 6 decimal places (treg prices in micro-dollars)")
    return int(micro)


def _validate_steps(raw: Any, uses: list[str], max_steps: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise _fail("steps", "a non-empty list of steps")
    if len(raw) > max_steps:
        raise _fail("steps", f"at most {max_steps} steps (limits.steps)")
    names: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, step in enumerate(raw):
        path = f"steps[{i}]"
        if not isinstance(step, dict):
            raise _fail(path, "an object with name, call, input")
        extra = sorted(set(step) - STEP_KEYS)
        if extra:
            raise _fail(f"{path}.{extra[0]}", "unknown key (allowed: " + ", ".join(sorted(STEP_KEYS)) + ")")
        name = step.get("name")
        if not isinstance(name, str) or not _IDENT_RE.match(name):
            raise _fail(f"{path}.name", "1-32 characters, lowercase letters, digits and underscores")
        if name in names:
            raise _fail(f"{path}.name", f"duplicate step name {name!r}")
        if name == "input":
            raise _fail(f"{path}.name", "`input` is reserved for the caller's inputs")
        names.add(name)
        call = step.get("call")
        if call not in uses:
            raise _fail(f"{path}.call", f"{call!r} is not in `uses`; every tool a step calls must be declared there")
        inp = step.get("input", {})
        if not isinstance(inp, dict):
            raise _fail(f"{path}.input", "an object of parameter → value or reference")
        for_each, as_name = step.get("for_each"), step.get("as")
        if (for_each is None) != (as_name is None):
            raise _fail(f"{path}.for_each", "`for_each` and `as` go together")
        if for_each is not None:
            if not isinstance(for_each, str) or not for_each.startswith("$"):
                raise _fail(f"{path}.for_each", "a reference to a list, like $people.contacts")
            if not isinstance(as_name, str) or not _IDENT_RE.match(as_name) or as_name in names or as_name == "input":
                raise _fail(f"{path}.as", "a fresh identifier for the current item")
        skip = step.get("skip_if_empty")
        if skip is not None and (not isinstance(skip, str) or not skip.startswith("$")):
            raise _fail(f"{path}.skip_if_empty", "a reference; the step is skipped when it is empty")
        if "writes" in step and not isinstance(step["writes"], bool):
            raise _fail(f"{path}.writes", "true or false")
        out.append({k: step[k] for k in STEP_KEYS if k in step} | {"input": inp})
    return out


def _validate_output_steps(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise _fail("output", "an object mapping each output field to a reference, like {\"leads\": \"$verify[]\"}")
    if len(raw) > MAX_OUTPUT_FIELDS:
        raise _fail("output", f"at most {MAX_OUTPUT_FIELDS} fields")
    for key, ref in raw.items():
        if not isinstance(key, str) or not _IDENT_RE.match(key):
            raise _fail(f"output.{key}", "field names are 1-32 characters, lowercase letters, digits and underscores")
        if not isinstance(ref, str) or not ref.startswith("$"):
            raise _fail(f"output.{key}", "a reference starting with $ (a literal value is not an output)")
    return dict(raw)


def _validate_output_script(raw: Any) -> dict[str, Any]:
    fields = raw.get("fields") if isinstance(raw, dict) else None
    if not isinstance(fields, list) or not fields:
        raise _fail("output.fields", "a non-empty list of the top-level field names the script returns")
    if len(fields) > MAX_OUTPUT_FIELDS:
        raise _fail("output.fields", f"at most {MAX_OUTPUT_FIELDS} fields")
    for f in fields:
        if not isinstance(f, str) or not _IDENT_RE.match(f):
            raise _fail("output.fields", f"{f!r}: field names are 1-32 characters, lowercase letters, digits and underscores")
    if set(raw) - {"fields"}:
        raise _fail("output", "for a script, only `fields` (the runner checks the returned object has them)")
    return {"fields": list(dict.fromkeys(fields))}


def validate_check(raw: Any, inputs: dict[str, dict[str, Any]], output_fields: list[str]) -> dict[str, Any]:
    """`check.json`: sample inputs and the least the answer must contain. Validated against the
    manifest so a check that can never pass is refused before anyone pays for it."""
    if not isinstance(raw, dict):
        raise _fail("check", "must be a JSON object with `inputs` and `fields`")
    extra = sorted(set(raw) - {"inputs", "fields", "min_rows"})
    if extra:
        raise _fail(f"check.{extra[0]}", "unknown key (allowed: inputs, fields, min_rows)")
    sample = raw.get("inputs")
    if not isinstance(sample, dict):
        raise _fail("check.inputs", "an object of input name → sample value")
    for key in sample:
        if key not in inputs:
            raise _fail(f"check.inputs.{key}", "not an input of this recipe")
    for key, spec in inputs.items():
        if "default" not in spec and key not in sample:
            raise _fail(f"check.inputs.{key}", "required input (no default) is missing from the sample")
    fields = raw.get("fields")
    if not isinstance(fields, list) or not fields:
        raise _fail("check.fields", "a non-empty list of output fields the check must find")
    for f in fields:
        if f not in output_fields:
            raise _fail("check.fields", f"{f!r} is not an output field of this recipe")
    min_rows = raw.get("min_rows", 0)
    if not _is_int(min_rows) or min_rows < 0:
        raise _fail("check.min_rows", "an integer ≥ 0")
    return {"inputs": sample, "fields": list(fields), "min_rows": min_rows}


def validate_readme(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _fail("readme", "required: what the tool does, for a human")
    if len(raw) > MAX_README:
        raise _fail("readme", f"at most {MAX_README} characters")
    return raw
