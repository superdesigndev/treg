"""Reading endpoint files through the strict models: the one YAML reading, the interim `defaults:`
header block, the one merge rule, and which spelling a row is written in."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

from . import violations
from .legacy import EndpointFile, LegacyRow
from .v2 import V2Row

V2_KEYS = frozenset({"price", "provenance", "reserve", "settle", "bill", "error", "ceiling_param"})
LEGACY_KEYS = frozenset({"cost", "observed_cost", "tier"})
# Header keys of today's spelling that a `defaults:` entry would give a second home.
HEADER_HOMES = ("cache", "expect", "async", "ceiling_param", "response_retention")


class CatalogSchemaError(ValueError):
    pass


# libyaml's C parser where it is installed: a worker process pays the whole load on its first call.
_SafeBase = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Loader(_SafeBase):
    """Keeps timestamps as the strings they were written as: a `date` in a served row breaks every
    plain `json.dumps` of it, and the validator reads the same strings the loader serves."""


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in rs if tag != "tag:yaml.org,2002:timestamp"]
    for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()}


def read_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogSchemaError(f"{path.name}: {type(exc).__name__}: {exc}") from exc


def describe(errors: Iterable[Mapping[str, Any]]) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" if e["loc"] else str(e["msg"])
                     for e in errors)


def stem(path: Path) -> tuple[str, Literal["core", "extended"]]:
    name = path.name.removesuffix(".yaml")
    return name.removesuffix(".extended"), "extended" if name.endswith(".extended") else "core"


def _strict(model: type[BaseModel], data: Mapping[str, Any], at: str,
            hits: set[tuple[str, str]] | None) -> BaseModel | None:
    """The parsed model, or None when every error is a listed violation of `at`."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        if left := violations.unlisted(at, exc, hits):
            raise CatalogSchemaError(f"{at}: {describe(left)}") from exc
        return None


@dataclass(frozen=True)
class EndpointDoc:
    path: Path
    provider: str
    tier: Literal["core", "extended"]
    raw: dict
    defaults: dict | None


def read_endpoint_file(path: Path, hits: set[tuple[str, str]] | None = None) -> EndpointDoc:
    raw = read_yaml(path)
    if not isinstance(raw, dict):
        raise CatalogSchemaError(f"{path.name}: an endpoint file is a mapping")
    _strict(EndpointFile, raw, path.name, hits)
    provider, tier = stem(path)
    if raw["provider"] != provider:
        raise CatalogSchemaError(f"{path.name}: provider '{raw['provider']}' != filename stem '{provider}'")
    return EndpointDoc(path, provider, tier, raw, raw.get("defaults"))


def provider_defaults(docs: Iterable[EndpointDoc]) -> dict[str, dict]:
    docs = list(docs)
    out: dict[str, dict] = {}
    homes: dict[str, str] = {}
    for doc in docs:
        if doc.defaults is None:
            continue
        if doc.provider in out:
            raise CatalogSchemaError(f"{doc.path.name}: defaults is also declared in {homes[doc.provider]}; "
                                     "it sits in at most one of a provider's two endpoint files")
        out[doc.provider], homes[doc.provider] = doc.defaults, doc.path.name
    for doc in docs:
        if twice := [k for k in HEADER_HOMES if k in doc.raw and k in out.get(doc.provider, {})]:
            raise CatalogSchemaError(f"{doc.path.name}: {', '.join(twice)} is declared both in the header and "
                                     f"in defaults ({homes[doc.provider]}); a provider-level declaration has "
                                     "one home")
    return out


def merge(row: Mapping[str, Any], defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """The one merge rule, per top-level key: a declared value wins whole, a declared null opts the
    row out, and an unmentioned key takes the default."""
    return {**{k: v for k, v in (defaults or {}).items() if k not in row}, **row}


def _v2_outcome(key: str, value: object) -> bool:
    if key == "expect":
        return isinstance(value, list) or (isinstance(value, Mapping) and "json_path" not in value)
    if not isinstance(value, Mapping):
        return False
    if key == "miss":
        return not isinstance(value.get("status"), int)
    return not isinstance(value.get("when"), str | None)


def spelling(row: Mapping[str, Any]) -> Literal["legacy", "v2"]:
    """v2 when a row declares a v2 key; else, with no cost of today's spelling, when one of its
    outcome keys has the v2 shape."""
    if V2_KEYS & row.keys():
        return "v2"
    if LEGACY_KEYS & row.keys():
        return "legacy"
    outcome = any(_v2_outcome(k, row[k]) for k in ("expect", "miss", "not_found") if row.get(k) is not None)
    return "v2" if outcome else "legacy"


def parse_row(row: Mapping[str, Any], where: str,
              hits: set[tuple[str, str]] | None = None) -> LegacyRow | V2Row | None:
    at = str(row.get("id") or where)
    if spelling(row) == "legacy":
        return _strict(LegacyRow, row, at, hits)
    if mixed := sorted(LEGACY_KEYS & row.keys()):
        raise CatalogSchemaError(f"{at}: mixes today's spelling ({', '.join(mixed)}) with v2 keys "
                                 f"({', '.join(sorted(V2_KEYS & row.keys()))})")
    return _strict(V2Row, {k: v for k, v in row.items() if v is not None}, at, hits)
