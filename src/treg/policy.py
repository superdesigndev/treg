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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol

from . import analytics

if TYPE_CHECKING:
    from fastapi import FastAPI

POLICY_API_VERSION = 1
ENTRY_POINT_GROUP = "treg.extensions"
SIGNUP_CREDIT_BUDGET_S = 0.3
CALL_ADMISSION_BUDGET_S = 0.02
_FAILED_LOG_EVERY_S = 10.0

_log = logging.getLogger("treg")
_failed_logged: dict[tuple[str, str], tuple[float | None, int]] = {}


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


@dataclass(frozen=True)
class CallAdmissionContext:
    """treg's own money is about to pay for one call: its key, or its pay-per-use OAuth app.

    Asked once the call has resolved and before anything is reserved, with no database session open.
    A call paid with the team's own credential never asks.
    """
    org_id: int
    user_id: int
    caller_email: str  # the calling identity: a person, or an agent identity
    owner_email: str | None  # the team's first owner, the person behind every agent identity
    org_has_paid: bool  # the team has bought credit at least once
    org_created_at: datetime | None
    api_key_id: int | None
    endpoint_id: str
    provider: str
    platform: str  # the catalog row's platform, for example `video-gen`; empty when it has none
    funding: str  # `platform` (treg's key) or `billed_oauth` (treg's pay-per-use OAuth app)
    estimate_micro: int


class CallDecision(StrEnum):
    ALLOW = "allow"
    # Refused before anything is reserved: a 403 that tells the caller to add funds or bring its
    # own key, never which rule decided.
    DENY = "deny"


SignupCreditHandler = Callable[[SignupCreditContext], Awaitable[CreditDecision]]
CallAdmissionHandler = Callable[[CallAdmissionContext], Awaitable[CallDecision]]


class PolicyRegistry:
    """Where an extension registers its handlers during `install`. One handler per point.

    Every point takes its default when the handler is slow, raises or answers anything else. The
    budget cancels a handler at its next await; a handler that blocks the event loop is not
    interrupted, so handlers must not block.
    """

    def __init__(self) -> None:
        self._signup_credit: SignupCreditHandler | None = None
        self._call_admission: CallAdmissionHandler | None = None

    def on_signup_credit(self, handler: SignupCreditHandler) -> None:
        if self._signup_credit is not None:
            raise RuntimeError("a signup_credit handler is already registered")
        self._signup_credit = handler

    def on_call_admission(self, handler: CallAdmissionHandler) -> None:
        if self._call_admission is not None:
            raise RuntimeError("a call_admission handler is already registered")
        self._call_admission = handler

    @property
    def asks_call_admission(self) -> bool:
        """Whether a handler listens, so treg reads the point's context only when one does."""
        return self._call_admission is not None

    async def signup_credit(self, ctx: SignupCreditContext) -> CreditDecision:
        """Default GRANT."""
        return await _ask("signup_credit", self._signup_credit, ctx, SIGNUP_CREDIT_BUDGET_S,
                          CreditDecision.GRANT)

    async def call_admission(self, ctx: CallAdmissionContext) -> CallDecision:
        """Default ALLOW."""
        return await _ask("call_admission", self._call_admission, ctx, CALL_ADMISSION_BUDGET_S,
                          CallDecision.ALLOW)


async def _ask(point: str, handler, ctx, budget_s: float, default):
    if handler is None:
        return default
    try:
        async with asyncio.timeout(budget_s):
            decision = await handler(ctx)
    except Exception as exc:  # noqa: BLE001 - any extension failure takes the default
        return _failed(point, type(exc).__name__, default)
    if not isinstance(decision, type(default)):
        return _failed(point, "BadOutcome", default)
    return decision


def _failed(point: str, error: str, fallback):
    """Report an extension failure and return the point's default. Only the exception TYPE is
    recorded: an extension's message might name one of its private rules.

    `call_admission` fails at the call rate when an extension breaks, so the log line is written at
    most once per `_FAILED_LOG_EVERY_S` for each point and error, counting the ones it held back.
    The fault event rolls repeats up on its own (`analytics.capture_fault`)."""
    now = time.monotonic()
    last, held = _failed_logged.get((point, error), (None, 0))
    if last is None or now - last >= _FAILED_LOG_EVERY_S:
        _log.warning("event=policy_failed point=%s error=%s outcome=%s suppressed=%d",
                     point, error, fallback, held)
        _failed_logged[(point, error)] = (now, 0)
    else:
        _failed_logged[(point, error)] = (last, held + 1)
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
