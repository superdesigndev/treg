"""Declarative settlement basis shared by request and terminal-response paths."""

from __future__ import annotations

import json
import math
from typing import Any

from ..asynctasks import json_path as _path


def _micro(value: float, unit_micro: int) -> int:
    raw = round(float(value) * int(unit_micro), 9)
    whole = int(raw)
    return whole + 1 if raw > whole else whole


def request_evidence(
    query_items: list[tuple[str, str]], body: bytes, *, path_names: set[str] | None = None,
) -> dict:
    """Return the provider-facing request values needed to replay a declarative price table."""
    query: dict[str, object] = {}
    for name, value in query_items:
        previous = query.get(name)
        if previous is None:
            query[name] = value
        elif isinstance(previous, list):
            previous.append(value)
        else:
            query[name] = [previous, value]
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    path_names = path_names or set()
    path = {name: query.pop(name) for name in tuple(query) if name in path_names}
    return {"queryParams": query, "pathParams": path, "body": parsed}


def _with_defaults(request: dict, input_schema: dict) -> dict:
    out = {"queryParams": dict(request.get("queryParams") or {}),
           "pathParams": dict(request.get("pathParams") or {}),
           "body": request.get("body") if isinstance(request.get("body"), dict) else {}}
    out["body"] = dict(out["body"])

    def apply(block: dict, values: dict) -> None:
        for name, spec in block.items():
            if not isinstance(spec, dict):
                continue
            if name not in values and "default" in spec:
                values[name] = spec["default"]
            if name in values and isinstance(values[name], str):
                try:
                    if spec.get("type") == "integer":
                        values[name] = int(values[name])
                    elif spec.get("type") == "number":
                        values[name] = float(values[name])
                    elif spec.get("type") == "boolean" and values[name].lower() in ("true", "false"):
                        values[name] = values[name].lower() == "true"
                except ValueError:
                    pass
            nested = spec.get("properties")
            if isinstance(nested, dict) and isinstance(values.get(name), dict):
                apply(nested, values[name])

    for location in ("queryParams", "pathParams", "body"):
        block = input_schema.get(location)
        if isinstance(block, dict):
            if isinstance(block.get("properties"), dict):
                block = block["properties"]
            apply(block, out[location])
    return out


def _spec_for(input_schema: dict, dotted: str) -> dict:
    """The input field declaration behind a location-qualified dotted path (`body.input.duration`)."""
    parts = dotted.split(".")
    block = input_schema.get(parts[0]) if isinstance(input_schema, dict) else None
    if isinstance(block, dict) and isinstance(block.get("properties"), dict):
        block = block["properties"]
    spec: dict = {}
    for name in parts[1:]:
        if not isinstance(block, dict):
            return {}
        spec = block.get(name) if isinstance(block.get(name), dict) else {}
        block = spec.get("properties")
    return spec


def _bounded_multiplier(value: object, spec: dict) -> float | None:
    """A `times` value the table may multiply by: a finite number inside the field's declared
    range (positive when no minimum is declared). Anything else is None - the row does not price
    that request and the explicit fallback does, so a caller cannot reserve zero with
    `duration: 0` or bill past the validated ceiling with `duration: 100`."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    low, high = spec.get("min"), spec.get("max")
    if isinstance(low, (int, float)) and not isinstance(low, bool) and value < low:
        return None
    if isinstance(high, (int, float)) and not isinstance(high, bool) and value > high:
        return None
    if not isinstance(low, (int, float)) and value <= 0:
        return None
    return float(value)


def table_amount_micro(cost: dict, request: dict, input_schema: dict, unit_micro: int) -> int:
    """Evaluate the frozen first-match table, falling back to its explicit global upper bound."""
    values = _with_defaults(request, input_schema)
    for row in cost.get("table") or []:
        when = row.get("when") or {}
        if all(_path(values, field) == expected for field, expected in when.items()):
            if not row.get("times"):
                return _micro(float(row["value"]), unit_micro)
            multiplier = _bounded_multiplier(
                _path(values, row["times"]), _spec_for(input_schema, str(row["times"])))
            if multiplier is None:
                break
            return _micro(float(row["value"]) * multiplier, unit_micro)
    return _micro(float(cost["fallback"]["value"]), unit_micro)


def derive_basis(
    cost: dict, *, request: dict, input_schema: dict, unit_micro: int,
    terminal: bool, response_estimate_micro: int = 0,
) -> dict:
    """Freeze when and how a reserved call will settle using catalog data only."""
    if cost.get("table") or cost.get("settle") == "usage":
        fallback = _micro(float(cost["fallback"]["value"]), unit_micro)
        if cost.get("settle") == "usage":
            # Reserve what the rate card says THIS request costs, not the matrix ceiling: the
            # ceiling made a $0.05 call demand a $6 balance. The provider's reported cost settles
            # and may exceed the reserve; the ledger takes the difference from the balance, and the
            # next reserve is the gate. reconcile lists every overrun (`async_task_settlement`).
            amount = {"kind": "usage", **dict(cost["usage"])}
            reserve = table_amount_micro(cost, request, input_schema, unit_micro)
        else:
            amount = {"kind": "table", "cost": cost, "input": input_schema,
                      "request": request, "unit_micro": unit_micro}
            reserve = table_amount_micro(cost, request, input_schema, unit_micro)
        return {"when": "terminal" if terminal else "response", "amount": amount,
                "fallback_micro": fallback,
                "reserve_micro": reserve}
    return {
        "when": "response",
        "amount": {"kind": "observed"},
        "fallback_micro": max(0, int(response_estimate_micro)),
        "reserve_micro": max(0, int(response_estimate_micro)),
    }


def usage_evidence(basis: dict, evidence: dict[str, Any]) -> float | None:
    """The provider-reported usage figure a `usage` basis settles on, or None when the terminal
    response does not carry a usable one."""
    amount = basis.get("amount") or {}
    value = _path(evidence.get("terminal"), str(amount.get("path") or ""))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
            and value >= 0:
        return float(value)
    return None


def settle(basis: dict, evidence: dict[str, Any]) -> int:
    """Resolve one frozen basis to raw integer micro-USD without moving ledger money."""
    amount = basis.get("amount") or {}
    kind = amount.get("kind")
    if kind == "table":
        return table_amount_micro(
            amount["cost"], amount["request"], amount["input"], int(amount["unit_micro"]))
    if kind == "usage":
        value = usage_evidence(basis, evidence)
        if value is not None:
            if amount.get("unit") == "usd":
                return _micro(float(value), 1_000_000)
        # A successful task whose terminal response no longer carries the usage field: the caller
        # got their result, so they pay - the reserve (the rate-card estimate), not the ceiling.
        # The worker marks the row for review; the provider changed its shape.
        return max(0, int(basis.get("reserve_micro") or 0))
    if kind == "observed":
        observed = evidence.get("observed_micro")
        if isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0:
            return observed
    return max(0, int(basis.get("fallback_micro") or 0))
