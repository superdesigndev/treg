"""What a call to a v2 row holds and settles at: its `price`, `reserve`, `settle` and `bill`, in the
settlement order of catalog schema v2. Pure: no session, no network; reads only the catalog."""

from __future__ import annotations

import json
import math
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

from ...domain import plugins
from ...domain.catalog import outcome as outcomes
from ...domain.catalog.schema.paths import holds, leaf_paths, parse_path, relative, select
from ...domain.money import settlement as settlement_basis
from .types import ResolutionFailed

_MICRO = Decimal(1_000_000)


def _micro_up(amount: Decimal) -> int:
    return max(0, int(amount.to_integral_value(rounding=ROUND_CEILING)))


def _rate(cat, provider: str, currency: str, unit: str | None = None) -> float | None:
    """USD of one unit of `currency`: a currency's fx rate, the provider's credit or meter rate."""
    if currency in ("USD", "usd"):
        return 1.0
    return (cat.credit_rates.get(provider) if currency == "credit"
            else cat.unit_rates.get(provider, {}).get(str(unit or "")) if currency == "unit"
            else cat.fx.get(currency))


def currency_micro(cat, provider: str, currency: str, unit: str | None = None) -> Decimal | None:
    """Micro-USD of one unit of `currency`, frozen when the call is resolved; None when unknown."""
    rate = _rate(cat, provider, currency, unit)
    return None if rate is None else Decimal(str(rate)) * _MICRO


def _meter(unit: str) -> tuple[str, str | None]:
    """The currency a reported or usage `unit` names: usd, the provider's credit, or a meter."""
    return ("usd", None) if unit == "usd" else ("credit", None) if unit == "credit" else ("unit", unit)


def request_document(ep: dict, request_data: dict, *, defaults: bool) -> dict:
    """The caller's request as v2 request paths read it: `body`, and `query` holding the path
    parameters too. With `defaults`, declared input defaults are applied and values are coerced by
    their declared type, as price tables compare them; a body that is not a JSON object is as sent."""
    values = settlement_basis._with_defaults(request_data, ep.get("input") or {}) if defaults else request_data
    body = request_data.get("body")
    return {"body": values.get("body") if isinstance(body, dict) or not body else body,
            "query": {**(values.get("queryParams") or {}), **(values.get("pathParams") or {})}}


def _input_spec(input_schema: dict, path: str) -> dict:
    location, _, rest = path.partition(".")
    for block in (("body",) if location == "body" else ("queryParams", "pathParams")):
        spec = settlement_basis._spec_for(input_schema, f"{block}.{rest}")
        if spec:
            return spec
    return {}


def _block(price: dict, request: dict, input_schema: dict, rate: Decimal) -> Decimal:
    """Micro-USD of `per` units: the value, or the first table row whose conditions hold."""
    if price.get("table") is None:
        return Decimal(price["value"]) * rate
    for row in price["table"]:
        if not holds(row["when"], request):
            continue
        if not row.get("times"):
            return Decimal(row["value"]) * rate
        found = select(row["times"], request)
        times = settlement_basis._bounded_multiplier(found[0] if found else None,
                                                     _input_spec(input_schema, row["times"]))
        if times is None:
            break
        return Decimal(row["value"]) * Decimal(str(times)) * rate
    return Decimal(price["fallback"]["value"]) * rate


def amount(terms: dict, units: int | float | Decimal) -> int:
    """What `units` cost at the frozen unit price: whole `per` blocks when the price rounds up."""
    per = Decimal(terms["per"])
    blocks = Decimal(str(units)) / per
    if terms["round"] == "up":
        blocks = blocks.to_integral_value(rounding=ROUND_CEILING)
    return _micro_up(blocks * Decimal(terms["block_micro"]))


def _priced(terms: dict, quantity: object) -> int | None:
    """A plugin's quantity priced: a number at the unit price, or `{rate: quantity}` at the frozen
    rates. None when the quantity is not one."""
    if terms.get("rates_micro") is not None:
        if not isinstance(quantity, dict):
            return None
        total = Decimal(0)
        for name, count in quantity.items():
            if name not in terms["rates_micro"] or not _count(count):
                return None
            total += Decimal(str(count)) * Decimal(terms["rates_micro"][name])
        return _micro_up(total)
    return amount(terms, quantity) if _count(quantity) else None


def _count(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and value >= 0)


def _rows(rule: dict, request: dict) -> int | None:
    """Rows the caller asks for, clamped to [1, max]; the default when the request lacks them, and
    None when there is no default to fall back on."""
    top = int(rule["max"])
    found = select(rule["param"], request) if rule.get("param") else []
    value = found[0] if found else None
    raw = str(value).strip() if value is not None and not isinstance(value, list) else ""
    if raw.isdigit():
        return top if len(raw) > len(str(top)) else max(1, min(int(raw), top))
    return int(rule["default"]["value"]) if rule.get("default") else None


def require_rows(ep: dict, request_data: dict) -> None:
    """On treg's key, a `reserve.rows` with no default refuses a request without its parameter
    before anything is held."""
    rule = (ep.get("reserve") or {}).get("rows")
    if rule and _rows(rule, request_document(ep, request_data, defaults=False)) is None:
        raise ResolutionFailed("catalog_parameter_invalid", status_code=400, detail={
            "error": "catalog_parameter_invalid", "endpoint_id": ep["id"], "parameter": rule["param"],
            "message": f"{ep['id']} needs {rule['param']}: a whole number from 1 to {rule['max']}"})


def _entities(rule: dict, request: dict) -> int:
    paths = rule["from"] if isinstance(rule["from"], list) else [rule["from"]]
    total = 0
    for path in paths:
        for value in select(path, request):
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str):
                    parts = item.split(",") if path.startswith("query.") else [item]
                    total += sum(1 for part in parts if part.strip())
                elif item is not None:
                    total += 1
    return max(1, min(total, int(rule.get("max") or 10_000)))


def plugin_request(ep: dict, request_data: dict) -> plugins.Request:
    raw = request_document(ep, request_data, defaults=False)
    return plugins.Request(method=ep.get("method") or "GET", query=raw["query"], body=raw["body"])


def price(ep: dict, cat, request_data: dict) -> tuple[int, int, int, dict]:
    """(hold, unit, reported-credit unit, terms) for a call to a v2 row, in RAW micro-USD.

    The hold is the reserved units at the matched unit price. `unit` is one `price.value` (a
    table's fallback), the scalar the views show. `terms` is everything the settle reads, frozen
    here so the answer is billed at the rates in force when the call was resolved."""
    price_ = ep.get("price") or {}
    provider = ep["provider"]
    rate = currency_micro(cat, provider, price_.get("currency", "USD"), price_.get("unit"))
    if rate is None or (price_.get("value") is None and price_.get("table") is None and not price_.get("rates")):
        # Unpriced or an unknown rate: nothing to hold, and treg's key never serves it.
        rate = Decimal(0)
    request = request_document(ep, request_data, defaults=True)
    raw = request_document(ep, request_data, defaults=False)
    rates = price_.get("rates")
    terms = {
        "provider": provider, "method": ep.get("method") or "GET",
        "per": int(price_.get("per", 1)), "round": price_.get("round"),
        "block_micro": None if rates else str(_block(price_, request, ep.get("input") or {}, rate)
                                              if rate else Decimal(0)),
        "rates_micro": {name: str(Decimal(v) * rate) for name, v in rates.items()} if rates else None,
        "reported_micro": None,
        **{key: ep.get(key) for key in ("expect", "error", "miss", "bill", "settle")},
    }
    reported = (ep.get("settle") or {}).get("reported")
    if reported is not None:
        unit_rate = currency_micro(cat, provider, *_meter(reported["unit"]))
        terms["reported_micro"] = None if unit_rate is None else str(unit_rate)
    if _reads_request(ep):
        terms.update(request=request_data, input=ep.get("input") or {})
    reserve = ep.get("reserve") or {}
    if "plugin" in reserve:
        quantity = plugins.call(reserve["plugin"], provider, plugin_request(ep, request_data))
        held = _priced(terms, quantity)
        if held is None:
            raise ResolutionFailed("catalog_price_invalid", status_code=503, detail={
                "error": "catalog_price_invalid", "endpoint_id": ep["id"],
                "message": f"{ep['id']} could not price this request"})
        hold = min(held, _micro_up(Decimal(reserve["max"]) * rate))
    else:
        units = ((_rows(reserve["rows"], raw) or 1) if "rows" in reserve
                 else _entities(reserve["entities"], raw) if "entities" in reserve else 1)
        hold = amount(terms, units)
    scalar = (price_.get("value") if price_.get("value") is not None
              else (price_.get("fallback") or {}).get("value"))
    unit = _micro_up(Decimal(scalar) * rate) if scalar is not None else 0
    credit = cat.credit_rates.get(provider)
    credit_unit = (settlement_basis._micro(credit, 1_000_000)
                   if credit and "credit" in (price_.get("currency"), (reported or {}).get("unit")) else 0)
    return hold, unit, credit_unit, terms


def _reads_request(ep: dict) -> bool:
    free_when = (ep.get("bill") or {}).get("free_when")
    named = [ep.get(key) or {} for key in ("reserve", "settle", "error", "miss")]
    return (free_when is not None and any(not p.startswith("response") for p in leaf_paths(free_when))) \
        or any("plugin" in rule for rule in named)


def basis(cat, ep: dict, hold: int, *, terminal: bool) -> dict:
    """The frozen basis in the vocabulary both spellings share: `usage` freezes the meters a usage
    settle reads, as today's spelling does; every other v2 settle is `observed` (the answer decides,
    else the hold)."""
    usage = (ep.get("settle") or {}).get("usage")
    when = "terminal" if terminal else "response"
    if usage is None:
        return {"when": when, "amount": {"kind": "observed"}, "fallback_micro": hold, "reserve_micro": hold}
    amount_ = {"kind": "usage", "unit": usage["unit"], **(
        {"terms": [{"path": relative(t["path"]), "rate": float(t["rate"])} for t in usage["terms"]]}
        if "terms" in usage else {"path": relative(usage["path"])})}
    meter = _rate(cat, ep["provider"], *_meter(usage["unit"]))
    if meter:
        amount_["unit_micro"] = settlement_basis._micro(meter, 1_000_000)
    price_ = ep.get("price") or {}
    fallback = (price_.get("fallback") or {}).get("value")
    rate = _rate(cat, ep["provider"], price_.get("currency", "USD"), price_.get("unit"))
    fallback_micro = hold if fallback is None or rate is None else settlement_basis._micro(
        float(fallback), settlement_basis._micro(rate, 1_000_000))
    return {"when": when, "amount": amount_, "fallback_micro": fallback_micro, "reserve_micro": hold}


def document(body: bytes) -> object:
    try:
        return json.loads(body) if body else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def _last_line(raw: bytes) -> object:
    """The last line of an NDJSON stream, which a usage meter reads when it closes with a summary."""
    last = next((line for line in reversed(raw.splitlines()) if line.strip()), b"")
    return document(last)


def reported(terms: dict, doc: object, headers=None) -> int | None:
    """The provider's own charge for this answer after `scale`, or None when it is unreadable."""
    rule = (terms.get("settle") or {}).get("reported")
    if rule is None or terms.get("reported_micro") is None:
        return None
    if "header" in rule:
        raw = headers.get(rule["header"]) if headers is not None else None
    else:
        found = select(rule["path"], {"response": doc})
        raw = found[0] if found else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        figure = Decimal(str(raw).strip()) * Decimal(str(rule.get("scale", 1)))
    except (InvalidOperation, ValueError):
        return None
    if not figure.is_finite() or figure < 0:
        return None
    return int((figure * Decimal(terms["reported_micro"])).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _evidence(terms: dict, hold: int, doc: object, request: plugins.Request | None,
              answer: plugins.Answer | None) -> int | None:
    """What a `count` or `plugin` settle reads off this answer, capped at the hold."""
    found = _uncapped(terms, doc, request, answer)
    return None if found is None else min(found, hold)


def _uncapped(terms: dict, doc: object, request: plugins.Request | None,
              answer: plugins.Answer | None) -> int | None:
    settle = terms.get("settle") or {}
    if "count" in settle:
        path = settle["count"]["path"]
        found = select(path, {"response": doc})
        if parse_path(path).many:
            if not _lists_readable(path, doc):
                return None
        elif not found or not isinstance(found[0], list):
            return None
        else:
            found = found[0]
        return amount(terms, sum(item is not None for item in found))
    if "plugin" in settle:
        return _priced(terms, plugins.call(settle["plugin"], terms["provider"], request, answer))
    return None


def _lists_readable(path: str, doc: object) -> bool:
    """Whether every list a many-valued path iterates is there and is a list."""
    parsed, values, gap = parse_path(path), [doc], False
    steps = [(None, parsed.filter)] if parsed.filter is not None else []
    steps += [(step, step.filter) for step in parsed.steps]
    for step, keep in steps:
        if step is not None and not step.every:
            if step.index is not None:
                found = [v[step.index] for v in values if isinstance(v, list) and step.index < len(v)]
            else:
                found = [v[step.name] for v in values if isinstance(v, dict) and step.name in v]
            gap, values = gap or len(found) < len(values), found
        if step is not None and step.every or keep is not None:
            if gap or not all(isinstance(v, list) for v in values):
                return False
            values = [item for v in values for item in v]
            if keep is not None:
                key, value = keep
                values = [item for item in values if isinstance(item, dict) and str(item.get(key)) == value]
    return True


def _usage(basis_: dict, doc: object) -> int | None:
    value = settlement_basis.usage_evidence(basis_, {"terminal": doc})
    per_unit = (basis_.get("amount") or {}).get("unit_micro")
    if value is None or not isinstance(per_unit, int) or per_unit <= 0:
        return None
    return settlement_basis._micro(value, per_unit)


def _settled(terms: dict, basis_: dict, hold: int, doc: object, raw: bytes, evidence: int | None) -> int:
    """Step 5: what a charged answer costs by its `settle` form. A usage meter may exceed the hold;
    missing evidence settles at the hold."""
    if "usage" in (terms.get("settle") or {}):
        used = _usage(basis_, doc if doc is not None else _last_line(raw))
        return hold if used is None else used
    return hold if evidence is None else evidence


def _figure(terms: dict, doc: object, headers, submitted: int | None) -> int | None:
    """A readable reported charge, else what the provider reported on an async submission."""
    figure = reported(terms, doc, headers)
    return submitted if figure is None and (terms.get("settle") or {}).get("reported") else figure


def charge(terms: dict, basis_: dict, hold: int, status: int, doc: object, *, raw: bytes = b"",
           headers=None, request_data: dict | None = None,
           submitted: int | None = None) -> tuple[int | None, int]:
    """(observed, actual) for a 2xx answer, by the settlement order from step 2: `free_when`, a
    readable reported charge, then the outcome with `bill`, then the `settle` form. `observed` is
    what the answer itself said it cost: a free outcome with no settle evidence says 0, as a row in
    today's spelling records it."""
    request_values = request_data if request_data is not None else terms.get("request") or {}
    endpoint = {"provider": terms["provider"], **terms}
    request = plugins.Request(method=terms["method"],
                              **request_document(endpoint, request_values, defaults=False))
    answer = plugins.Answer(status=status, headers=dict(headers or {}), raw=raw, body=doc)
    bill = terms.get("bill") or {}
    free_when = bill.get("free_when")
    if free_when is not None and holds(free_when, {
            **request_document(endpoint, request_values, defaults=True), "response": doc}):
        return 0, 0
    if (figure := _figure(terms, doc, headers, submitted)) is not None:
        return figure, figure
    evidence = _evidence(terms, hold, doc, request, answer)
    judged = outcomes.judge(endpoint, status, doc, request=request, answer=answer)
    rule = {"error": bill.get("error", "free"), "miss": bill.get("miss", "free")}.get(judged.state, "charged")
    if rule == "free":
        return (0 if evidence is None else evidence), 0
    if rule != "charged":
        return evidence, min(amount(terms, rule), hold)
    return evidence, _settled(terms, basis_, hold, doc, raw, evidence)


def terminal(terms: dict, basis_: dict, outcome: str, doc: object) -> int:
    """What an async v2 task settles at on its terminal answer: a `billed_failure` is charged by
    its `settle` form, a `success` goes through steps 2 to 5 with the terminal document; a figure
    the provider reported on the submission stands in for one the terminal answer lacks."""
    hold = int(basis_.get("reserve_micro") or 0)
    raw = json.dumps(doc).encode() if doc is not None else b""
    submitted = basis_.get("submission_charge_micro")
    if outcome != "billed_failure":
        return charge(terms, basis_, hold, 200, doc, raw=raw, submitted=submitted)[1]
    if (figure := _figure(terms, doc, None, submitted)) is not None:
        return figure
    return _settled(terms, basis_, hold, doc, raw, _evidence(terms, hold, doc, None, None))


def persisted(basis_: dict, terms: dict | None) -> dict:
    """The basis an async task is stored with: a v2 row's terms ride along, so the worker settles
    the terminal answer by the rules and rates the call was resolved with."""
    return basis_ if terms is None else {**basis_, "terms": terms}


def settle_terminal(basis_: dict, outcome: str, doc: object) -> int:
    """Raw micro-USD an async task settles at on a `success` or `billed_failure` terminal answer."""
    if basis_.get("terms") is not None:
        return terminal(basis_["terms"], basis_, outcome, doc)
    return settlement_basis.settle(basis_, {"terminal": doc})


def _answer_path(path: str) -> str:
    """An answer path up to its first `*` or filter, relative to the answer, as the spool keeps it."""
    kept = []
    for part in relative(path).split("."):
        if part == "*" or "[" in part:
            if "[" in part and part.split("[", 1)[0]:
                kept.append(part.split("[", 1)[0])
            break
        kept.append(part)
    return ".".join(kept)


def evidence_paths(ep: dict) -> tuple[str, ...]:
    """What a spooled answer to a v2 row keeps: the top-level objects its usage meters read, then
    every other answer path its outcome and settle rules read."""
    settle = ep.get("settle") or {}
    usage = settle.get("usage") or {}
    roots = [_answer_path(t["path"]).split(".", 1)[0] for t in usage.get("terms") or ()] or \
        ([_answer_path(usage["path"]).split(".", 1)[0]] if usage.get("path") else [])
    paths = [rule[form]["path"] for rule, form in ((settle, "count"), (settle, "reported"))
             if form in rule and "path" in rule[form]]
    for rule in (ep.get("expect"), (ep.get("error") or {}).get("when"), (ep.get("miss") or {}).get("when"),
                 (ep.get("bill") or {}).get("free_when")):
        if rule is not None:
            paths += [p for p in leaf_paths(rule) if p.startswith("response")]
    return tuple(dict.fromkeys(p for p in roots + [_answer_path(p) for p in paths] if p))
