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
# One pricing rule per kind (docs/hub-pricing-decisions.md round 4, 2026-09-24):
#   steps  -> {"price_usd": N}: a fixed price per successful run; a JSON recipe has no code to count.
#   script -> {"max_price_usd": N}: the script prices itself with ctx.charge(usd, note) (a fee, per
#             result, a margin on ctx.call's cost_usd, a vendor's cost); N is the most one run may
#             charge. The caller sees N before the run, the hold is N, the run settles at the sum.
# Stored as {"mode": "per_call", "price_usd"} or {"mode": "charge", "max_price_usd"}.
PRICING_MODES = ("per_call", "charge")
MAX_CHARGES_PER_RUN = 20

INPUT_TYPES = ("string", "int", "float", "bool", "list", "object")
INPUT_KEYS = frozenset({"type", "default", "max", "min", "secret", "example", "note"})
STEP_KEYS = frozenset({"name", "call", "method", "input", "for_each", "as", "skip_if_empty", "writes", "allow_fail"})
LIMIT_KEYS = frozenset({"steps", "wall_s", "cost_usd"})
MANIFEST_KEYS = frozenset({
    "name", "version", "summary", "writes", "inputs", "uses", "limits", "price_usd", "pricing",
    "steps", "script", "output", "capability",
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
    price_micro: int          # a recipe's fixed price; 0 for a script
    pricing: dict[str, Any]   # the runner's micro view: mode, price_micro, max_charge_micro
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
    capabilities: frozenset[str] | set[str] | None = None,
) -> Validated:
    """Validate one manifest. `catalog_ids` and `own_tools` are the two universes `uses` may
    name; `hub_ids` are refused by name (a hub tool may not use a hub tool, depth one).
    `capabilities` is the catalog's job list an optional `capability` must name (None: not checked)."""
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
    has_steps, has_script = "steps" in raw, "script" in raw
    if has_steps == has_script:
        raise _fail("steps", "exactly one of `steps` or `script` (found "
                    + ("both" if has_steps else "neither") + ")")
    # A script may use nothing: a tool that serves its uploaded CSV (ctx.data) makes no call.
    uses = _validate_uses(raw.get("uses", []), catalog_ids, own_tools, hub_ids, allow_empty=has_script)
    limits = _validate_limits(raw.get("limits", {}))

    if has_script:
        kind = "script"
        script = raw["script"]
        if script != SCRIPT_FILE:
            raise _fail("script", f"must be {SCRIPT_FILE!r}, the file beside the manifest")
        steps = None
        output = _validate_output_script(raw.get("output"))
        output_fields = set(output["fields"])
    else:
        kind = "steps"
        script = None
        steps = _validate_steps(raw["steps"], uses, limits["steps"])
        output = _validate_output_steps(raw.get("output"))
        output_fields = set(output)

    pricing_public, pricing_micro = _validate_pricing(raw, is_script=has_script)
    # The job this tool does, in the catalog's words (docs/hub-listing-decisions.md round 3): only a
    # proposal. It takes effect when treg approves the listing, which puts the tool beside the
    # catalog providers of that job.
    capability = raw.get("capability")
    if capability is not None:
        if not isinstance(capability, str) or not _CATALOG_ID_RE.match(capability):
            raise _fail("capability", "a catalog capability id, like \"people.email.find\" (treg catalog search shows them)")
        if capabilities is not None and capability not in capabilities:
            raise _fail("capability", f"{capability!r} is not a catalog capability; pick the job your tool does from "
                        "`treg catalog search` results (their `capability` field)")
    price_micro = pricing_micro["price_micro"]

    manifest = {
        "name": name, "summary": summary, "writes": writes, "inputs": inputs, "uses": uses,
        "limits": limits, **({} if has_script else {"price_usd": price_micro / 1_000_000}), "pricing": pricing_public,
        **({"steps": steps} if steps is not None else {"script": script}),
        "output": output, **({"capability": capability} if capability else {}),
    }
    return Validated(name=name, kind=kind, summary=summary, writes=writes, inputs=inputs,
                     uses=uses, limits=limits, price_micro=price_micro, pricing=pricing_micro,
                     steps=steps, script=script, output=output, manifest=manifest)


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
                   hub_ids: frozenset[str] | set[str], allow_empty: bool = False) -> list[str]:
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise _fail("uses", "a non-empty list of the tools this recipe may call")
    # The runner tells an own tool from a catalog id by the dot. An own tool NAMED like a catalog
    # id would run as the caller and could later resolve to a hub tool of that name: a nested
    # run with an undisclosed price (8.1 review). Refused here, by name.
    for i, u in enumerate(raw):
        if isinstance(u, str) and u in own_tools and "." in u:
            raise _fail(f"uses[{i}]", f"own tool {u!r} is named like a catalog id; rename it without a dot")
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
    import math
    if _is_number(raw) and not math.isfinite(float(raw)):
        raise _fail("price_usd", "a finite number")
    if not _is_number(raw) or raw < 0 or raw > MAX_PRICE_USD:
        raise _fail("price_usd", f"a number from 0 to {MAX_PRICE_USD} (dollars per successful run; 0 = free)")
    micro = round(float(raw) * 1_000_000)
    if abs(micro - float(raw) * 1_000_000) > 1e-6:
        raise _fail("price_usd", "at most 6 decimal places (treg prices in micro-dollars)")
    return int(micro)


def _money_micro(field: str, raw: Any, *, allow_zero: bool) -> int:
    """A dollar amount in the range 0..MAX_PRICE_USD, at most 6 decimals, as a micro-dollar int."""
    import math
    if not _is_number(raw) or not math.isfinite(float(raw)):
        raise _fail(field, "a finite number")
    if raw < 0 or raw > MAX_PRICE_USD:
        raise _fail(field, f"a number from 0 to {MAX_PRICE_USD} (dollars)")
    micro = round(float(raw) * 1_000_000)
    if abs(micro - float(raw) * 1_000_000) > 1e-6:
        raise _fail(field, "at most 6 decimal places (treg prices in micro-dollars)")
    if not allow_zero and micro <= 0:
        raise _fail(field, "a number above 0")
    return int(micro)


def _validate_pricing(raw: dict[str, Any], is_script: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (public, micro): the stored `pricing` block and the runner's micro-dollar view. A
    steps recipe has one fixed price (`price_usd`, top level or inside `pricing`; none = free). A
    script declares `max_price_usd`, the most its ctx.charge lines may total in one run; a script
    with no `pricing` is free and ctx.charge is refused."""
    block = raw.get("pricing")
    if block is not None and not isinstance(block, dict):
        raise _fail("pricing", "an object: {\"price_usd\": N} for a JSON recipe, {\"max_price_usd\": N} for a script")
    block = dict(block or {})
    stored = block.pop("mode", None) if block.get("mode") in PRICING_MODES else None
    # A stored recipe keeps `price_usd` at the top level beside its block (the same number); a
    # maker's file says it once.
    if "price_usd" in raw and block and not (stored == "per_call" and raw["price_usd"] == block.get("price_usd")):
        raise _fail("price_usd", "not a top-level field when `pricing` is present; put it inside `pricing`")
    if is_script:
        if "price_usd" in raw or "price_usd" in block:
            raise _fail("pricing.price_usd", "a script prices itself with ctx.charge(usd, note); declare "
                        "`pricing.max_price_usd`, the most one run may charge")
        bad = sorted(set(block) - {"max_price_usd"})
        if bad:
            raise _fail(f"pricing.{bad[0]}", "a script's pricing is only `max_price_usd`; ctx.charge(usd, note) "
                        "in run.js sets the amount (a fee, per result, a margin, a vendor's cost)")
        if "max_price_usd" not in block:
            return {"mode": "charge", "max_price_usd": 0}, _micro_block("charge")
        cap = _money_micro("pricing.max_price_usd", block["max_price_usd"], allow_zero=False)
        return {"mode": "charge", "max_price_usd": cap / 1_000_000}, _micro_block("charge", max_charge_micro=cap)
    bad = sorted(set(block) - {"price_usd"})
    if bad:
        raise _fail(f"pricing.{bad[0]}", "a JSON recipe's pricing is only `price_usd`, a fixed price per "
                    "successful run; a variable price needs a script and ctx.charge")
    micro = (_money_micro("pricing.price_usd", block["price_usd"], allow_zero=True) if "price_usd" in block
             else _validate_price(raw.get("price_usd", 0)))
    return {"mode": "per_call", "price_usd": micro / 1_000_000}, _micro_block("per_call", price_micro=micro)


def _micro_block(mode: str, *, price_micro: int = 0, max_charge_micro: int = 0) -> dict[str, Any]:
    """The runner's view of a pricing block, in micro-dollars: `price_micro` is a JSON recipe's
    fixed price, `max_charge_micro` the most a script's ctx.charge lines may total in one run."""
    return {"mode": mode, "price_micro": price_micro, "max_charge_micro": max_charge_micro}


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
        # A catalog id as is, or one of the maker's own tools as `<tool>/<path>` (the tool name
        # must be in `uses`; the path is the upstream path under its base url).
        target = call.split("/", 1)[0] if isinstance(call, str) else None
        if not isinstance(call, str) or target not in uses:
            raise _fail(f"{path}.call", f"{call!r} is not in `uses`; every tool a step calls must be declared there")
        if "/" in call and "." in target:
            raise _fail(f"{path}.call", f"{target!r} is a catalog id; it takes no path (a path belongs to one of your own tools)")
        method = step.get("method", None)
        if method is not None and (not isinstance(method, str)
                                   or method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE")):
            raise _fail(f"{path}.method", "one of GET, POST, PUT, PATCH, DELETE")
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
        if "allow_fail" in step and not isinstance(step["allow_fail"], bool):
            raise _fail(f"{path}.allow_fail", "true or false")
        for where, value in (("input", inp), ("for_each", for_each), ("skip_if_empty", skip)):
            bad = _bad_ref(value)
            if bad is not None:
                raise _fail(f"{path}.{where}", f"{bad!r} is not a valid reference (see the reference language)")
        out.append({k: step[k] for k in STEP_KEYS if k in step} | {"input": inp})
    from . import graph as _graph   # local: graph imports ManifestError from here
    _graph.build(out)               # unknown-step references and cycles are refused at publish
    return out


def own_names(uses: list[str]) -> set[str]:
    """The entries of `uses` that are a team's own tool names (no dot) rather than catalog ids."""
    return {u for u in uses if "." not in u}


def pricing_micro(manifest: dict[str, Any]) -> dict[str, Any]:
    """The stored `pricing` block as the runner's micro-dollar view (see `_micro_block`). A
    manifest from before the block (only `price_usd`) reads as a fixed price."""
    p = stored_pricing(manifest)

    def m(x: Any) -> int:
        return int(round(float(x or 0) * 1_000_000))

    if p["mode"] == "charge":
        return _micro_block("charge", max_charge_micro=m(p.get("max_price_usd")))
    return _micro_block("per_call", price_micro=m(p.get("price_usd")))


def stored_pricing(manifest: dict[str, Any]) -> dict[str, Any]:
    """The pricing block of a stored manifest: {"mode": "per_call", "price_usd"} or
    {"mode": "charge", "max_price_usd"}. A manifest with only a top-level `price_usd` is per_call."""
    p = manifest.get("pricing") or {}
    if p.get("mode") == "charge":
        return {"mode": "charge", "max_price_usd": float(p.get("max_price_usd") or 0)}
    return {"mode": "per_call", "price_usd": float(p.get("price_usd", manifest.get("price_usd", 0)) or 0)}


def price_label(manifest: dict[str, Any]) -> str:
    """The maker's price in the maker's words: "$0.15 a run", "up to $0.05 a run", "free". What
    the maker earns on a successful run; the provider fees (the metered steps) are billed to the
    caller on top. A caller reads `range_label` instead."""
    p = stored_pricing(manifest)
    if p["mode"] == "charge":
        return f"up to ${p['max_price_usd']:.6g} a run" if p["max_price_usd"] else "free"
    return f"${p['price_usd']:.6g} a run" if p["price_usd"] else "free"


def fees_label(manifest: dict[str, Any], rng: dict[str, Any] | None = None) -> str:
    """The provider-fee half of a hub tool's price line, with its limit: "+ provider fees up to $1 a
    run" (the tool's `limits.cost_usd`, else the runner's $1.00 default; a caller's
    X-Treg-Run-Max-Cost lowers it). Empty when the tool calls only its maker's own tools. Found in
    hub simulation run 2: a maker set the limit to $100 and buyers read only "+ provider fees"."""
    if not any("." in u for u in manifest.get("uses", [])):
        return ""
    cap = (manifest.get("limits") or {}).get("cost_usd") or 1.0
    # The limit alone read as the likely cost (hub simulation run 3); what runs have paid comes first.
    lo, hi = (rng or {}).get("fees_low_micro"), (rng or {}).get("fees_high_micro")
    if lo is None or hi is None:
        return f" + provider fees (at most ${float(cap):.6g} a run)"
    seen = f"${lo / 1e6:.6g}" if lo == hi else f"${lo / 1e6:.6g}–${hi / 1e6:.6g}"
    return f" + provider fees (about {seen} so far, at most ${float(cap):.6g} a run)"


PAY_NOTE = ("a failed run pays no seller price; the provider fees of the steps that ran are still "
            "charged, and an empty answer pays no seller price")


def seller_part_micro(manifest: dict[str, Any], observed_price_micro: int | None, charged_micro: int = 0) -> int:
    """What the seller earns on one run, for the price range: the observed price when a caller
    paid one (a run by another team), else what a caller would have paid (the maker's own runs and
    the checks pay no seller price): the fixed price, or the sum of the run's ctx.charge lines."""
    if observed_price_micro is not None:
        return int(observed_price_micro)
    p = pricing_micro(manifest)
    return charged_micro if p["mode"] == "charge" else p["price_micro"]


def range_label(manifest: dict[str, Any], low_micro: int | None, high_micro: int | None) -> str:
    """The headline price a caller reads: what one successful run has actually cost, steps and
    seller price together, over recent runs (docs/hub-pricing-decisions.md). One number when
    every run cost the same, a range otherwise, and before any run the declared price with the
    steps unknown."""
    p = stored_pricing(manifest)
    fees = " + provider fees" if any("." in u for u in manifest.get("uses", [])) else ""
    if low_micro is None or high_micro is None:
        if p["mode"] == "charge":
            return (f"up to ${p['max_price_usd']:.6g}/run" if p["max_price_usd"] else "free") + fees
        return (f"${p['price_usd']:.6g}/run" if p["price_usd"] else "free") + fees
    lo, hi = low_micro / 1_000_000, high_micro / 1_000_000
    span = (f"${lo:.6g}/run" if low_micro else "free") if low_micro == high_micro else f"${lo:.6g}–${hi:.6g}/run"
    # A script's recent runs may all have charged little; the cap says what a run CAN charge (found in
    # hub simulation run 1: search showed $0.0103 while a good result cost $0.0163).
    if p["mode"] == "charge" and p["max_price_usd"]:
        return f"{span} so far · seller up to ${p['max_price_usd']:.6g} a run"
    return span


def _bad_ref(value: Any) -> str | None:
    """The first reference-looking token that does not parse, or None."""
    from . import refs
    if isinstance(value, str):
        for m in re.finditer(r"\$[A-Za-z0-9_.\[\]]+", value):
            token = m.group(0)
            try:
                refs.parse(token)
            except refs.RefError:
                # a template may end a reference at punctuation; accept if a prefix parses
                if not any(refs.find(token)):
                    return token
        return None
    if isinstance(value, dict):
        for v in value.values():
            b = _bad_ref(v)
            if b is not None:
                return b
    if isinstance(value, list):
        for v in value:
            b = _bad_ref(v)
            if b is not None:
                return b
    return None


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
        from . import refs
        try:
            refs.parse(ref)
        except refs.RefError:
            raise _fail(f"output.{key}", f"{ref!r} is not a valid reference") from None
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


MAX_CHECK_CASES = 5


def validate_check(raw: Any, inputs: dict[str, dict[str, Any]], output_fields: list[str]) -> dict[str, Any]:
    """`check.json`: sample inputs and the least the answer must contain, or `{"cases": [...]}`,
    up to MAX_CHECK_CASES of those, each run at publish (hub simulation run 3: one sample could not
    reach both the verified and the risky path of an email tool). The first case's keys stay at the
    top level, where the scheduled check reads them."""
    if isinstance(raw, dict) and "cases" in raw:
        if set(raw) != {"cases"}:
            raise _fail("check", "with `cases`, nothing else at the top level")
        cases = raw["cases"]
        if not isinstance(cases, list) or not (1 <= len(cases) <= MAX_CHECK_CASES):
            raise _fail("check.cases", f"a list of 1-{MAX_CHECK_CASES} checks, each with `inputs` and `fields`")
        done = []
        for i, case in enumerate(cases):
            try:
                done.append(_validate_one_check(case, inputs, output_fields))
            except ManifestError as exc:
                raise _fail(exc.field.replace("check", f"check.cases[{i}]", 1), exc.rule) from None
        return {**done[0], "cases": done}
    return _validate_one_check(raw, inputs, output_fields)


def _validate_one_check(raw: Any, inputs: dict[str, dict[str, Any]], output_fields: list[str]) -> dict[str, Any]:
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


# A hub tool beside catalog providers needs a success rate to compare, and a new one has no runs
# (docs/hub-listing-decisions.md round 3). It starts at SEED_OK_RATE counted as SEED_RUNS runs; each
# run by another team moves it, so after ~20 real runs the seed barely counts. Until then the number
# is marked `estimated`.
SEED_OK_RATE = 0.9
SEED_RUNS = 5
ESTIMATED_UNDER = 20


def seeded_observed(ok: int, runs: int) -> dict[str, Any]:
    """The `observed` block of a hub tool shown beside providers: the seeded success rate, the
    real run count, and whether the number is still mostly the seed."""
    rate = (SEED_OK_RATE * SEED_RUNS + ok) / (SEED_RUNS + runs)
    return {"samples": runs, "decided": runs, "ok_rate": round(rate, 4), "estimated": runs < ESTIMATED_UNDER,
            "seed": {"ok_rate": SEED_OK_RATE, "runs": SEED_RUNS}}

