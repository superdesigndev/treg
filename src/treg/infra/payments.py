"""pympp, the x402 and MPP payment rail. The only module that imports it.

Builds one payment server from settings and splits each payment in two: the route handler
validates a credential without moving money, and the caller takes the payment later
(`Pending.take`) only when the call it paid for turned out billable.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

from mpp import Credential, Receipt
from mpp.errors import VerificationFailedError
from mpp.server import Mpp, PaymentRequired, PaymentVerified, Request, Response
from mpp.server.method import ServerMethod, VerifyContext

from ..config import get_settings

# A claimed credential is kept past the longest validity an EVM authorization may carry
# (pympp's default, one hour), so a burned credential cannot be presented again.
_CLAIM_TTL_S = 2 * 3600

_pending: contextvars.ContextVar[Pending | None] = contextvars.ContextVar(
    "treg_payment_pending", default=None)


class PaymentFailed(Exception):
    """The terminal payment operation failed after validation passed."""


@dataclass
class Pending:
    """One validated payment whose terminal operation has not run."""

    method: ServerMethod | None = None
    context: VerifyContext | None = None
    broadcast: Any = None
    payer: str | None = None

    async def take(self) -> Receipt:
        if self.context is None or self.broadcast is None:
            raise PaymentFailed("no validated payment to take")
        try:
            receipt = await self.broadcast(self.context)
        except Exception as exc:
            raise PaymentFailed(str(exc) or type(exc).__name__) from exc
        if receipt.payer is None and self.payer is not None:
            receipt = replace(receipt, payer=self.payer)
        return receipt


def _payload_kind(payload: Any) -> str | None:
    root = getattr(payload, "root", payload)
    if isinstance(root, dict):
        return root.get("type")
    return getattr(root, "type", None)


def _deferred(method: ServerMethod) -> ServerMethod:
    if method.broadcast is None or method.validate is None:
        raise ValueError(f"{method.key} cannot defer its payment")
    original = method.broadcast

    async def broadcast(context: VerifyContext) -> Receipt:
        pending = _pending.get()
        if pending is None:
            return await original(context)
        # A pushed Tempo transaction has already moved the payer's money: it could not be left
        # untaken when the call is not billable.
        if _payload_kind(context.credential.payload) == "hash":
            raise VerificationFailedError(
                "pay per call needs a signed transaction for the server to broadcast, "
                "not the hash of one already sent")
        pending.method, pending.context, pending.broadcast = method, context, original
        return Receipt(status="success", timestamp=datetime.now(timezone.utc),
                       reference="pending", method=method.name)

    return replace(method, broadcast=broadcast)


@dataclass(frozen=True)
class Validated:
    """A credential that passed validation, with the payment still to take."""

    verified: PaymentVerified
    pending: Pending

    @property
    def claim_key(self) -> str:
        """The payment itself, not its wrapping: one signed payment is one claim."""
        payload = json.dumps(self.verified.credential.payload, sort_keys=True, default=str)
        return "claim:" + hashlib.sha256(payload.encode()).hexdigest()

    def receipt_headers(self, receipt: Receipt) -> dict[str, str]:
        carrier = PaymentVerified(credential=self.verified.credential, receipt=receipt)
        return dict(carrier.with_receipt(Response()).headers)


def _pushed_hash(authorization: str | None) -> bool:
    if not authorization or not authorization.lower().startswith("payment "):
        return False
    try:
        credential = Credential.from_authorization(authorization)
    except Exception:  # noqa: BLE001 - a malformed credential is pympp's to reject
        return False
    return _payload_kind(credential.payload) == "hash"


class Payments:
    def __init__(self, server: Mpp, store: Any, *, sponsored: bool = False) -> None:
        self._server = server
        self._store = store
        self._sponsored = sponsored

    @property
    def methods(self) -> list[str]:
        return sorted({method.name for method in self._server.methods})

    async def validate(self, request: Request, *, price_micro: int, description: str,
                       meta: dict[str, str]) -> PaymentRequired | Validated:
        """The 402 to send, or the validated credential. Moves no money."""
        amount = str(Decimal(price_micro) / Decimal(1_000_000))
        options = {"amount": amount, "description": description, "meta": meta}
        if self._sponsored:
            # Tempo offers sponsor the payer's fees; EVM offers ignore the option.
            options["fee_payer"] = True
        if _pushed_hash(request.headers.get("authorization")):
            # Refused before pympp validates it: a pushed transaction has already moved the
            # payer's money, so it could not be left untaken when the call is not billable.
            request = Request(method=request.method, url=request.url,
                              headers=[(k, v) for k, v in request.headers.multi_items()
                                       if k.lower() != "authorization"])
        pending = Pending()
        token = _pending.set(pending)
        try:
            result = await self._server.charge(**options)(request)
        finally:
            _pending.reset(token)
        if isinstance(result, PaymentRequired):
            return result
        pending.payer = result.receipt.payer
        return Validated(verified=result, pending=pending)

    async def claim(self, validated: Validated) -> bool:
        """Claim a credential once, before the call it pays for, and keep the claim."""
        return await self._store.put_if_absent_until(
            validated.claim_key, "1", time.time() + _CLAIM_TTL_S)


def _build() -> Payments:
    from redis.asyncio import from_url
    from mpp.stores import RedisStore

    settings = get_settings()
    store = RedisStore(from_url(settings.kv_url), key_prefix="treg:pay:")
    methods: list[ServerMethod] = []
    if settings.mpp_enabled:
        from mpp.methods.tempo import TempoAccount
        from mpp.methods.tempo import server as tempo

        methods += tempo.charge(
            recipient=settings.mpp_tempo_recipient,
            chain_id=settings.mpp_tempo_chain_id,
            rpc_url=settings.mpp_tempo_rpc_url or None,
            currencies=settings.mpp_tempo_currency_list or None,
            fee_payer=(TempoAccount.from_key(settings.mpp_tempo_fee_payer_key)
                       if settings.mpp_tempo_fee_payer_key else None),
            store=store,
        )
    if settings.x402_enabled:
        from mpp.methods.evm import BASE_SEPOLIA_USDC, BASE_USDC
        from mpp.methods.evm import server as evm
        from mpp.x402.facilitator import HttpFacilitator

        if settings.x402_cdp_api_key_id:
            from mpp.x402 import cdp

            facilitator = cdp.facilitator(settings.x402_cdp_api_key_id,
                                          settings.x402_cdp_api_key_secret)
        else:
            facilitator = HttpFacilitator(
                settings.x402_facilitator_url,
                headers=({"Authorization": settings.x402_facilitator_authorization}
                         if settings.x402_facilitator_authorization else None))
        methods.append(evm.charge(
            currency=BASE_USDC if settings.x402_network == "base" else BASE_SEPOLIA_USDC,
            recipient=settings.x402_recipient,
            facilitator=facilitator,
            # Standard x402 clients bind a payment by the echoed terms and resource URL.
            route_binding="resource",
            store=store,
        ))
    server = Mpp(methods=[_deferred(method) for method in methods],
                 realm=settings.payments_realm, secret_key=settings.payments_secret_key,
                 store=store)
    return Payments(server, store, sponsored=bool(settings.mpp_tempo_fee_payer_key))


def enabled() -> bool:
    settings = get_settings()
    return settings.x402_enabled or settings.mpp_enabled


@lru_cache(maxsize=1)
def payments() -> Payments:
    return _build()


def to_request(method: str, url: str, headers: Any) -> Request:
    return Request(method=method, url=url, headers=headers)
