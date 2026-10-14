"""Decision points a deployment's private policy extension can plug into.

treg keeps every defense mechanism public; a hosted deployment may add its own abuse policy on top
through an installed extension, without forking treg. This module is the whole contract between
the two: an extension may import nothing else from treg.

An extension is a package exposing an object under the `treg.extensions` entry-point group, with an
integer `api_version` and an `install(app, registry)` method. `create_app` loads every installed
extension; `TREG_REQUIRED_EXTENSIONS` (comma-separated entry-point names) makes a missing one a
startup failure, so a broken build never serves without the policy its operator relies on.

Extensions can only restrict. Each decision point defines its outcomes and what treg does when the
extension is slow or fails; the extension cannot change either. A point with no handler runs its
public default, which is treg's behavior without any extension.

A failing extension is almost always a bug or a misconfiguration on the operator's side, not
something a caller can trigger, so every point falls back to its public default and reports the
failure (a log line and a fault event) for the operator to fix. An extension that wants to be
strict when its own state is missing (a counter store down, say) decides that itself and answers
normally.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol

from . import analytics

if TYPE_CHECKING:
    from fastapi import FastAPI

POLICY_API_VERSION = 1
ENTRY_POINT_GROUP = "treg.extensions"
SIGNUP_CREDIT_BUDGET_S = 0.3

_log = logging.getLogger("treg")


@dataclass(frozen=True)
class SignupCreditContext:
    """A verified user is about to claim the one-time signup credit for a team they created."""
    user_id: int
    org_id: int
    email: str
    canonical_inbox: str  # the mailbox the credit is limited to (`promotions.promo_inbox`)


class CreditDecision(StrEnum):
    GRANT = "grant"
    # No credit now. Nothing is written: the user stays eligible, and the point is asked again
    # the next time they create a team.
    DEFER = "defer"


SignupCreditHandler = Callable[[SignupCreditContext], Awaitable[CreditDecision]]


class PolicyRegistry:
    """Where an extension registers its handlers during `install`. One handler per point."""

    def __init__(self) -> None:
        self._signup_credit: SignupCreditHandler | None = None

    def on_signup_credit(self, handler: SignupCreditHandler) -> None:
        if self._signup_credit is not None:
            raise RuntimeError("a signup_credit handler is already registered")
        self._signup_credit = handler

    async def signup_credit(self, ctx: SignupCreditContext) -> CreditDecision:
        """Default GRANT, also when the handler is slow, raises or answers anything else.

        The budget cancels a handler at its next await; a handler that blocks the event loop is
        not interrupted, so handlers must not block.
        """
        if self._signup_credit is None:
            return CreditDecision.GRANT
        try:
            async with asyncio.timeout(SIGNUP_CREDIT_BUDGET_S):
                decision = await self._signup_credit(ctx)
        except Exception as exc:  # noqa: BLE001 - any extension failure takes the default
            return _failed("signup_credit", type(exc).__name__, CreditDecision.GRANT)
        if not isinstance(decision, CreditDecision):
            return _failed("signup_credit", "BadOutcome", CreditDecision.GRANT)
        return decision


def _failed(point: str, error: str, fallback):
    """Report an extension failure and return the point's default. Only the exception TYPE is
    recorded: an extension's message might name one of its private rules."""
    _log.warning("event=policy_failed point=%s error=%s outcome=%s", point, error, fallback)
    analytics.capture_fault(component="policy", fault_type="PolicyFailed", value=f"{point} {error}")
    return fallback


class Extension(Protocol):
    api_version: int

    def install(self, app: FastAPI, registry: PolicyRegistry) -> None: ...


# Process-wide: the handlers of the most recent `load_extensions`. A process serves one app, so
# one registry; tests that build several apps restore it themselves.
registry = PolicyRegistry()


def load_extensions(app: FastAPI, required: str = "") -> list[str]:
    """Install every `treg.extensions` entry point into a fresh registry and return their names.

    Refuses to start when an extension targets another API version, fails to install, or a name
    in `required` is not installed.
    """
    global registry
    fresh = PolicyRegistry()
    names: list[str] = []
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        extension: Extension = ep.load()
        version = getattr(extension, "api_version", None)
        if version != POLICY_API_VERSION:
            raise RuntimeError(
                f"extension {ep.name!r} targets policy API {version!r}, treg provides {POLICY_API_VERSION}")
        extension.install(app, fresh)
        names.append(ep.name)
    missing = {name.strip() for name in required.split(",") if name.strip()} - set(names)
    if missing:
        raise RuntimeError(f"required extensions not installed: {', '.join(sorted(missing))}")
    registry = fresh
    if names:
        _log.info("event=policy_extensions_loaded names=%s api=%d", ",".join(names), POLICY_API_VERSION)
    return names
