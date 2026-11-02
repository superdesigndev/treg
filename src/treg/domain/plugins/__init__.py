"""Provider plugins: pure, I/O-free functions a catalog row names as `plugin: <module>.<function>`,
one module per provider (the slug with `-` replaced by `_`). Resolved by import; no registry."""

from __future__ import annotations

import importlib
from collections.abc import Callable
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
