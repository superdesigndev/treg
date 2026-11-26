"""Agent payments: a caller with no treg credential pays a catalog call per call
(docs/context/architecture/agent-payments.md).

A payer is a wallet address on one network with a payer team. A payment confirmed on chain is
an ordinary `topup` on that team, then the call runs the normal road as the team: reserve, relay,
settle or release. What a call does not use stays as the team's credit. A later call whose quote
the credit covers proves the wallet instead of paying: on MPP with a zero-amount proof (no money
moves); on x402 with a fresh, short-lived payment that treg does not settle, which the reconciler
checks after it expires and credits if it was settled anyway.

No database session is open while a rail verifies, settles or looks a payment up. This module is
the only writer of `AgentPayment`; `governance.teams.make_payer_team` stages the payer team.
`reconcile` (run by `treg-worker agentpay reconcile`) resolves the payments whose settlement went
unanswered, closes the expired ones that drew credit, and, when payments settle to Stripe, records
each settled one there.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

from sqlalchemy import and_, false, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select, update

from .. import ratestore
from ..config import get_settings
from ..domain import money as ledger
from ..domain.governance.teams import make_payer_team
from ..domain.identity.access import Caller
from ..infra import stripe as stripe_api
from ..infra.agent_payments import (
    Offer, PaymentRail, PaymentRejected, Settlement, Unconfirmed, Verified, micro_to_decimal)
from ..infra.agent_payments.tempo import CONSUMED_HEADERS as MPP_HEADERS
from ..infra.agent_payments.x402 import CONSUMED_HEADERS as X402_HEADERS
from ..infra.db import session_maker
from ..models import AgentPayer, AgentPayment, Membership, Org, User, _now
from .call.resolve import (
    QueryValues, _catalog_endpoint_for, async_utility, platform_quote, wallet_refusal)

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
BPS = 10_000
# The rails give the chain this long past `expires_at` before calling a payment absent.
CREDIT_PROOF_GRACE = timedelta(seconds=90)
PAYMENT_HEADERS = frozenset(MPP_HEADERS + X402_HEADERS)


def enabled() -> bool:
    return bool(get_settings().agent_payments_enabled and _rails)


@dataclass(frozen=True)
class PaidOffer:
    endpoint_id: str
    quote_micro: int     # what the call will reserve, margin included
    offer: Offer         # what one payment must carry: max(quote, the payment floor)
    proof_only: bool = False   # a free poll or fetch: the payment proves the wallet, never settles
    is_async: bool = False     # the call starts a task that is polled later


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


class RateLimited(Exception):
    """Too many keyless requests from one client address, or too many payment checks across all
    of them; nothing was checked or charged."""

    def __init__(self, message: str = "too many payments from this address; wait a minute and "
                                      "send it again") -> None:
        super().__init__(message)
        self.detail = {"error": "rate_limited", "reason": "rate_limited", "message": message}


class PayerSuspended(Exception):
    """The wallet's payer team, or its member, is suspended; no payment was taken."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.detail = {"error": "payer_suspended", "reason": reason,
                       "message": "this wallet's treg team is suspended; no payment was taken"}


RATE_NS = "agent_payments"
RATE_WINDOW_S = 60
PROOF_NS = "agentpay-proof"
# A proof's claim outlives its challenge by this much, so a clock running behind cannot accept it.
PROOF_CLAIM_GRACE_S = 300


def presents_payment(headers: Mapping[str, str]) -> bool:
    return any(rail.presented(headers) for rail in _rails)


async def limit_request(headers: Mapping[str, str], client_ip: str) -> None:
    """Per client address, before any body is read: requests that carry a payment (each costs a
    check) and requests that carry none (each is priced) have separate limits."""
    settings = get_settings()
    if presents_payment(headers):
        key, cap = f"ip:{client_ip}", settings.agent_payments_max_payments_per_ip_minute
        message = "too many payments from this address; wait a minute and send it again"
    else:
        key, cap = f"quote-ip:{client_ip}", settings.agent_payments_max_quotes_per_ip_minute
        message = "too many requests from this address; wait a minute and send it again"
    async with session_maker() as db:
        await ratestore.sweep(db, RATE_NS)
        ok = await ratestore.rate_check(
            db, RATE_NS, ratestore.ip_limit(client_ip, cap, key=key), RATE_WINDOW_S, atomic=True)
        await db.commit()
    if not ok:
        raise RateLimited(message)


async def _limit_verification() -> None:
    """A payment or proof check about to make a chain or facilitator call: one cap across all
    clients, which many addresses cannot get around. The rail runs it only once local checks
    have passed, so a credential refused locally (or a SIWX proof, checked locally) costs none."""
    async with session_maker() as db:
        ok = await ratestore.rate_check(
            db, RATE_NS, [("verify:all", get_settings().agent_payments_max_verifications_minute)],
            RATE_WINDOW_S, atomic=True)
        await db.commit()
    if not ok:
        log.warning("agent payments: the payment check cap was reached")
        raise RateLimited("treg is checking too many payments right now; wait a minute and send "
                          "it again")


def _once(hook: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    """`hook`, run by the first call only: one request spends at most one check."""
    spent = False

    async def run() -> None:
        nonlocal spent
        if not spent:
            spent = True
            await hook()
    return run


class PaymentChallenge(Exception):
    """The 402 that asks for a payment. Carries every rail's challenge headers."""

    def __init__(self, detail: dict, headers: list[tuple[str, str]]) -> None:
        super().__init__(detail.get("message", "payment required"))
        self.detail = detail
        self.headers = headers


def consumes(name: str, value: str) -> bool:
    """True for a request header that carries a payment credential and must never be relayed,
    whichever rails are switched on."""
    lowered = name.lower()
    if lowered in PAYMENT_HEADERS:
        return lowered != "authorization" or value[:8].lower() == "payment "
    return False


async def quote(*, rest: str, method: str, path: str, query: str,
                query_items: tuple[tuple[str, str], ...], body: bytes, host: str = "") -> PaidOffer:
    """The price of this call for a payer. Raises NotServed when the payment path does not serve
    it (not a catalog id, free, routed, own-account, or not on treg's key here)."""
    if reason := unserved(rest, method):
        raise NotServed(reason)
    ep = _catalog_endpoint_for(rest)
    query_values = QueryValues(query_items)
    raw, reason = platform_quote(ep, query_values, body)
    proof_only = reason == "free" and async_utility(ep)
    if proof_only:
        raw, reason = platform_quote(ep, query_values, body, allow_free=True)
    if reason:
        raise NotServed(reason)
    charged = ledger.with_margin(raw)
    if charged <= 0 and not proof_only:
        raise NotServed("free")
    settings = get_settings()
    base = settings.public_url.rstrip("/")
    amount = max(with_fee(charged), settings.agent_payments_min_payment_micro)
    if settings.agent_payments_settlement == "stripe":
        amount = -(-amount // CENT_MICRO) * CENT_MICRO  # Stripe records whole cents
    description = f"{ep['id']} on treg"
    if charged > 0:
        description += f", at most ${micro_to_decimal(charged)}"
    resource_url = f"{base}{path}" + (f"?{query}" if query else "")
    offer = Offer(
        amount_micro=amount, method=method.upper(), path=path, query=query,
        resource_url=resource_url, description=description, host=_hostname(host),
        terms=(_terms_token(resource_url, Terms(max(charged, 0), settings.agent_payments_fee_bps))
               if settings.agent_payments_terms_binding else ""))
    return PaidOffer(endpoint_id=ep["id"], quote_micro=charged, offer=offer,
                     proof_only=proof_only, is_async=bool(ep.get("async")))


@dataclass(frozen=True)
class Terms:
    """What a payer was offered for one call: its quote (margin included) and treg's fee. `nonce`
    (the token's random part) and `expires` (Unix seconds) are set for terms read from a token."""
    quote_micro: int
    fee_bps: int
    nonce: str = ""
    expires: int = 0


_TERMS = re.compile(r"q(\d{1,15})f(\d{1,5})e(\d{1,12})r([0-9a-f]{16})m([0-9a-f]{32})")
# A SIWX challenge issued without a token carries pympp's own random nonce in its place.
_LEGACY_NONCE = re.compile(r"[0-9a-f]{32}")
TERMS_NS = "agentpay-terms"
# Past the longest a challenge lives (MPP's TTL, x402's timeout, SIWX's five minutes) by a grace.
TERMS_GRACE_S = 60


def _terms_ttl_s() -> int:
    settings = get_settings()
    return max(settings.agent_payments_mpp_challenge_ttl_s,
               settings.agent_payments_x402_max_timeout_s, 300) + TERMS_GRACE_S


def _terms_mac(resource_url: str, body: str) -> str:
    secret = get_settings().agent_payments_secret.encode()
    return hmac.new(secret, f"{resource_url}|{body}".encode(), hashlib.sha256).hexdigest()[:32]


def _terms_token(resource_url: str, terms: Terms) -> str:
    """An opaque, letters-and-digits token for `terms` on this request, authenticated with treg's
    secret. It rides where the payer echoes or signs it (MPP's challenge data, x402's `extra`, the
    SIWX nonce), so a payment pays on the terms it was offered, not today's."""
    expires = int(time.time()) + _terms_ttl_s()
    body = f"q{terms.quote_micro}f{terms.fee_bps}e{expires}r{secrets.token_hex(8)}"
    return f"{body}m{_terms_mac(resource_url, body)}"


def signed_terms(token: str, resource_url: str, today: Terms) -> Terms | None:
    """The terms a payment or proof was offered for this request: `today`'s when it carries no
    token (a challenge issued before tokens were, or with the switch off: no MPP or x402 terms,
    or a SIWX nonce of pympp's own; dropping one only gets what a fresh challenge offers), or
    None when it carries anything else treg did not issue for this request, or a token that
    expired."""
    if not token or _LEGACY_NONCE.fullmatch(token):
        return today
    found = _TERMS.fullmatch(token)
    if found is None:
        return None
    body = token[:token.rindex("m")]
    if not hmac.compare_digest(found.group(5), _terms_mac(resource_url, body)):
        return None
    if int(found.group(3)) < time.time():
        return None
    return Terms(int(found.group(1)), int(found.group(2)), nonce=found.group(4),
                 expires=int(found.group(3)))


def _hostname(host: str) -> str:
    """The Host header's name, lowercase, without its port."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def with_fee(amount_micro: int) -> int:
    """The payment whose credit, after treg's fee, still covers `amount_micro`."""
    bps = get_settings().agent_payments_fee_bps
    return -(-amount_micro * BPS // (BPS - bps))


def fee_of(amount_micro: int, fee_bps: int | None = None) -> int:
    bps = get_settings().agent_payments_fee_bps if fee_bps is None else fee_bps
    return amount_micro * bps // BPS


def _fee_note() -> str:
    bps = get_settings().agent_payments_fee_bps
    return f" Each settled payment carries treg's {bps / 100:g}% fee, which is not credited." if bps else ""


def wallet_view(ep: dict) -> dict | None:
    """What a keyless agent needs to know before it calls `ep` with a wallet, for catalog search
    and tool details; None while the feature is off."""
    if not enabled() or ep.get("kind") == "hub":
        return None
    if async_utility(ep) and not wallet_refusal(ep, allow_free=True):
        return {"payable": True, "free_for_task_owner": True,
                "note": "free for the wallet that started the task: it proves the wallet with a "
                        "signature, and no money moves"}
    if reason := wallet_refusal(ep):
        return {"payable": False, "reason": reason,
                "note": f"{_NOT_SERVED.get(reason, reason)}; sign in to call it"}
    settings = get_settings()
    minimum = settings.agent_payments_min_payment_micro
    return {"payable": True, "protocols": [rail.protocol for rail in _rails],
            "minimum_payment_micro": minimum, "fee_bps": settings.agent_payments_fee_bps,
            "note": f"pay per call with a wallet, no account. A payment is at least "
                    f"${ledger.usd(minimum):g}; the call is charged only its real price, and the "
                    "rest stays as this wallet's credit for its next calls (credit is not paid "
                    "back as money)." + _fee_note()}


_HOW_TO_PAY = {"mpp": "MPP (WWW-Authenticate: Payment)", "x402": "x402 (PAYMENT-REQUIRED)"}


async def _headers(rails, make) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for rail in rails:
        try:
            headers += await make(rail)
        except Exception:
            log.exception("agent payments: %s challenge failed", rail.protocol)
    return headers


async def challenge(paid: PaidOffer, *, reason: str = "", message: str = "",
                    offer_proof: bool = True) -> PaymentChallenge:
    """The 402. A returning wallet's zero-amount proof challenge comes first, because clients
    answer the first challenge they can pay: a wallet whose credit covers the call then pays
    nothing, and one whose credit does not is asked again with `credit_insufficient`, without it.
    A free poll offers only what proves the wallet without settling."""
    payers = [rail for rail in _rails if not paid.proof_only or rail.deferrable_payments]
    provers = [rail for rail in _rails if offer_proof and rail.proves(paid.offer)]
    headers = await _headers(_rails, lambda rail: rail.challenge(
        paid.offer, pay=rail in payers, prove=rail in provers))
    ways = " or ".join(_HOW_TO_PAY.get(rail.protocol, rail.protocol) for rail in payers)
    proof_ways = _proof_ways(provers)
    settings = get_settings()
    minimum = settings.agent_payments_min_payment_micro
    amount, hold = paid.offer.amount_micro, paid.quote_micro
    if paid.proof_only:
        message_text = ("This call is free for the wallet that started the task. Prove that wallet "
                        "and send the same request again: ")
        message_text += f"sign {proof_ways}" if proof_ways else ""
        if ways:
            message_text += (", or " if proof_ways else "") + (
                f"sign a payment with {ways} that treg checks and never settles")
        message_text += ". No money moves."
    else:
        pay = (f"${ledger.usd(amount):g} (the smallest payment)" if amount > with_fee(hold)
               else f"${ledger.usd(amount):g}")
        message_text = (
            f"This call costs at most ${ledger.usd(hold):g}. Pay {pay} with {ways} and send the "
            "same request again. treg charges only the real price after the call (a failed call "
            "or one with no billable result costs nothing); the rest stays as this wallet's credit "
            "for its next calls. Credit is not paid back as money." + _fee_note())
        if proof_ways:
            message_text += (f" A wallet that already has credit signs {proof_ways} instead and "
                             "pays nothing now.")
        message_text += " Or sign in and use a treg key."
        if paid.is_async:
            message_text += (" This tool starts a task: the hold stays open until the task ends. "
                             "Poll it with the same wallet; polling is free.")
    detail = {
        "error": "payment_required",
        "reason": reason or ("proof_required" if paid.proof_only else "payment_required"),
        "endpoint_id": paid.endpoint_id,
        "amount_micro": amount,
        "estimated_cost_micro": hold,
        "minimum_payment_micro": minimum,
        "fee_bps": settings.agent_payments_fee_bps,
        "protocols": [rail.protocol for rail in _rails],
        "message": message_text,
    }
    if reason:
        detail["message"] = f"{message or reason}. " + detail["message"]
    return PaymentChallenge(detail, headers)


_PROOF_WAY = {"mpp": "the zero-amount MPP challenge",
              "x402": "the x402 sign-in-with-x (SIWX) challenge"}


def _proof_ways(rails) -> str:
    return " or ".join(_PROOF_WAY.get(rail.protocol, rail.protocol) for rail in rails)


async def credit_short(paid: PaidOffer, org_id: int | None) -> PaymentChallenge:
    """The 402 for a proven wallet whose credit does not cover the call: payment challenges only."""
    credit = 0
    if org_id is not None:
        async with session_maker() as db:
            credit = max(await ledger.balance_of(db, org_id), 0)
    return await challenge(paid, reason="credit_insufficient", offer_proof=False,
                           message=f"this wallet's credit (${ledger.usd(credit):g}) does not cover "
                                   "this call")


async def admit(paid: PaidOffer, headers: Mapping[str, str]) -> PayerAdmission:
    """Verify the presented payment or proof, settle a payment unless credit covers the call, and
    return the payer team to call as. Raises PaymentChallenge when it cannot be used."""
    presented = [(rail, cred) for rail in _rails if (cred := rail.presented(headers))]
    if not presented:
        raise await challenge(paid)
    if len(presented) > 1:
        raise await challenge(paid, reason="ambiguous_payment",
                              message="send one payment credential, not several")
    rail, credential = presented[0]
    check = _once(_limit_verification)
    try:
        verified = await rail.verify(credential, paid.offer, before_remote=check)
    except PaymentRejected as exc:
        raise await challenge(paid, reason=exc.code, message=exc.message) from exc
    # The terms the payer was offered, which treg's own token names: a price that rose since then
    # is not what the payer agreed to.
    signed = signed_terms(verified.terms, paid.offer.resource_url,
                          Terms(paid.quote_micro, get_settings().agent_payments_fee_bps))
    agreed = signed is not None and signed.quote_micro >= paid.quote_micro
    if verified.kind == "proof":
        if not agreed:
            raise await _price_changed(paid)
        return await _admit_proof(paid, verified)
    # A verified payment is settled or looked up on chain next, even one its rail checked
    # locally (an MPP pull transfer is first broadcast at settlement).
    await check()
    max_life = get_settings().agent_payments_credit_proof_max_s
    if verified.deferrable and verified.expires_at > _now() + timedelta(seconds=max_life):
        verified = replace(verified, deferrable=False)
    agreed = agreed and verified.amount_micro >= paid.offer.amount_micro
    if not agreed and not verified.moved:
        raise await _price_changed(paid)   # nothing has moved: the payer signs the new terms

    if paid.proof_only:
        org_id = await _known_payer_org(verified.network, verified.payer)
        if org_id is None:
            raise await challenge(paid, reason="unknown_wallet",
                                  message="this wallet has started no task on treg")
        if not verified.deferrable:
            raise await challenge(paid, reason="signature_needed",
                                  message="prove the wallet with the zero-amount MPP challenge or "
                                          "x402 sign-in-with-x, or an x402 payment that treg can "
                                          f"check without settling it, expiring within {max_life} "
                                          "seconds")
    else:
        org_id = await _payer_org(verified.network, verified.payer)
    if reason := await _suspension(org_id):
        raise PayerSuspended(reason)
    used = False
    if agreed and signed.nonce and not await _claim_terms(signed, verified):
        # A challenge's terms buy one call: the first payment made under them, before they expire
        # (on x402 they ride outside what the wallet signs). A second transfer already sent is
        # kept as credit below, and runs no call.
        if not verified.moved:
            raise await challenge(paid, reason="challenge_used",
                                  message="another payment already used this challenge's terms")
        agreed, used = False, True
    # A transfer the payer already sent is kept on the terms it was sent under, whatever the call
    # costs now; it never draws credit, so it settles below.
    payment_id, draw = await _claim(verified, org_id, paid.quote_micro,
                                    fee_bps=signed.fee_bps if signed is not None else None)
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
    if not agreed:
        raise await _kept(paid, rail, verified, settlement, org_id, used=used)
    return PayerAdmission(caller=await _caller(org_id), verified=verified, settlement=settlement)


class PriceChanged(Exception):
    """A transfer the payer already sent was signed on terms that no longer buy the call. It is
    kept as credit; the answer is not a 402, because the paying rail offers this host no challenge
    that spends credit without paying again."""

    def __init__(self, detail: dict, headers: list[tuple[str, str]]) -> None:
        super().__init__(detail["message"])
        self.detail = detail
        self.headers = headers


async def _kept(paid: PaidOffer, rail: PaymentRail, verified: Verified,
                settlement: Settlement | None, org_id: int, *,
                used: bool) -> PaymentChallenge | PriceChanged:
    """The answer for a transfer already sent on terms that no longer buy the call: it is the
    wallet's credit now, and the answer says how much it paid, its transaction and the credit, so
    the wallet can match the top-up. Where the paying rail offers this host its zero-amount proof
    (an x402 proof cannot spend Tempo credit), the 402 asks for that proof; elsewhere a 402 could
    only be answered by paying again, so it is a 409."""
    async with session_maker() as db:
        credit = max(await ledger.balance_of(db, org_id), 0)
    reference = settlement.reference if settlement is not None else ""
    headers = [("X-Treg-Payment", "paid"), ("X-Treg-Paid-Micro", str(verified.amount_micro)),
               ("X-Treg-Credit-Micro", str(credit))]
    if reference:
        headers.append(("X-Treg-Payment-Reference", reference))
    reason = "challenge_used" if used else "price_changed"
    why = ("another payment already used this challenge's terms" if used else
           "the price of this call changed after this transfer was signed")
    kept = {"paid_micro": verified.amount_micro, "payment_reference": reference,
            "credit_micro": credit}
    if rail.proves(paid.offer):
        asked = await challenge(paid, reason=reason, message=(
            f"{why}; the transfer is kept as this wallet's credit, which pays this call with the "
            "zero-amount proof"))
        asked.detail.update(kept)
        asked.headers += headers
        return asked
    realm = get_settings().public_url.rstrip("/")
    return PriceChanged({
        "error": reason, "reason": reason, "endpoint_id": paid.endpoint_id,
        "estimated_cost_micro": paid.quote_micro, **kept,
        "message": (f"{why}. The transfer is kept as this wallet's credit "
                    f"(${ledger.usd(credit):g}); do not pay again. Send the request to {realm} and "
                    f"sign its zero-amount proof to pay this call (at most "
                    f"${ledger.usd(paid.quote_micro):g}) from that credit."),
    }, headers)


async def _claim_terms(terms: Terms, verified: Verified) -> bool:
    """One authorization per terms token, claimed until the token expires. The same authorization
    presented again passes here, so its replay is refused as one (`_claim`)."""
    async with session_maker() as db:
        await ratestore.sweep(db, TERMS_NS)
        key = f"{verified.protocol}:{terms.nonce}"   # one 402 offers every rail the same terms
        claimed = await ratestore.kv_claim(db, TERMS_NS, key,
                                           max(terms.expires - time.time(), 0) + TERMS_GRACE_S,
                                           v={"proof": verified.proof_id})
        held = None if claimed else await ratestore.kv_get(db, TERMS_NS, key)
        await db.commit()
    return claimed or (held or {}).get("proof") == verified.proof_id


async def _price_changed(paid: PaidOffer) -> PaymentChallenge:
    """The 402 for a payment or proof signed on terms that no longer buy this call: the price rose
    (or the payment no longer covers it) since its challenge. A fresh challenge names the new
    terms; nothing was charged."""
    return await challenge(paid, reason="price_changed",
                           message="the price of this call changed after this was signed")


async def _admit_proof(paid: PaidOffer, verified: Verified) -> PayerAdmission:
    """A zero-amount proof names the wallet and moves no money: the call runs on the credit that
    wallet already holds on this network, or it is asked to pay."""
    org_id = await _known_payer_org(verified.network, verified.payer)
    if paid.proof_only:
        if org_id is None:
            raise await challenge(paid, reason="unknown_wallet",
                                  message="this wallet has started no task on treg")
    else:
        credit = 0
        if org_id is not None:
            async with session_maker() as db:
                credit = await ledger.balance_of(db, org_id)
        if org_id is None or credit < paid.quote_micro:
            raise await credit_short(paid, org_id)
    if reason := await _suspension(org_id):
        raise PayerSuspended(reason)
    if not await _claim_proof(verified):
        raise await challenge(paid, reason="payment_replayed", message="this proof was already used")
    return PayerAdmission(caller=await _caller(org_id), verified=verified, settlement=None)


REPLAY_NS = "agentpay-mpp"
REPLAY_KEEP_S = 7 * 24 * 3600


class ReplayStore:
    """pympp's replay store (`mpp.store.ExpiringStore`) on treg's `Ephemeral` table, shared by
    every instance. pympp claims transaction hashes in it before it broadcasts or accepts them."""

    async def get(self, key: str):
        async with session_maker() as db:
            value = await ratestore.kv_get(db, REPLAY_NS, key)
            await db.commit()
        return None if value is None else value.get("v")

    async def put(self, key: str, value) -> None:
        async with session_maker() as db:
            await ratestore.kv_put(db, REPLAY_NS, key, {"v": value}, ttl_s=REPLAY_KEEP_S)
            await db.commit()

    async def delete(self, key: str) -> None:
        async with session_maker() as db:
            await ratestore.kv_pop(db, REPLAY_NS, key)
            await db.commit()

    async def put_if_absent(self, key: str, value) -> bool:
        return await self._claim(key, value, REPLAY_KEEP_S)

    async def put_if_absent_until(self, key: str, value, expires_at: float) -> bool:
        ttl = expires_at - time.time()
        if not ttl > 0:
            raise ValueError("expires_at must be a finite future Unix timestamp")
        return await self._claim(key, value, ttl)

    async def _claim(self, key: str, value, ttl_s: float) -> bool:
        async with session_maker() as db:
            await ratestore.sweep(db, REPLAY_NS)
            claimed = await ratestore.kv_claim(db, REPLAY_NS, key, ttl_s, v={"v": value})
            await db.commit()
        return claimed


async def _claim_proof(verified: Verified) -> bool:
    """A proof is single-use: its challenge id is claimed once, for as long as it could be valid."""
    ttl = max((verified.expires_at - _now()).total_seconds(), 0) + PROOF_CLAIM_GRACE_S
    async with session_maker() as db:
        await ratestore.sweep(db, PROOF_NS)
        claimed = await ratestore.kv_claim(db, PROOF_NS, verified.proof_id, ttl)
        await db.commit()
    return claimed


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
        if verified.moved and exc.code != "settlement_unknown":
            # A pushed transfer is on chain whatever settlement said (its challenge may have
            # expired since verification): the reconciler reads the chain and credits it.
            exc = PaymentRejected("settlement_unknown", exc.message)
        status = "settling" if exc.code == "settlement_unknown" else "failed"
        await _mark(payment_id, status=status, error=exc.code)
        raise exc from None
    await _credit(payment_id, settlement.reference)
    return settlement


async def _suspension(org_id: int) -> str | None:
    """What `require_member` refuses on the account path, checked before a payment is taken."""
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        if org is None or org.suspended:
            return "org_suspended"
        users = (await db.execute(
            select(User).join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org_id))).scalars().all()
        return "account_suspended" if not users or any(u.suspended for u in users) else None


async def _known_payer_org(network: str, address: str) -> int | None:
    async with session_maker() as db:
        payer = await db.get(AgentPayer, (network, address))
        return payer.org_id if payer is not None else None


async def _payer_org(network: str, address: str) -> int:
    for _ in range(2):
        async with session_maker() as db:
            payer = await db.get(AgentPayer, (network, address))
            if payer is not None:
                return payer.org_id
            try:
                org = await make_payer_team(db, network, address)
                await db.commit()
                return int(org.id)
            except IntegrityError:
                await db.rollback()  # a concurrent first payment from this address created it
    raise RuntimeError("payer team could not be created")


async def _claim(verified: Verified, org_id: int, quote_micro: int, *,
                 fee_bps: int | None = None) -> tuple[int | None, bool | None]:
    """Record the proof as used, deciding whether it draws credit or must settle. A proof seen
    before returns `(None, None)`: the unique `proof_id` is what refuses a replay."""
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        draw = verified.deferrable and balance >= quote_micro
        row = AgentPayment(
            proof_id=verified.proof_id, protocol=verified.protocol, network=verified.network,
            payer_address=verified.payer, org_id=org_id, amount_micro=verified.amount_micro,
            fee_micro=0 if draw else fee_of(verified.amount_micro, fee_bps),
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
            db, row.org_id, row.amount_micro - row.fee_micro, f"agentpay:{row.proof_id}",
            meta={"source": "agent_payment", "protocol": row.protocol, "fee_micro": row.fee_micro,
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
    "usage_priced": "this tool's price is known only after the call, which needs a treg account",
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
    paid: PaidOffer

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
    NotServed, RateLimited, PaymentChallenge, PaymentPending or PriceChanged; no call has run when it raises.
    `limit_request` has already run for this request."""
    paid = await quote(rest=rest, method=method, path=path, query=query,
                       query_items=query_items, body=body, host=headers.get("host", ""))
    admission = await admit(paid, headers)
    relayed = [(name, value) for name, value in raw_headers
               if not consumes(name.decode("latin-1"), value.decode("latin-1"))]
    return PaidCall(caller=admission.caller, headers=relayed, admission=admission, paid=paid)


CREDIT_PATH = "/agentpay/credit"
RECENT_PAYMENTS = 20


class NotEnabled(Exception):
    """The feature is off: the credit route does not exist."""


async def credit_of(headers: Mapping[str, str]) -> dict:
    """What a wallet holds on one network, after it proves the wallet with a proof (MPP's
    zero-amount charge or x402's SIWX): its credit and its last payments. Read only. Raises
    NotEnabled, RateLimited or PaymentChallenge."""
    if not enabled():
        raise NotEnabled
    settings = get_settings()
    base = settings.public_url.rstrip("/")
    # x402 lists a payment beside its SIWX challenge (its format requires one); this route
    # refuses it.
    offer = Offer(amount_micro=max(settings.agent_payments_min_payment_micro, 1), method="GET",
                  path=CREDIT_PATH, query="", resource_url=f"{base}{CREDIT_PATH}",
                  description="this wallet's credit", host=_hostname(headers.get("host", "")))
    provers = [rail for rail in _rails if rail.proves(offer)]
    asked = PaymentChallenge(
        {"error": "payment_required", "reason": "proof_required",
         "message": (f"Prove the wallet: sign {_proof_ways(provers) or 'a wallet proof'} and send "
                     "the same request again. No money moves.")},
        await _headers(provers, lambda rail: rail.challenge(offer, pay=False, prove=True)))
    presented = [(rail, cred) for rail in _rails if (cred := rail.presented(headers))]
    if not presented:
        raise asked
    if len(presented) > 1:
        asked.detail.update(reason="ambiguous_payment",
                            message="send one proof, not several. " + asked.detail["message"])
        raise asked
    rail, credential = presented[0]
    try:
        verified = await rail.verify(credential, offer, before_remote=_limit_verification)
    except PaymentRejected as exc:
        asked.detail.update(reason=exc.code, message=f"{exc.message}. {asked.detail['message']}")
        raise asked from exc
    if verified.kind != "proof":
        asked.detail.update(reason="proof_required",
                            message="this route takes a wallet proof, not a payment. "
                                    + asked.detail["message"])
        raise asked
    if not await _claim_proof(verified):
        asked.detail.update(reason="payment_replayed",
                            message="this proof was already used. " + asked.detail["message"])
        raise asked
    org_id = await _known_payer_org(verified.network, verified.payer)
    credit, recent = 0, []
    if org_id is not None:
        async with session_maker() as db:
            credit = max(await ledger.balance_of(db, org_id), 0)
            rows = (await db.execute(
                select(AgentPayment).where(AgentPayment.org_id == org_id)
                .order_by(AgentPayment.created_at.desc(), AgentPayment.id.desc())
                .limit(RECENT_PAYMENTS))).scalars().all()
            recent = [{"at": row.created_at.isoformat() + "Z", "protocol": row.protocol,
                       "amount_micro": row.amount_micro, "status": row.status,
                       "reference": row.reference} for row in rows]
    return {"network": verified.network, "address": verified.payer, "credit_micro": credit,
            "recent": recent}


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
    stuck: int = 0      # still `settling` past `agent_payments_stuck_after_s`: alert on this
    late_landed: int = 0  # a payment that drew credit was settled anyway; credited now
    closed: int = 0       # a payment that drew credit expired unsettled
    reasons: dict[str, int] = field(default_factory=dict)


async def reconcile(*, limit: int = 50) -> ReconcileResult:
    """Resolve `settling` payments the request path could not confirm: credit the ones whose
    transfer landed, fail the ones that can no longer land. Check each expired `credit` payment
    once: credit it if someone settled it anyway, else close it. Then, when payments settle to
    Stripe, record each settled one there. Chain and Stripe I/O run with no session open."""
    settings = get_settings()
    result = ReconcileResult()
    cutoff = _now() - timedelta(seconds=settings.agent_payments_reconcile_min_age_s)
    async with session_maker() as db:
        rows = (await db.execute(
            select(AgentPayment).where(AgentPayment.status == "settling",
                                       AgentPayment.created_at < cutoff, _on_configured_rails())
            .order_by(AgentPayment.created_at).limit(limit))).scalars().all()
        stuck = [(row.id, _rail(row.protocol, row.network), _unconfirmed(row)) for row in rows]
    for payment_id, rail, payment in stuck:
        assert rail is not None
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
    await _close_credit_proofs(result, limit=limit)
    if settings.agent_payments_settlement == "stripe":
        await _record_to_stripe(result, limit=limit, api_key=settings.agent_payments_stripe_key)
    stuck_before = _now() - timedelta(seconds=settings.agent_payments_stuck_after_s)
    async with session_maker() as db:
        result.stuck = int((await db.execute(
            select(func.count()).select_from(AgentPayment).where(
                AgentPayment.status == "settling",
                AgentPayment.created_at < stuck_before))).scalar_one())
    if result.stuck:
        log.warning("agent payments: %d payments still unconfirmed", result.stuck)
    return result


def _unconfirmed(row: AgentPayment) -> Unconfirmed:
    return Unconfirmed(proof_id=row.proof_id, payer=row.payer_address,
                       amount_micro=row.amount_micro, asset=row.asset, pay_to=row.pay_to,
                       created_at=row.created_at, expires_at=row.expires_at)


def _rail(protocol: str, network: str) -> PaymentRail | None:
    return next((r for r in _rails if r.protocol == protocol and r.network == network), None)


def _on_configured_rails():
    """Only rows a configured rail can look up: rows of a switched-off rail wait in the table
    and never fill a batch."""
    return or_(false(), *(and_(AgentPayment.protocol == r.protocol,
                               AgentPayment.network == r.network) for r in _rails))


def _count(result: ReconcileResult, reason: str) -> None:
    result.reasons[reason] = result.reasons.get(reason, 0) + 1


async def _close_credit_proofs(result: ReconcileResult, *, limit: int) -> None:
    """A payment that drew credit is never settled by treg, but anyone holding it could submit it
    until it expires. Past expiry and the rail's grace the chain's answer is final, so each one is
    looked up once: a landed transfer becomes credit, the rest close."""
    after = _now() - CREDIT_PROOF_GRACE
    async with session_maker() as db:
        rows = (await db.execute(
            select(AgentPayment).where(AgentPayment.status == "credit",
                                       AgentPayment.expires_at < after, _on_configured_rails())
            .order_by(AgentPayment.expires_at).limit(limit))).scalars().all()
        due = [(row.id, _rail(row.protocol, row.network), _unconfirmed(row)) for row in rows]
    for payment_id, rail, payment in due:
        assert rail is not None
        lookup = await rail.find(payment)
        if lookup.state == "landed":
            _count(result, "credit_proof_landed")
            if await _claim_closed(payment_id, "settling"):
                await _credit(payment_id, lookup.reference)
                result.late_landed += 1
        elif lookup.state == "absent":
            _count(result, lookup.reason or "absent")
            if await _claim_closed(payment_id, "expired", lookup.reason, lookup.reference):
                result.closed += 1
        else:
            _count(result, lookup.reason or "pending")


async def _claim_closed(payment_id: int, status: str, reason: str | None = None,
                        reference: str | None = None) -> bool:
    """Move a `credit` row on, once: a second reconciler that read the same row finds it moved."""
    async with session_maker() as db:
        moved_rows = (await db.execute(
            update(AgentPayment)
            .where(AgentPayment.id == payment_id, AgentPayment.status == "credit")
            .values(status=status, error=reason, reference=reference, updated_at=_now()))).rowcount
        await db.commit()
        return moved_rows == 1


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
    if not destinations:
        return
    current = or_(*(and_(AgentPayment.protocol == protocol, AgentPayment.network == network,
                         AgentPayment.pay_to == pay_to)
                    for (protocol, network), (pay_to, _) in destinations.items()))
    async with session_maker() as db:
        rows = (await db.execute(
            select(AgentPayment).where(AgentPayment.status == "settled",
                                       AgentPayment.stripe_payment_intent.is_(None),
                                       AgentPayment.reference.is_not(None), current)
            .order_by(AgentPayment.created_at).limit(limit))).scalars().all()
        due = [(row.id, row.amount_micro, row.reference, destinations[(row.protocol, row.network)],
                _stripe_metadata(row)) for row in rows]
    for payment_id, amount_micro, reference, (_, network), metadata in due:
        try:
            intent = await stripe_api.record_crypto_payment(
                amount_cents=amount_micro // CENT_MICRO, network=network,
                transaction_hash=reference, api_key=api_key, metadata=metadata)
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


def _stripe_metadata(row: AgentPayment) -> dict[str, str]:
    """What support searches Stripe by: the paying wallet, its team and treg's payment row."""
    return {"treg_agent_payment_id": str(row.id), "treg_team_id": str(row.org_id),
            "payer_address": row.payer_address, "protocol": row.protocol, "network": row.network}


async def create_stripe_deposit_address(network: str) -> dict:
    """Ops setup for settlement `stripe`: one reusable deposit address per network, set as the
    rail's recipient. Never called on the request path."""
    return await stripe_api.create_crypto_deposit_address(
        network, api_key=get_settings().agent_payments_stripe_key)
