"""Provider plugins: pure, I/O-free functions a catalog row names as `plugin: <module>.<function>`,
one module per provider (the slug with `-` replaced by `_`). Resolved by import; no registry."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import isfunction


def module_name(provider: str) -> str:
    return provider.replace("-", "_")


def resolve(name: str, provider: str) -> Callable:
    module, _, function = name.partition(".")
    if module != module_name(provider):
        raise ValueError(f"{name}: a row names a function of its own provider's module, "
                         f"{module_name(provider)}")
    try:
        found = getattr(importlib.import_module(f"{__name__}.{module}"), function)
    except (ImportError, AttributeError):
        raise ValueError(f"{name} does not import from {__name__}.{module}") from None
    if not isfunction(found) or function.startswith("_"):
        raise ValueError(f"{name} is not a public function")
    return found


@dataclass(frozen=True)
class Request:
    """What a plugin may read of the caller's request: the method, the query (path parameters
    included) and the parsed JSON body, or None."""

    method: str
    query: Mapping[str, object]
    body: object


@dataclass(frozen=True)
class Answer:
    """What a plugin may read of the provider's answer: the status, the headers, the raw body and
    the parsed JSON body, or None."""

    status: int
    headers: Mapping[str, str]
    raw: bytes
    body: object


def call(name: str, provider: str, *args: object) -> object:
    """A plugin's result, or None when it raises: a raising plugin is a bug, read as missing
    evidence by its hook. Only the exception's type is logged, never its message."""
    try:
        return resolve(name, provider)(*args)
    except Exception as exc:  # noqa: BLE001 - one failing plugin must never fail the call
        logging.getLogger("treg.plugins").error("plugin %s raised %s", name, type(exc).__name__)
        return None
