"""The shrink-only list of existing violations (`violations.yaml`). Every entry may only disappear:
an unlisted use fails validation, a listed entry nothing uses any more fails too, and CI compares the
list with the base branch so it never grows."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .common import UNREAD_SPEC_WORDS

VIOLATIONS_FILE = Path(__file__).with_name("violations.yaml")
SECTIONS = ("endpoint_keys", "cost_keys", "unread_input_words", "rejected", "legacy_holds")


@dataclass(frozen=True)
class Violations:
    endpoint_keys: Mapping[str, str] = field(default_factory=dict)
    cost_keys: Mapping[str, str] = field(default_factory=dict)
    unread_input_words: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    rejected: Mapping[tuple[str, str], str] = field(default_factory=dict)
    legacy_holds: frozenset[str] = frozenset()


def parse(doc: Mapping[str, Any]) -> Violations:
    if unknown := sorted(set(doc) - set(SECTIONS)):
        raise ValueError(f"violations.yaml: unknown section {', '.join(unknown)}")
    return Violations(
        endpoint_keys={str(k): str(v["provider"]) for k, v in (doc.get("endpoint_keys") or {}).items()},
        cost_keys={str(k): str(v["provider"]) for k, v in (doc.get("cost_keys") or {}).items()},
        unread_input_words={str(k): tuple(v) for k, v in (doc.get("unread_input_words") or {}).items()},
        rejected={(str(e["at"]), str(e["path"])): str(e["reason"]) for e in doc.get("rejected") or ()},
        legacy_holds=frozenset(str(k) for k in doc.get("legacy_holds") or ()),
    )


def entries(doc: Mapping[str, Any]) -> set[tuple[str, ...]]:
    """Every entry as a comparable tuple, for the base-branch comparison."""
    v = parse(doc)
    return ({("endpoint_keys", k, p) for k, p in v.endpoint_keys.items()}
            | {("cost_keys", k, p) for k, p in v.cost_keys.items()}
            | {("unread_input_words", at, w) for at, words in v.unread_input_words.items() for w in words}
            | {("rejected", at, path) for at, path in v.rejected}
            | {("legacy_holds", at) for at in v.legacy_holds})


@lru_cache(maxsize=1)
def load() -> Violations:
    return parse(yaml.safe_load(VIOLATIONS_FILE.read_text(encoding="utf-8")) or {})


def _loc(error: Mapping[str, Any]) -> str:
    return ".".join(str(p) for p in error["loc"])


def unlisted(at: str, exc: ValidationError, hits: set[tuple[str, str]] | None = None) -> list[dict]:
    """The errors of `exc` no `rejected` entry for `at` covers. Covered entries are added to `hits`."""
    listed = [path for (where, path) in load().rejected if where == at]
    left = []
    for error in exc.errors(include_url=False):
        loc = _loc(error)
        covering = next((p for p in listed if loc == p or loc.startswith(p + ".")), None)
        if covering is None:
            left.append(error)
        elif hits is not None:
            hits.add((at, covering))
    return left


def _spec_words(specs: object) -> Iterator[str]:
    if not isinstance(specs, Mapping):
        return
    for spec in specs.values():
        if isinstance(spec, Mapping):
            yield from (w for w in UNREAD_SPEC_WORDS if w in spec)
            yield from _spec_words(spec.get("properties"))
            if isinstance(spec.get("items"), Mapping):
                yield from _spec_words({"items": spec["items"]})


def input_words(input_schema: object) -> set[str]:
    if not isinstance(input_schema, Mapping):
        return set()
    words: set[str] = set()
    for location in ("queryParams", "pathParams", "headers"):
        words |= set(_spec_words(input_schema.get(location)))
    body = input_schema.get("body")
    if isinstance(body, Mapping):
        if isinstance(body.get("properties"), Mapping):
            words |= set(_spec_words(body["properties"]))
        elif body and all(isinstance(v, Mapping) for v in body.values()):
            words |= set(_spec_words(body))
        else:
            words |= set(_spec_words({"body": body}))
    return words


@dataclass(frozen=True)
class Use:
    """One row as the audit sees it: where it is, its provider, and the row after the merge."""
    at: str
    provider: str
    row: Mapping[str, Any]


def audit(uses: Iterable[Use], headers: Iterable[tuple[str, Mapping[str, Any]]],
          rejected_hits: set[tuple[str, str]], v: Violations | None = None) -> list[str]:
    v = v or load()
    errors: list[str] = []
    key_users: dict[str, set[str]] = {}
    cost_users: dict[str, set[str]] = {}
    words_at: dict[str, set[str]] = {}
    holds: set[str] = set()
    for use in uses:
        for key in use.row:
            key_users.setdefault(key, set()).add(use.provider)
        cost = use.row.get("cost")
        for key in cost if isinstance(cost, Mapping) else ():
            cost_users.setdefault(key, set()).add(use.provider)
        if words := input_words(use.row.get("input")):
            words_at[use.at] = words
        rows = (use.row.get("reserve") or {}).get("rows") if isinstance(use.row.get("reserve"), Mapping) else None
        if isinstance(rows, Mapping) and rows.get("param") is None:
            holds.add(use.at)
    for name, header in headers:
        if words := set(_spec_words(header.get("queryParams"))):
            words_at[name] = words
    for section, listed, users in (("endpoint_keys", v.endpoint_keys, key_users),
                                   ("cost_keys", v.cost_keys, cost_users)):
        for key, provider in listed.items():
            if extra := sorted(users.get(key, set()) - {provider}):
                errors.append(f"{key}: a single-provider key of {provider} is also used by "
                              f"{', '.join(extra)}; provider-named keys are never added to")
            if provider not in users.get(key, set()):
                errors.append(f"violations.yaml: {section}.{key} is no longer used by {provider}; remove it")
    for at in sorted(words_at.keys() | v.unread_input_words.keys()):
        used, allowed = words_at.get(at, set()), set(v.unread_input_words.get(at, ()))
        if extra := sorted(used - allowed):
            errors.append(f"{at}: input uses {', '.join(extra)}, JSON Schema words nothing reads; "
                          "drop them, or say it with note, min, max, minItems or maxItems")
        if stale := sorted(allowed - used):
            errors.append(f"violations.yaml: unread_input_words.{at} no longer uses {', '.join(stale)}; remove it")
    for at, path in sorted(set(v.rejected) - rejected_hits):
        errors.append(f"violations.yaml: rejected {at} {path} parses now; remove it")
    for at in sorted(holds - v.legacy_holds):
        errors.append(f"{at}: reserve.rows without param is a frozen legacy hold and must be listed")
    for at in sorted(v.legacy_holds - holds):
        errors.append(f"violations.yaml: legacy_holds.{at} is no longer a legacy hold; remove it")
    return errors
