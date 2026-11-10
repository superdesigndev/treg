"""The base every catalog schema model shares: strict parsing, unknown keys rejected, and a kind on
every field (`x-kind`, and `x-gated` on credential placement) in the exported JSON Schema."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

# How much a recorded price is worth as evidence: verified (observed in real billing or the
# provider's live rate card), documented (its docs or pricing page), inferred (from an adjacent
# price), unknown (no figure; the value is null and a note says why).
CONFIDENCES = ("verified", "documented", "inferred", "unknown")
# Where the figure came from; `observed` is the provider's own reported charge on a captured call.
COST_SOURCES = ("rate_card_api", "docs", "observed", "vendor_email", "inferred")
# What one unit is called; under `currency: unit` it names the provider's own meter.
COST_UNITS = ("call", "result", "row", "record", "keyword", "page", "character", "utf8_byte", "review",
              "section", "employee", "GB", "ad", "month", "line", "target", "domain", "item",
              "post", "user",
              "api_unit", "analysis_unit", "retrieval_unit", "index_item_unit", "quota_row",
              "verifier_credit", "step", "credit")

KINDS = {
    "P": "provider fact",
    "A": "account term of the reference deployment",
    "D": "deployment switch",
    "C": "catalog curation",
}


def _stamp_kinds(schema: dict[str, Any], model: type[BaseModel]) -> None:
    for prop in schema.get("properties", {}).values():
        prop.setdefault("x-kind", model.x_kind)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True,
                              json_schema_extra=_stamp_kinds)
    x_kind: ClassVar[str] = "P"


def field(default: Any = None, *, kind: str | None = None, gated: bool = False, **kw: Any) -> Any:
    extra: dict[str, Any] = {}
    if kind:
        extra["x-kind"] = kind
    if gated:
        extra["x-gated"] = True
    if "default_factory" in kw:
        return Field(json_schema_extra=extra or None, **kw)
    return Field(default, json_schema_extra=extra or None, **kw)


_DECIMAL = re.compile(r"\d+(?:\.\d+)?")


def _money(value: object) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError("money is a quoted decimal string, such as '0.009'")
    return Decimal(value)


Money = Annotated[Decimal, BeforeValidator(_money),
                  WithJsonSchema({"type": "string", "pattern": _DECIMAL.pattern})]

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date(value: object) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("a date is YYYY-MM-DD")
    date.fromisoformat(value)
    return value


Date = Annotated[str, BeforeValidator(_date), WithJsonSchema({"type": "string", "format": "date"})]
