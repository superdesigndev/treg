"""Today's spelling of an endpoint row and its file header, modelled strictly. It goes once every
provider's rows are in the v2 spelling."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from ..routing.paths import parse as parse_expression
from .base import Model, field
from .common import CeilingParam, Input, Json, ParamSpec, RowBase, Statuses
from .manifest import Defaults

Number = int | float


class TableRow(Model):
    when: dict[str, Json]
    value: Number
    times: str | None = None
    times_min: Number | None = None


class Fallback(Model):
    value: Number
    note: str


class Display(Model):
    unit: str
    grouped: bool | None = None
    round_up: bool | None = None
    variable: bool | None = None
    maximum: bool | None = None
    value: Number | None = None


class RowsReserve(Model):
    param: str
    default: dict[str, Json]
    max: int


class Reserve(Model):
    rows: RowsReserve | None = None
    entities: dict[str, Json] | None = None


class Count(Model):
    path: str


class SettleCount(Model):
    count: Count


class UsageTerm(Model):
    path: str
    rate: Number


class Usage(Model):
    unit: str
    path: str | None = None
    terms: list[UsageTerm] | None = None


class ReportedCharge(Model):
    path: str
    unit: str


class Presence(Model):
    field: str
    row: str | None = None
    request_field: str | None = None


class Quantity(Model):
    field: str
    max: int
    applied: str | None = None


class Modifier(Model):
    location: str
    when: str
    add_credits: Number | None = None
    add_credits_per_result: Number | None = None
    set_credits: Number | None = None
    reserve_only: bool | None = None


class Cost(Model):
    type: Literal["per_call", "per_result", "per_success", "free", "quota_rows"] | None = None
    value: Number | None = None
    currency: Literal["USD", "CNY", "credit", "unit"] | None = None
    per: int | None = None
    unit: str | None = None
    note: str | None = None
    display: Display | None = None
    table: list[TableRow] | None = None
    fallback: Fallback | None = None
    table_min: Number | None = None
    reserve: Reserve | None = None
    settle: Literal["table", "usage", "quantity", "base", "modifiers", "terminal_presence"] \
        | SettleCount | None = None
    usage: Usage | None = None
    reported_charge: ReportedCharge | None = None
    presence: Presence | None = None
    modifiers: dict[str, Modifier] | None = None
    quantity: Quantity | None = None
    ceiling_param: CeilingParam | None = field(kind="P")
    source: str | None = field(kind="C")
    source_url: str | None = field(kind="C")
    checked: str | None = field(kind="C")
    confidence: str | None = field(kind="C")
    # single-provider keys: each is on the shrink-only list until its provider's batch
    call_fee: Number | None = None
    call_fee_per: list[str] | None = None
    contactout: dict[str, Json] | None = None
    sumble: dict[str, Json] | None = None
    enrichlayer: dict[str, Json] | None = None
    octen_rates: dict[str, Number] | None = None
    tavily_rates: dict[str, Number] | None = None
    rate_card: dict[str, str] | None = field(kind="C")
    bills_miss: bool | None = None


class Expect(Model):
    json_path: str
    equals: Json
    misses: list[Json] | None = None


def _expression(expr: str) -> str:
    parse_expression(expr)
    return expr


Expression = Annotated[str, AfterValidator(_expression)]


class Miss(Model):
    status: int
    means: str
    when: Expression | None = None


class NotFound(Model):
    status: int | Statuses
    means: str
    when: Expression | None = None


class LegacyRow(RowBase):
    tier: Literal["core", "extended"] | None = None
    cost: Cost | None = field(kind="P")
    observed_cost: Number | None = field(kind="C")
    expect: Expect | None = field(kind="P")
    miss: Miss | None = field(kind="P")
    not_found: NotFound | None = field(kind="P")
    async_: dict[str, Json] | Literal[False] | None = field(alias="async", kind="P")


class Source(Model):
    x_kind = "C"
    docs: str | None = None
    openapi: str | None = None
    curated: str | None = None
    ingested: str | None = None
    method: str | None = None
    llms: str | None = None
    rate_card: str | None = None
    spec_urls: list[str] | None = None


class EndpointFile(Model):
    """An endpoint file's header in today's spelling, plus the interim `defaults:` block."""

    provider: str
    source: Source | None = field(kind="C")
    proposed_capabilities: dict[str, str] | None = field(kind="C", min_length=1)
    endpoints: list[dict[str, Json]] = field(..., min_length=1)
    pricing_url: str | None = None
    limits: str | None = None
    cache: str | dict[str, Json] | None = None
    expect: Expect | None = None
    async_: dict[str, Json] | None = Field(default=None, alias="async")
    ceiling_param: CeilingParam | None = None
    client_header: str | None = None
    queryParams: dict[str, ParamSpec] | None = None
    response_retention: str | None = None
    defaults: Defaults | None = None


__all__ = ["Cost", "EndpointFile", "Expect", "Input", "LegacyRow", "Miss", "NotFound"]
