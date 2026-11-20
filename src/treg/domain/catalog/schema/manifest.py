"""The v2 provider manifest (`src/treg/catalog/providers/<slug>.yaml`): every provider-level fact in
one file, in eight sections. Manifest batches start writing them; the loader reads none yet."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, model_validator

from .base import Date, Model, Money, field
from .common import Json
from .paths import ResponsePath, ResponsePredicate
from .v2 import PluginName

FORBIDDEN_DEFAULTS = frozenset({"host", "auth", "credential", "base_url", "hosts"})


def _https(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError(f"{url!r}: a plain https:// URL with no user info, query or fragment")
    return url


HttpsUrl = Annotated[str, AfterValidator(_https)]


class Display(Model):
    name: str
    category: str
    summary: str
    docs_url: str
    pricing_url: str = ""
    limits: str = ""


class Connect(Model):
    setup_url: str
    action: str
    steps: list[str] = Field(min_length=1)
    note: str = ""


def _has_secret(value: str) -> str:
    if "{secret}" not in value:
        raise ValueError("a credential format contains {secret}")
    return value


SecretFormat = Annotated[str, AfterValidator(_has_secret)]


class Placement(Model):
    in_: Literal["header", "query", "json"] = field(..., alias="in", gated=True)
    name: str = field(..., gated=True)
    format: SecretFormat = field("{secret}", gated=True)


class Credential(Placement):
    encode: Literal["base64"] | None = field(gated=True)
    label: str = ""
    placeholder: str = ""


class Pair(Placement):
    part: Annotated[str, Field(pattern=r"^[a-z]+$")] = field(..., gated=True)
    label: str
    note: str = ""


class Host(Model):
    base_url: HttpsUrl = field(..., gated=True)
    credential: Placement | None = field(gated=True)


class Auth(Model):
    kind: Literal["key", "token"] = field(..., gated=True)
    base_url: HttpsUrl = field(..., gated=True)
    legacy_base_urls: list[HttpsUrl] = field(default_factory=list, gated=True)
    credential: Credential
    pair: Pair | None = None
    hosts: list[Host] = field(default_factory=list, gated=True)
    required_headers: dict[str, str] = field(default_factory=dict, gated=True)

    def approved_hosts(self) -> set[str]:
        roots = [self.base_url, *self.legacy_base_urls, *(h.base_url for h in self.hosts)]
        return {urlsplit(url).hostname or "" for url in roots}


class Request(Model):
    method: Literal["GET", "POST"] = "GET"
    host: str | None = None
    path: str
    body: dict[str, Json] | None = None


class Probe(Model):
    request: Request
    deferred: list[int] = Field(default_factory=list)
    reject: list[int] = Field(default_factory=list)
    ok: ResponsePredicate | None = None
    cost_usd: Money | None = None


class Rate(Model):
    x_kind = "A"
    usd: Money | None
    basis: str = field(..., kind="C")
    source: str = field(..., kind="C")
    checked: Date = field(..., kind="C")
    note: str = field("", kind="C")
    kind: Literal["treg_shared_plan", "treg_trial"] | None = None
    fee_usd_month: Money | None = None
    trial_calls_per_team_day: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _kind_terms(self) -> Rate:
        if self.kind == "treg_shared_plan" and self.fee_usd_month is None:
            raise ValueError("a treg_shared_plan rate needs fee_usd_month")
        if self.kind == "treg_trial" and self.trial_calls_per_team_day is None:
            raise ValueError("a treg_trial rate needs trial_calls_per_team_day")
        return self


class Pricing(Model):
    x_kind = "A"
    credit: Rate | None = None
    units: dict[str, Rate] = Field(default_factory=dict)


class Quota(Model):
    x_kind = "A"
    limit: int
    period: str
    resets_at_rule: str


class RateLimit(Model):
    x_kind = "A"
    limit: int = Field(ge=1)
    window_s: int | float = Field(gt=0)
    source: Literal["docs", "policy", "headers", "api"]


class Seed(Model):
    x_kind = "A"
    meter: Literal["credits", "cash", "monthly_quota", "requests", "rolling_quota"]
    funding: Literal["manual", "auto_recharge", "quota_reset", "subscription", "unknown"]
    observed_by: Literal["api", "manual", "headers", "none"]
    quota: Quota | None = None
    rate_limit: RateLimit | None = None
    overflow: bool = True


class BalanceRead(Model):
    path: ResponsePath
    unit: str


class Balance(Model):
    request: Request | None = None
    read: BalanceRead | None = None
    plugin: PluginName | None = None
    none: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Balance:
        given = [k for k in ("read", "plugin", "none") if k in self.model_fields_set]
        if len(given) != 1:
            raise ValueError(f"exactly one of read, plugin, none, got {', '.join(given) or 'none'}")
        if (self.request is None) != (self.none is not None):
            raise ValueError("balance.request is present unless balance.none is")
        return self


def _regex(value: str) -> str:
    re.compile(value)
    return value


class Signature(Model):
    status: int
    body_regex: Annotated[str, AfterValidator(_regex)]
    kind: Literal["balance", "quota", "burst", "unknown"]
    resets: Literal["next_utc_midnight"] | None = None
    tripwire: bool = False


class Capacity(Model):
    x_kind = "A"
    seed: Seed
    concurrency: int | None = Field(default=None, ge=1)
    endpoint_rate_limits: dict[str, RateLimit] = Field(default_factory=dict)
    overdraft_served: bool = field(False, kind="P")
    balance: Balance | None = field(kind="P")
    signatures: list[Signature] = field(default_factory=list, kind="P")


def _defaults(value: dict[str, Json]) -> dict[str, Json]:
    if forbidden := sorted(FORBIDDEN_DEFAULTS & value.keys()):
        raise ValueError(f"defaults may not hold {', '.join(forbidden)}: placement is provider-only")
    if nulls := sorted(k for k, v in value.items() if v is None):
        raise ValueError(f"defaults may not hold null ({', '.join(nulls)}): there is nothing to opt out of")
    return value


Defaults = Annotated[dict[str, Json], AfterValidator(_defaults)]


class Manifest(Model):
    provider: str
    display: Display
    connect: Connect
    auth: Auth | None = None
    probe: Probe | None = None
    pricing: Pricing | None = field(kind="A")
    capacity: Capacity | None = field(kind="A")
    defaults: Defaults | None = None

    @model_validator(mode="after")
    def _hosts(self) -> Manifest:
        requests = [r for r in (self.probe and self.probe.request,
                                self.capacity and self.capacity.balance and self.capacity.balance.request)
                    if r is not None and r.host is not None]
        approved = self.auth.approved_hosts() if self.auth else set()
        for request in requests:
            if request.host not in approved:
                raise ValueError(f"host {request.host!r} is not an approved host of auth")
        return self
