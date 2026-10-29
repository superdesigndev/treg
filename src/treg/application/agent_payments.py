"""Agent payments: a caller with no treg credential pays a catalog call per call
(docs/context/architecture/agent-payments.md).

A payer is a wallet address with a payer team. A payment confirmed on chain is an ordinary
`topup` on that team, then the call runs the normal road as the team: reserve, relay, settle or
release. What a call does not use stays as the team's credit, and a later call whose quote the
credit covers proves key control with a fresh payment that treg never settles.

No database session is open while a rail verifies or settles. This module is the only writer of
`AgentPayment`; `governance.teams.make_payer_team` stages the payer team.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..config import get_settings
from ..domain import money as ledger
from ..domain.governance.teams import make_payer_team
from ..domain.identity.access import Caller
from ..infra.agent_payments import Offer, PaymentRail, PaymentRejected, Settlement, Verified
from ..infra.db import session_maker
from ..models import AgentPayer, AgentPayment, Membership, Org, User, _now
from .call.resolve import QueryValues, _catalog_endpoint_for, platform_quote_micro

log = logging.getLogger("treg.agent_payments")

_rails: tuple[PaymentRail, ...] = ()


def configure(rails: Sequence[PaymentRail]) -> None:
    """Install the rails this process offers (bootstrap, or a test)."""
    global _rails
    _rails = tuple(rails)


# A keyless caller is not yet anyone; its body is read only for a payable catalog call, and only
# this much of it.
MAX_BODY_BYTES = 1 << 20


def serves(rest: str, method: str) -> bool:
    """A cheap first check, before any body is read: is `rest` a catalog id for this method?"""
    ep = _catalog_endpoint_for(rest)
    return ep is not None and method.upper() == (ep.get("method") or "GET").upper()


def enabled() -> bool:
    return bool(get_settings().agent_payments_enabled and _rails)


@dataclass(frozen=True)
class PaidOffer:
    endpoint_id: str
    quote_micro: int     # what the call will reserve, margin included
    offer: Offer         # what one payment must carry: max(quote, the payment floor)


@dataclass(frozen=True)
class PayerAdmission:
    caller: Caller
    verified: Verified
    settlement: Settlement | None   # None: the call draws credit the payer already holds


class PaymentPending(Exception):
    """The payment may have moved but treg could not confirm it; no call was made."""

    def __init__(self, detail: dict) -> None:
        super().__init__(detail["message"])
        self.detail = detail


class PaymentChallenge(Exception):
    """The 402 that asks for a payment. Carries every rail's challenge headers."""

    def __init__(self, detail: dict, headers: list[tuple[str, str]]) -> None:
        super().__init__(detail.get("message", "payment required"))
        self.detail = detail
        self.headers = headers


def consumes(name: str, value: str) -> bool:
    """True for a request header that carries a payment credential and must never be relayed."""
    lowered = name.lower()
    for rail in _rails:
        if lowered in rail.consumed_headers:
            return lowered != "authorization" or value[:8].lower() == "payment "
    return False


async def quote(*, rest: str, method: str, path: str, query: str,
                query_items: tuple[tuple[str, str], ...], body: bytes) -> PaidOffer | None:
    """The price of this call for a payer, or None when the payment path does not serve it
    (not a catalog id, free, routed, own-account, or not on treg's key here)."""
    ep = _catalog_endpoint_for(rest)
    if ep is None or method.upper() != (ep.get("method") or "GET").upper():
        return None
    raw = platform_quote_micro(ep, QueryValues(query_items), body)
    if raw is None:
        return None
    charged = ledger.with_margin(raw)
    if charged <= 0:
        return None
    settings = get_settings()
    base = settings.public_url.rstrip("/")
    amount = max(charged, settings.agent_payments_min_payment_micro)
    offer = Offer(
        amount_micro=amount, method=method.upper(), path=path, query=query,
        resource_url=f"{base}{path}" + (f"?{query}" if query else ""),
        description=f"{ep['id']} on treg")
    return PaidOffer(endpoint_id=ep["id"], quote_micro=charged, offer=offer)


async def challenge(paid: PaidOffer, *, reason: str = "", message: str = "") -> PaymentChallenge:
    headers: list[tuple[str, str]] = []
    for rail in _rails:
        try:
            headers += await rail.challenge(paid.offer)
        except Exception:
            log.exception("agent payments: %s challenge failed", rail.protocol)
    detail = {
        "error": "payment_required",
        "endpoint_id": paid.endpoint_id,
        "amount_micro": paid.offer.amount_micro,
        "estimated_cost_micro": paid.quote_micro,
        "protocols": [rail.protocol for rail in _rails],
        "message": (
            f"{paid.endpoint_id} costs ~${ledger.usd(paid.quote_micro):g}. Pay "
            f"${ledger.usd(paid.offer.amount_micro):g} with MPP (WWW-Authenticate: Payment) or "
            "x402 (PAYMENT-REQUIRED) and send the request again; what the call does not use stays "
            "as credit for this wallet's next calls. Or sign in and use a treg key."),
    }
    if reason:
        detail["reason"] = reason
        detail["message"] = f"{message or reason}. " + detail["message"]
    return PaymentChallenge(detail, headers)


async def admit(paid: PaidOffer, headers: Mapping[str, str]) -> PayerAdmission:
    """Verify the presented payment, settle it unless credit covers the call, and return the
    payer team to call as. Raises PaymentChallenge when the payment cannot be used."""
    presented = [(rail, cred) for rail in _rails if (cred := rail.presented(headers))]
    if not presented:
        raise await challenge(paid)
    if len(presented) > 1:
        raise await challenge(paid, reason="ambiguous_payment",
                              message="send one payment credential, not several")
    rail, credential = presented[0]
    try:
        verified = await rail.verify(credential, paid.offer)
    except PaymentRejected as exc:
        raise await challenge(paid, reason=exc.code, message=exc.message) from exc

    org_id = await _payer_org(verified.payer)
    payment_id, draw = await _claim(verified, org_id, paid.quote_micro)
    if draw is None:
        raise await challenge(paid, reason="payment_replayed",
                              message="this payment was already used")
    settlement = None
    if not draw:
        # A settlement outlives the request: a caller that disconnects mid-settlement must still
        # be credited for a transfer that lands, so the work runs as its own task.
        work = asyncio.ensure_future(_settle_and_credit(rail, verified, paid, payment_id, org_id))
        _inflight.add(work)
        work.add_done_callback(_forget)
        try:
            settlement = await asyncio.shield(work)
        except PaymentRejected as exc:
            if exc.code == "settlement_unknown":
                raise PaymentPending({
                    "error": "payment_outcome_unknown", "endpoint_id": paid.endpoint_id,
                    "message": ("treg could not confirm the settlement of this payment, so the "
                                "call was not made. If the transfer lands it is kept as this "
                                "wallet's credit; do not send this payment again."),
                }) from exc
            raise await challenge(paid, reason=exc.code, message=exc.message) from exc
    return PayerAdmission(caller=await _caller(org_id), verified=verified, settlement=settlement)


_inflight: set[asyncio.Task] = set()


def _forget(task: asyncio.Task) -> None:
    _inflight.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.info("agent payments: settlement ended with %s", type(task.exception()).__name__)


async def _settle_and_credit(rail: PaymentRail, verified: Verified, paid: PaidOffer,
                             payment_id: int | None, org_id: int) -> Settlement:
    """Settle, then top up. A definite refusal marks the proof `failed`; an unknown outcome leaves
    it `settling` for reconciliation, because the transfer may still land."""
    try:
        settlement = await rail.settle(verified, paid.offer)
    except PaymentRejected as exc:
        status = "settling" if exc.code == "settlement_unknown" else "failed"
        await _mark(payment_id, status=status, error=exc.code)
        raise
    await _credit(payment_id, verified, org_id, settlement)
    return settlement


async def _payer_org(address: str) -> int:
    for _ in range(2):
        async with session_maker() as db:
            payer = await db.get(AgentPayer, address)
            if payer is not None:
                return payer.org_id
            org = await make_payer_team(db, address)
            try:
                await db.commit()
                return int(org.id)
            except IntegrityError:
                await db.rollback()  # a concurrent first payment from this address created it
    raise RuntimeError("payer team could not be created")


async def _claim(verified: Verified, org_id: int, quote_micro: int) -> tuple[int | None, bool | None]:
    """Record the proof as used, deciding whether it draws credit or must settle. A proof seen
    before returns `(None, None)`: the unique `proof_id` is what refuses a replay."""
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        draw = verified.deferrable and balance >= quote_micro
        row = AgentPayment(
            proof_id=verified.proof_id, protocol=verified.protocol, network=verified.network,
            payer_address=verified.payer, org_id=org_id, amount_micro=verified.amount_micro,
            status="credit" if draw else "settling")
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return None, None
        return row.id, draw


async def _mark(payment_id: int | None, *, status: str, error: str | None = None,
                reference: str | None = None) -> None:
    async with session_maker() as db:
        row = await db.get(AgentPayment, payment_id)
        if row is not None:
            row.status, row.error, row.reference, row.updated_at = status, error, reference, _now()
            db.add(row)
            await db.commit()


async def _credit(payment_id: int | None, verified: Verified, org_id: int,
                  settlement: Settlement) -> None:
    async with session_maker() as db:
        await ledger.topup(
            db, org_id, verified.amount_micro, f"agentpay:{verified.proof_id}",
            meta={"source": "agent_payment", "protocol": verified.protocol,
                  "network": verified.network, "reference": settlement.reference})
        row = await db.get(AgentPayment, payment_id)
        row.status, row.reference, row.updated_at = "settled", settlement.reference, _now()
        db.add(row)
        await db.commit()


async def _caller(org_id: int) -> Caller:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        membership = (await db.execute(
            select(Membership).where(Membership.org_id == org_id))).scalars().first()
        user = await db.get(User, membership.user_id)
        return Caller(membership=membership, user=user, org=org, api_key=None)


def payment_headers(admission: PayerAdmission) -> list[tuple[str, str]]:
    """Every answer after a payment says which money the call used, and the settlement
    transaction when this payment settled, whatever the call's outcome."""
    if admission.settlement is None:
        return [("X-Treg-Payment", "credit")]
    return [("X-Treg-Payment", "settled"),
            ("X-Treg-Payment-Reference", admission.settlement.reference)]


def receipt_headers(admission: PayerAdmission) -> list[tuple[str, str]]:
    """The protocol receipt, for a 2xx answer only (the MPP draft sends none on errors)."""
    return list(admission.settlement.receipt_headers) if admission.settlement else []
