"""Agent payments: a caller with no treg credential pays a catalog call per call
(docs/context/architecture/agent-payments.md).

A payer is a wallet address with a payer team. A payment confirmed on chain is an ordinary
`topup` on that team, then the call runs the normal road as the team: reserve, relay, settle or
release. What a call does not use stays as the team's credit, and a later call whose quote the
credit covers proves key control with a fresh payment that treg never settles.

No database session is open while a rail verifies, settles or looks a payment up. This module is
the only writer of `AgentPayment`; `governance.teams.make_payer_team` stages the payer team.
`reconcile` (run by `treg-worker agentpay reconcile`) resolves the payments whose settlement went
unanswered and, when payments settle to Stripe, records each settled one there.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..config import get_settings
from ..domain import money as ledger
from ..domain.governance.teams import make_payer_team
from ..domain.identity.access import Caller
from ..infra import stripe as stripe_api
from ..infra.agent_payments import (
    Offer, PaymentRail, PaymentRejected, Settlement, Unconfirmed, Verified)
from ..infra.db import session_maker
from ..models import AgentPayer, AgentPayment, Membership, Org, User, _now
from .call.resolve import QueryValues, _catalog_endpoint_for, platform_quote

log = logging.getLogger("treg.agent_payments")

_rails: tuple[PaymentRail, ...] = ()


def configure(rails: Sequence[PaymentRail]) -> None:
    """Install the rails this process offers (bootstrap, or a test)."""
    global _rails
    _rails = tuple(rails)


# A keyless caller is not yet anyone; its body is read only for a payable catalog call, and only
# this much of it.
MAX_BODY_BYTES = 1 << 20


def unserved(rest: str, method: str) -> str:
    """A cheap first check, before any body is read: "" when `rest` is a catalog id for this
    method, else the reason."""
    ep = _catalog_endpoint_for(rest)
    if ep is None:
        return "not_a_catalog_tool"
    return "" if method.upper() == (ep.get("method") or "GET").upper() else "wrong_method"


CENT_MICRO = 10_000


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
                query_items: tuple[tuple[str, str], ...], body: bytes) -> PaidOffer:
    """The price of this call for a payer. Raises NotServed when the payment path does not serve
    it (not a catalog id, free, routed, own-account, or not on treg's key here)."""
    if reason := unserved(rest, method):
        raise NotServed(reason)
    ep = _catalog_endpoint_for(rest)
    raw, reason = platform_quote(ep, QueryValues(query_items), body)
    if reason:
        raise NotServed(reason)
    charged = ledger.with_margin(raw)
    if charged <= 0:
        raise NotServed("free")
    settings = get_settings()
    base = settings.public_url.rstrip("/")
    amount = max(charged, settings.agent_payments_min_payment_micro)
    if settings.agent_payments_settlement == "stripe":
        amount = -(-amount // CENT_MICRO) * CENT_MICRO  # Stripe records whole cents
    offer = Offer(
        amount_micro=amount, method=method.upper(), path=path, query=query,
        resource_url=f"{base}{path}" + (f"?{query}" if query else ""),
        description=f"{ep['id']} on treg")
    return PaidOffer(endpoint_id=ep["id"], quote_micro=charged, offer=offer)


_HOW_TO_PAY = {"mpp": "MPP (WWW-Authenticate: Payment)", "x402": "x402 (PAYMENT-REQUIRED)"}


async def challenge(paid: PaidOffer, *, reason: str = "", message: str = "") -> PaymentChallenge:
    headers: list[tuple[str, str]] = []
    for rail in _rails:
        try:
            headers += await rail.challenge(paid.offer)
        except Exception:
            log.exception("agent payments: %s challenge failed", rail.protocol)
    ways = " or ".join(_HOW_TO_PAY.get(rail.protocol, rail.protocol) for rail in _rails)
    minimum = get_settings().agent_payments_min_payment_micro
    amount, hold = paid.offer.amount_micro, paid.quote_micro
    why = (f"the payment minimum, ${ledger.usd(minimum):g}" if amount > hold
           else "the highest price this call can cost")
    detail = {
        "error": "payment_required",
        "reason": reason or "payment_required",
        "endpoint_id": paid.endpoint_id,
        "amount_micro": amount,
        "estimated_cost_micro": hold,
        "minimum_payment_micro": minimum,
        "protocols": [rail.protocol for rail in _rails],
        "message": (
            f"Pay ${ledger.usd(amount):g} ({why}) with {ways} and send the same request again. "
            f"The call holds up to ${ledger.usd(hold):g} and is charged only its real price; a "
            "failed call or a call with no billable result is charged nothing. What the call does "
            "not use stays as credit for this wallet's next calls and is not refunded. Or sign in "
            "and use a treg key."),
    }
    if reason:
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
    await _credit(payment_id, settlement.reference)
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
            asset=verified.asset, pay_to=verified.pay_to, expires_at=verified.expires_at,
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


async def _credit(payment_id: int | None, reference: str) -> None:
    """Top up the payer team for a confirmed transfer and mark the proof settled, in one
    transaction. The block reference `agentpay:<proof id>` makes a second credit a no-op, so the
    request and the reconciler may both arrive here."""
    async with session_maker() as db:
        row = await db.get(AgentPayment, payment_id)
        await ledger.topup(
            db, row.org_id, row.amount_micro, f"agentpay:{row.proof_id}",
            meta={"source": "agent_payment", "protocol": row.protocol,
                  "network": row.network, "reference": reference})
        row.status, row.reference, row.error, row.updated_at = "settled", reference, None, _now()
        db.add(row)
        await db.commit()


async def _caller(org_id: int) -> Caller:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        membership = (await db.execute(
            select(Membership).where(Membership.org_id == org_id))).scalars().first()
        user = await db.get(User, membership.user_id)
        return Caller(membership=membership, user=user, org=org, api_key=None)


_NOT_SERVED = {
    "not_a_catalog_tool": "only a catalog tool can be paid for with a wallet",
    "wrong_method": "this tool takes another HTTP method",
    "routed": "a routed treg tool needs a treg account",
    "unavailable": "this tool is not available now",
    "needs_connection": "this tool runs on a connected account, which needs a treg account",
    "paused": "this provider is paused on treg's key now",
    "free": "this call is free, and free calls need a treg account",
    "own_key_only": "this tool runs only on a team's own key",
    "invalid_request": "treg cannot price this request; check the tool's parameters",
}


class NotServed(Exception):
    """The payment path does not serve this call; it answers 401, with the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def detail(self) -> dict:
        base = get_settings().public_url.rstrip("/")
        return {"error": "not_authenticated", "reason": self.reason,
                "message": f"{_NOT_SERVED.get(self.reason, self.reason)}. Sign in at {base} "
                           "and send a treg key."}


@dataclass(frozen=True)
class PaidCall:
    """A paid call ready to run: who it runs as, the headers it may relay, and what every answer
    adds."""
    caller: Caller
    headers: list[tuple[bytes, bytes]]   # the request's headers minus the payment credential
    admission: PayerAdmission

    async def answer_headers(self) -> list[tuple[str, str]]:
        """Which money paid, and the credit left once the call has settled or released."""
        async with session_maker() as db:
            credit = await ledger.balance_of(db, int(self.caller.org.id))
        return [*payment_headers(self.admission), ("X-Treg-Credit-Micro", str(max(credit, 0)))]

    def receipt_headers(self, status: int) -> list[tuple[str, str]]:
        return receipt_headers(self.admission) if 200 <= status < 300 else []


async def open_call(*, rest: str, method: str, path: str, query: str,
                    query_items: tuple[tuple[str, str], ...], body: bytes,
                    headers: Mapping[str, str],
                    raw_headers: Sequence[tuple[bytes, bytes]]) -> PaidCall:
    """Price the call, take its payment or draw credit, and return the call to run. Raises
    NotServed, PaymentChallenge or PaymentPending; no call has run when it raises."""
    paid = await quote(rest=rest, method=method, path=path, query=query,
                       query_items=query_items, body=body)
    admission = await admit(paid, headers)
    relayed = [(name, value) for name, value in raw_headers
               if not consumes(name.decode("latin-1"), value.decode("latin-1"))]
    return PaidCall(caller=admission.caller, headers=relayed, admission=admission)


def payment_headers(admission: PayerAdmission) -> list[tuple[str, str]]:
    """Every answer after a payment says which money the call used, and the settlement
    transaction when this payment settled, whatever the call's outcome."""
    if admission.settlement is None:
        return [("X-Treg-Payment", "credit")]
    return [("X-Treg-Payment", "paid"),
            ("X-Treg-Paid-Micro", str(admission.verified.amount_micro)),
            ("X-Treg-Payment-Reference", admission.settlement.reference)]


def receipt_headers(admission: PayerAdmission) -> list[tuple[str, str]]:
    """The protocol receipt, for a 2xx answer only (the MPP draft sends none on errors)."""
    return list(admission.settlement.receipt_headers) if admission.settlement else []


@dataclass
class ReconcileResult:
    landed: int = 0
    failed: int = 0
    pending: int = 0
    recorded: int = 0
    record_errors: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


async def reconcile(*, limit: int = 50) -> ReconcileResult:
    """Resolve `settling` payments the request path could not confirm: credit the ones whose
    transfer landed, fail the ones that can no longer land. Then, when payments settle to
    Stripe, record each settled one there. Chain and Stripe I/O run with no session open."""
    settings = get_settings()
    result = ReconcileResult()
    cutoff = _now() - timedelta(seconds=settings.agent_payments_reconcile_min_age_s)
    async with session_maker() as db:
        rows = (await db.execute(
            select(AgentPayment).where(AgentPayment.status == "settling",
                                       AgentPayment.created_at < cutoff)
            .order_by(AgentPayment.created_at).limit(limit))).scalars().all()
        stuck = [(row.id, row.protocol, row.network, _unconfirmed(row)) for row in rows]
    for payment_id, protocol, network, payment in stuck:
        rail = _rail(protocol, network)
        if rail is None:
            result.pending += 1
            _count(result, "rail_not_configured")
            continue
        lookup = await rail.find(payment)
        _count(result, lookup.reason or lookup.state)
        if lookup.state == "landed":
            await _credit(payment_id, lookup.reference)
            result.landed += 1
        elif lookup.state == "absent":
            await _resolve_failed(payment_id, lookup.reason or "absent", lookup.reference)
            result.failed += 1
        else:
            result.pending += 1
    if settings.agent_payments_settlement == "stripe":
        await _record_to_stripe(result, limit=limit, api_key=settings.stripe_secret_key)
    return result


def _unconfirmed(row: AgentPayment) -> Unconfirmed:
    return Unconfirmed(proof_id=row.proof_id, payer=row.payer_address,
                       amount_micro=row.amount_micro, asset=row.asset, pay_to=row.pay_to,
                       created_at=row.created_at, expires_at=row.expires_at)


def _rail(protocol: str, network: str) -> PaymentRail | None:
    return next((r for r in _rails if r.protocol == protocol and r.network == network), None)


def _count(result: ReconcileResult, reason: str) -> None:
    result.reasons[reason] = result.reasons.get(reason, 0) + 1


async def _resolve_failed(payment_id: int, reason: str, reference: str | None) -> None:
    """Only a row still `settling` fails: a request that credited it meanwhile wins."""
    async with session_maker() as db:
        row = await db.get(AgentPayment, payment_id)
        if row is None or row.status != "settling":
            return
        row.status, row.error, row.reference, row.updated_at = "failed", reason, reference, _now()
        db.add(row)
        await db.commit()


async def _record_to_stripe(result: ReconcileResult, *, limit: int, api_key: str) -> None:
    """Each payment settled to a current Stripe deposit address becomes one PaymentIntent. A
    payment settled to an address treg no longer offers (an earlier wallet) is not Stripe's."""
    destinations = {(r.protocol, r.network): (r.pay_to, r.stripe_network)
                    for r in _rails if r.stripe_network}
    async with session_maker() as db:
        rows = (await db.execute(
            select(AgentPayment).where(AgentPayment.status == "settled",
                                       AgentPayment.stripe_payment_intent.is_(None),
                                       AgentPayment.reference.is_not(None))
            .order_by(AgentPayment.created_at).limit(limit))).scalars().all()
        due = [(row.id, row.amount_micro, row.reference, destinations.get((row.protocol, row.network)))
               for row in rows if (row.protocol, row.network) in destinations
               and row.pay_to == destinations[(row.protocol, row.network)][0]]
    for payment_id, amount_micro, reference, (_, network) in due:
        try:
            intent = await stripe_api.record_crypto_payment(
                amount_cents=amount_micro // CENT_MICRO, network=network,
                transaction_hash=reference, api_key=api_key)
        except Exception:
            log.exception("agent payments: recording payment %s to Stripe failed", payment_id)
            result.record_errors += 1
            continue
        async with session_maker() as db:
            row = await db.get(AgentPayment, payment_id)
            row.stripe_payment_intent, row.updated_at = str(intent.get("id") or ""), _now()
            db.add(row)
            await db.commit()
        result.recorded += 1


async def create_stripe_deposit_address(network: str) -> dict:
    """Ops setup for settlement `stripe`: one reusable deposit address per network, set as the
    rail's recipient. Never called on the request path."""
    return await stripe_api.create_crypto_deposit_address(
        network, api_key=get_settings().stripe_secret_key)
