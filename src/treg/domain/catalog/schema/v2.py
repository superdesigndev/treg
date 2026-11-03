"""The v2 spelling of an endpoint row: one outcome declaration and the cost split into `price`,
`provenance`, `reserve`, `settle` and `bill`. Absent keys take the meaning each model states."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter, model_validator

from .base import CONFIDENCES, COST_SOURCES, COST_UNITS, Date, Model, Money, field
from .common import CeilingParam, Json, RowBase
from .paths import (AnswerOrRequestPredicate, Predicate, RequestPath, RequestPredicate, ResponsePath,
                    ResponsePredicate, parse_path, relative)

_PLUGIN = re.compile(r"[a-z][a-z0-9_]*\.[a-z_][a-z0-9_]*")


def _plugin(name: str) -> str:
    if not _PLUGIN.fullmatch(name):
        raise ValueError(f"{name!r}: a plugin is named <module>.<function>")
    return name


PluginName = Annotated[str, AfterValidator(_plugin)]


def _one_of(model: Model, keys: tuple[str, ...], *, at_most: bool = False) -> None:
    given = [k for k in keys if k in model.model_fields_set]
    if len(given) > 1 or (not at_most and not given):
        raise ValueError(f"exactly one of {', '.join(keys)}, got {', '.join(given) or 'none'}")


class TableRow(Model):
    when: list[RequestPredicate] = Field(min_length=1)
    value: Money
    times: RequestPath | None = None
    times_min: int | float | None = None


class Fallback(Model):
    value: Money
    note: str = Field(min_length=1)


class PriceDisplay(Model):
    unit: str
    grouped: bool = False
    round_up: bool = False
    variable: bool = False
    maximum: bool = False
    value: Money | None = None


class Price(Model):
    """Absent: unpriced, never billable on treg's key."""

    value: Money | None = None
    table: list[TableRow] | None = Field(default=None, min_length=1)
    rates: dict[str, Money] | None = Field(default=None, min_length=1)
    fallback: Fallback | None = None
    per: int = Field(default=1, ge=1)
    round: Literal["up"] | None = None
    currency: Literal["USD", "CNY", "credit", "unit"] = "USD"
    unit: Literal[COST_UNITS] | None = None  # type: ignore[valid-type]
    display: PriceDisplay | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Price:
        _one_of(self, ("value", "table", "rates"))
        if (self.table is None) != (self.fallback is None):
            raise ValueError("a table needs a fallback, and a fallback needs a table")
        if "value" in self.model_fields_set and self.value is None and not self.note:
            raise ValueError("an unknown price (value: null) needs a note saying why")
        if self.currency == "unit" and not self.unit:
            raise ValueError("currency: unit names its meter in unit")
        return self


class Provenance(Model):
    x_kind = "C"
    source: Literal[COST_SOURCES]  # type: ignore[valid-type]
    source_url: str | None = None
    checked: Date
    confidence: Literal[CONFIDENCES]  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _url(self) -> Provenance:
        if self.source != "observed" and not self.source_url:
            raise ValueError("source_url is required unless source is observed")
        return self


class RowsDefault(Model):
    value: int = Field(ge=1)
    source: Literal["docs", "observed", "legacy"]


class Rows(Model):
    param: RequestPath | None = None
    default: RowsDefault | None = None
    max: int = Field(ge=1)

    @model_validator(mode="after")
    def _shape(self) -> Rows:
        if self.param is None and (self.default is None or self.default.source != "legacy"):
            raise ValueError("rows without param is only a frozen legacy hold (default.source: legacy)")
        if self.default is not None and self.default.value > self.max:
            raise ValueError("rows.default.value exceeds rows.max")
        return self


class Entities(Model):
    from_: RequestPath | list[RequestPath] = Field(alias="from")
    max: int = Field(default=10_000, ge=1)


class Reserve(Model):
    """Absent: one unit, the flat price or the matched table row."""

    rows: Rows | None = None
    entities: Entities | None = None
    plugin: PluginName | None = None
    max: Money | None = None

    @model_validator(mode="after")
    def _shape(self) -> Reserve:
        _one_of(self, ("rows", "entities", "plugin"))
        if (self.plugin is None) != (self.max is None):
            raise ValueError("reserve.plugin needs max beside it, and max belongs only to a plugin")
        return self


class Count(Model):
    path: ResponsePath


class Reported(Model):
    path: ResponsePath | None = None
    header: str | None = Field(default=None, min_length=1)
    unit: str = Field(min_length=1)
    scale: int | float = 1

    @model_validator(mode="after")
    def _shape(self) -> Reported:
        _one_of(self, ("path", "header"))
        return self


def _meter(text: str) -> str:
    path = parse_path(text)
    if path.filter is not None or any(step.every for step in path.steps) or not path.steps:
        raise ValueError(f"{text!r}: a usage meter is one value under response: names, indexes and "
                         "[k=v] filters, no * and no filter on the root")
    return text


MeterPath = Annotated[ResponsePath, AfterValidator(_meter)]


class UsageTerm(Model):
    path: MeterPath
    rate: Money


class Usage(Model):
    path: MeterPath | None = None
    terms: list[UsageTerm] | None = Field(default=None, min_length=1)
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> Usage:
        _one_of(self, ("path", "terms"))
        return self


class Settle(Model):
    """Absent: the hold."""

    count: Count | None = None
    reported: Reported | None = None
    usage: Usage | None = None
    plugin: PluginName | None = None

    @model_validator(mode="after")
    def _shape(self) -> Settle:
        _one_of(self, ("count", "reported", "usage", "plugin"))
        return self


class Bill(Model):
    """Absent: a hit is charged, a miss and an error are free."""

    miss: Literal["free", "charged"] | Annotated[int, Field(ge=1)] = "free"
    error: Literal["free", "charged"] = "free"
    free_when: AnswerOrRequestPredicate | None = None


class OutcomeRule(Model):
    when: ResponsePredicate | None = None
    plugin: PluginName | None = None
    means: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> OutcomeRule:
        _one_of(self, ("when", "plugin"))
        return self


ClientErrors = Annotated[list[Annotated[int, Field(ge=400, le=499)]], Field(min_length=1)]


class MissRule(Model):
    status: ClientErrors | None = None
    when: ResponsePredicate | None = None
    plugin: PluginName | None = None
    means: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> MissRule:
        _one_of(self, ("when", "plugin"), at_most=True)
        if not {"status", "when", "plugin"} & self.model_fields_set:
            raise ValueError("a miss rule needs status, when or plugin")
        return self


class NotFoundRule(Model):
    status: Annotated[list[Annotated[int, Field(ge=200, le=499)]], Field(min_length=1)]
    when: ResponsePredicate | None = None
    means: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> NotFoundRule:
        if self.when is None and any(200 <= s < 300 for s in self.status):
            raise ValueError("not_found with a 2xx status needs a when, or every success ends the call")
        return self


class V2Row(RowBase):
    expect: ResponsePredicate | None = field(kind="P")
    error: OutcomeRule | None = field(kind="P")
    miss: MissRule | None = field(kind="P")
    not_found: NotFoundRule | None = field(kind="P")
    price: Price | None = field(kind="P")
    provenance: Provenance | None = field(kind="C")
    reserve: Reserve | None = field(kind="P")
    settle: Settle | None = field(kind="P")
    bill: Bill | None = field(kind="P")
    ceiling_param: CeilingParam | None = field(kind="P")
    async_: dict[str, Json] | None = field(alias="async", kind="P")

    @model_validator(mode="after")
    def _cost(self) -> V2Row:
        priced = self.price is not None and (
            self.price.table is not None or self.price.rates is not None
            or (self.price.value is not None and self.price.value > 0))
        if priced and self.provenance is None:
            raise ValueError("a priced row needs provenance")
        unknown = self.price is not None and "value" in self.price.model_fields_set \
            and self.price.value is None
        if self.provenance is not None and (self.provenance.confidence == "unknown") != unknown:
            raise ValueError("provenance.confidence is unknown exactly when price.value is null")
        if self.price is not None and self.price.rates is not None:
            if self.reserve is None or self.reserve.plugin is None \
                    or (self.settle is not None and self.settle.plugin is None):
                raise ValueError("price.rates stands only beside plugins: a reserve.plugin, and a "
                                 "settle, if any, that is a plugin")
            if self.bill is not None and isinstance(self.bill.miss, int):
                raise ValueError("bill.miss in units needs a unit price; price.rates has none")
        return self


RESOLVED_KEYS = ("expect", "error", "miss", "not_found", "price", "provenance", "reserve", "settle",
                 "bill", "ceiling_param")
_PREDICATE = TypeAdapter(Predicate)


def _plain(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Model):
        return value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return _PREDICATE.dump_python(value, mode="json", exclude_unset=True)


def _number(text: str) -> int | float:
    value = Decimal(text)
    return int(value) if value == value.to_integral_value() else float(value)


def _legacy_location(path: str, input_schema: dict) -> str:
    """`query.x` in today's spelling: `pathParams.x` when the endpoint declares it a path parameter."""
    location, _, rest = path.partition(".")
    if location != "query":
        return path
    named = rest.split(".", 1)[0]
    return f"pathParams.{rest}" if named in (input_schema.get("pathParams") or {}) else f"queryParams.{rest}"


def _table_view(table: list[dict], input_schema: dict) -> list[dict] | None:
    """A price table in today's display shape (`when` as a map of request fields to values), or
    None when a row has a condition other than one equality, which today's shape cannot show."""
    rows = []
    for row in table:
        if any(not isinstance(c, dict) or c.get("op") != "equals" for c in row["when"]):
            return None
        out = {"when": {_legacy_location(c["path"], input_schema): c["value"] for c in row["when"]},
               "value": _number(row["value"])}
        if row.get("times"):
            out["times"] = _legacy_location(row["times"], input_schema)
        if row.get("times_min") is not None:
            out["times_min"] = row["times_min"]
        rows.append(out)
    return rows


def cost_type(price: dict | None, reserve: dict | None, settle: dict | None, bill: dict) -> str | None:
    """Today's `cost.type` label of a v2 row, for the views that show one: free, per_result (the
    bill follows a quantity), per_call (a miss is charged) or per_success."""
    if price is None:
        return None
    if price.get("value") is not None and Decimal(price["value"]) == 0:
        return "free"
    if reserve is not None or (settle or {}).keys() & {"count", "plugin"}:
        return "per_result"
    if bill.get("miss") == "charged":
        return "per_call"
    return "per_success"


def _reserve_view(reserve: dict, input_schema: dict) -> dict:
    if "rows" in reserve:
        rows = reserve["rows"]
        view = {"param": _legacy_location(rows["param"], input_schema)} if rows.get("param") else {}
        return {"rows": {**view, **{k: rows[k] for k in ("default", "max") if k in rows}}}
    return {"entities": {}} if "entities" in reserve else {}


def served_cost(row: dict, input_schema: dict) -> dict | None:
    """The served cost of a v2 row: its price, provenance, hold and settle forms in today's `cost`
    shape, so every view and eligibility check that reads `cost` reads a v2 row unchanged. Billing
    never reads it."""
    price, provenance = row.get("price"), row.get("provenance") or {}
    if price is None:
        return None
    reserve, settle = row.get("reserve") or {}, row.get("settle") or {}
    out: dict = {"type": cost_type(price, row.get("reserve"), row.get("settle"), row["bill"])}
    if price.get("value") is not None:
        out["value"] = _number(price["value"])
    if price.get("rates") is not None:
        out["fallback"] = {"value": _number(reserve["max"]),
                           "note": "the most one call is held at; its plugin's quantities are priced by the rates"}
    if price.get("table") is not None:
        if (table := _table_view(price["table"], input_schema)) is not None:
            out["table"] = table
        out["fallback"] = {"value": _number(price["fallback"]["value"]), "note": price["fallback"]["note"]}
    for key in ("currency", "per", "unit"):
        if key in price:
            out[key] = price[key]
    if "display" in price:
        out["display"] = {k: (_number(v) if k == "value" else v) for k, v in price["display"].items()}
    if view := _reserve_view(reserve, input_schema):
        out["reserve"] = view
    if "count" in settle:
        out["settle"] = {"count": {"path": relative(settle["count"]["path"])}}
    reported = settle.get("reported") or {}
    if reported.get("path"):
        out["reported_charge"] = {"path": relative(reported["path"]), "unit": reported["unit"]}
    usage = settle.get("usage")
    if usage is not None:
        out["settle"] = "usage"
        out["usage"] = {"unit": usage["unit"], **(
            {"terms": [{"path": relative(t["path"]), "rate": float(t["rate"])} for t in usage["terms"]]}
            if "terms" in usage else {"path": relative(usage["path"])})}
    out.update(provenance)
    if "note" in price:
        out["note"] = price["note"]
    return out


def served(row: V2Row, input_schema: dict) -> dict:
    """The v2 keys of a resolved row as plain JSON values: every key present, absence as its stated
    meaning (None, or the default `bill`), money as quoted decimal strings, predicates as written;
    and `cost`, the served cost view's source."""
    out = {key: _plain(getattr(row, key)) for key in RESOLVED_KEYS}
    bill = row.bill or Bill()
    out["bill"] = {"miss": bill.miss, "error": bill.error,
                   **({"free_when": _plain(bill.free_when)} if bill.free_when is not None else {})}
    return {**out, "cost": served_cost(out, input_schema)}


def is_v2(endpoint: dict) -> bool:
    return "bill" in endpoint
