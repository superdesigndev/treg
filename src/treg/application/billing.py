"""The ONE module that talks to Stripe — the mirror of `domain/money`, which is the one module that
moves money. Neither reaches into the other's job: Stripe authorizes a payment here, and the ledger
credits balance there, and the only thing that crosses between them is
`ledger.topup(org, amount_micro, payment_ref)`.

Two unit systems meet in this file and NOWHERE else:
  * our ledger speaks **integer micro-USD** (1e-6 USD) — a catalog call costs ~600 micro,
  * Stripe speaks **integer cents**,
so 1 cent == 10_000 micro and every crossing goes through `micro_to_cents` / `cents_to_micro`,
which refuse to lose a fraction rather than silently rounding someone's money away. Whole dollars
appear only in the settings and in what a human types.

**Credit happens on the WEBHOOK, not on the browser's return from Checkout.** The success redirect is
a URL the payer controls — treating it as proof of payment means anyone can type it and mint balance.
The one exception is the off-session auto-top-up charge, where the *server itself* holds the
PaymentIntent's confirmed status in its hand (nothing attacker-supplied is involved), so it credits
immediately and the webhook redelivery lands as a no-op. That safety comes from `ledger.topup` being
idempotent on the PaymentIntent id, which is also what makes Stripe's at-least-once webhook delivery
harmless: the same event twice credits once.

Async: the Stripe SDK is synchronous and this is an async FastAPI app, so every SDK call goes through
`_sdk()` onto a worker thread. A blocking network call on the event loop would stall every other
in-flight request, including the proxy's hot path.

Auto-top-up is the part that can go wrong expensively, so it is guarded in depth (see
`attempt_auto_topup`): recorded consent (the PSD2/SCA mandate — a compliance requirement, not a
checkbox), a monthly cap, a cooldown stamped in the DB *before* the charge so a second web worker
sees it, a consecutive-failure limit, and an idempotency key derived from the threshold crossing so a
burst of concurrent calls that all notice the low balance produces exactly ONE charge.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
from uuid import uuid4
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, delete, func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import select

from .. import adsconv
from .. import analytics
from ..infra import db as _db
from .. import email as email_mod
from ..domain import money as ledger
from ..domain import referrals
from ..config import get_settings
from ..infra import stripe as stripe_adapter
from ..models import CreditBlock, EnterpriseSSO, EnterpriseSSOIdentity, EnterpriseDirectoryUser, LedgerEntry, Membership, Org, User
from ..infra.db import session_maker
from . import enterprise_sso



log = logging.getLogger("treg.billing")

MICRO_PER_CENT = 10_000
MICRO_PER_USD = 1_000_000
# A ceiling on a single top-up. Not a business rule — a fat-finger guard: the difference between "$50"
# and "$5000" is one keystroke, and a card that big is likelier a mistake or a stolen number than a
# customer. Raise it deliberately when someone asks.
TOPUP_MAX_USD = 2_000
# Tags Checkout sessions in the Stripe dashboard so this flow's conversion can be compared against
# any other we add later (required suffix is 8 letters — see Stripe's integration_identifier docs).
INTEGRATION_ID = "tregtopup-qwkmzhrb"
AUTH_REQUIRED_CODES = {"authentication_required", "invoice_payment_intent_requires_action"}


class BillingNotConfigured(Exception):
    """No `TREG_STRIPE_SECRET_KEY`. The endpoints turn this into a 503 — top-ups are simply off on
    this deployment, which is a different thing from the caller doing something wrong."""


class TopupRejected(Exception):
    """The requested amount isn't one we'll charge (below the minimum, not whole dollars, absurd).
    Carries a message written for the person who typed it; the endpoints return it as a 422."""


class BillingJourneyError(Exception):
    """A billing use-case failure translated by the HTTP router."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class PriorAutoInvoiceUnresolved(Exception):
    """A prior invoice with different charge parameters must be resolved before a new charge."""

    def __init__(self, invoice_id: str) -> None:
        super().__init__("unfinished auto top-up invoice with an earlier attempt key")
        self.invoice_id = invoice_id


def usd_to_micro(amount_usd: int | float) -> int:
    """Whole-dollar setting → micro-USD. Accepts a float only so a config like `5.0` works; the
    result is always an integer, and fractional cents are refused rather than rounded."""
    micro = round(float(amount_usd) * MICRO_PER_USD)
    if micro % MICRO_PER_CENT:
        raise TopupRejected(f"${amount_usd} is not a whole number of cents")
    return int(micro)


def micro_to_cents(amount_micro: int) -> int:
    """micro-USD → the integer cents Stripe charges. Refuses a sub-cent remainder: Stripe cannot
    charge it, so silently dropping it would make the ledger and the card disagree."""
    if int(amount_micro) % MICRO_PER_CENT:
        raise TopupRejected(f"{amount_micro} micro-USD is not a whole number of cents")
    return int(amount_micro) // MICRO_PER_CENT


def cents_to_micro(amount_cents: int) -> int:
    """The integer cents Stripe actually charged → micro-USD for the ledger. Always exact."""
    return int(amount_cents) * MICRO_PER_CENT


def bonus_for_topup(amount_micro: int) -> tuple[int, int]:
    """The promotional bonus a MANUAL top-up of `amount_micro` earns, as `(bonus_micro, percent)`.

    Tiered by size (`topup_bonus_tiers`): the highest tier at or below the amount applies, so $99 gets
    the $50 rate and $250 the $200 rate. Integer micro-USD throughout — `amount * pct // 100`
    truncates the sub-micro remainder, which is at most a millionth of a dollar in treg's favour.
    Callers decide whether the top-up qualifies at all (automatic refills never do).
    """
    tiers = get_settings().topup_bonus_tiers or {}
    usd = int(amount_micro) // MICRO_PER_USD
    pct = 0
    for floor_usd in sorted(int(k) for k in tiers):
        if usd >= floor_usd:
            pct = int(tiers[floor_usd] if floor_usd in tiers else tiers[str(floor_usd)])
    if pct <= 0:
        return 0, 0
    return int(amount_micro) * pct // 100, pct


async def next_default_usd(db: AsyncSession, org_id: int | None) -> int:
    """The amount to preselect for this org's next MANUAL top-up: one preset above the last manual
    one, capped at `topup_default_cap_usd`. No history → `topup_default_usd`.

    Automatic refills are ignored on purpose: they repeat the org's own chosen amount, and stepping
    the default up because of them would ratchet forever. "Above" means the first preset strictly
    greater than the last amount; an amount at or past the cap stays at the cap.
    """
    s = get_settings()
    if not org_id:
        return int(s.topup_default_usd)
    rows = (await db.execute(
        select(LedgerEntry.amount_micro, LedgerEntry.meta)
        .where(LedgerEntry.org_id == org_id, LedgerEntry.kind == "topup")
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(50)
    )).all()
    last_micro = None
    for amount, meta in rows:
        if not (meta or {}).get("auto"):
            last_micro = int(amount)
            break
    if last_micro is None:
        return int(s.topup_default_usd)
    last_usd = last_micro // MICRO_PER_USD
    cap = int(s.topup_default_cap_usd)
    for p in sorted(int(x) for x in s.topup_presets):
        if p > last_usd:
            return min(p, cap)
    return min(max(last_usd, int(s.topup_default_usd)), cap)


def validate_threshold_usd(amount_usd) -> int:
    """The auto-top-up THRESHOLD is not a charge, so it is not held to the top-up minimum (which is
    card-fee math): any whole dollar amount from $1 up to the single-top-up ceiling."""
    try:
        value = float(amount_usd)
    except (TypeError, ValueError) as exc:
        raise TopupRejected("threshold must be a number of US dollars") from exc
    if value != int(value):
        raise TopupRejected("the threshold is whole dollars (e.g. 5, not 5.50)")
    if value < 1:
        raise TopupRejected("the threshold must be at least $1")
    if value > TOPUP_MAX_USD:
        raise TopupRejected(f"the threshold cannot exceed ${TOPUP_MAX_USD}")
    return int(value)


def validate_topup_usd(amount_usd) -> int:
    """The one gate on "how much". Returns whole dollars; raises `TopupRejected` with a message the
    payer can act on.

    Whole dollars only, at or above `topup_min_usd`. The presets are SUGGESTIONS, not an allow-list —
    someone who wants $37 should get $37; the minimum is the part that has to hold, because at
    2.9% + $0.30 a $1 top-up loses a third of itself to fees.
    """
    s = get_settings()
    try:
        value = float(amount_usd)
    except (TypeError, ValueError) as exc:
        raise TopupRejected("amount must be a number of US dollars") from exc
    if value != int(value):
        raise TopupRejected("top-ups are whole dollars (e.g. 10, not 10.50)")
    value = int(value)
    if value < s.topup_min_usd:
        raise TopupRejected(f"the minimum top-up is ${s.topup_min_usd} (card fees eat a smaller one)")
    if value > TOPUP_MAX_USD:
        raise TopupRejected(f"the maximum single top-up is ${TOPUP_MAX_USD}")
    return value


def _now() -> datetime:
    # Naive UTC — the models._now convention (TIMESTAMP WITHOUT TIME ZONE; asyncpg rejects tz-aware).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def configured() -> bool:
    return bool(get_settings().stripe_secret_key)


def _require_configured() -> str:
    key = get_settings().stripe_secret_key
    if not key:
        raise BillingNotConfigured("TREG_STRIPE_SECRET_KEY is not set — top-ups are disabled")
    return key


async def _sdk(fn, /, **kwargs):
    """Run one Stripe SDK call on a worker thread with our key. The SDK is sync; the app is not.

    The key is passed per-call (`api_key=`) rather than set on the module, so nothing depends on
    global mutable state and a test can monkeypatch this single function to intercept every call.

    Returns a plain dict, never a `StripeObject`. The SDK's objects stopped subclassing dict, so
    `.get()` on one raises ("'get' is a dict method, but a PaymentIntent is not a dict") — which is
    exactly how every `checkout.session.completed` webhook 500'd in production while the suite,
    whose fakes return plain dicts through this same funnel, stayed green. Every consumer of this
    function reads dict-style; converting once here keeps them and the test fakes on one shape.
    `to_dict()` is deep: nested objects and list `data` items all come back as plain dicts.
    """
    key = _require_configured()
    return await stripe_adapter.call(fn, api_key=key, **kwargs)


def autotopup_prefs(org: Org) -> dict:
    """The org's effective auto-top-up numbers in micro-USD. A stored 0 means "we never chose", so it
    resolves to the deployment default — which is what lets the default move for everyone who never
    set their own, without a data migration."""
    s = get_settings()
    return {
        "threshold_micro": int(org.autotopup_threshold_micro or usd_to_micro(s.autotopup_default_threshold_usd)),
        "amount_micro": int(org.autotopup_amount_micro or usd_to_micro(s.autotopup_default_amount_usd)),
        "monthly_cap_micro": int(org.autotopup_monthly_cap_micro or usd_to_micro(TOPUP_MAX_USD)),
        "per_hour": int(org.autotopup_max_per_hour or s.autotopup_default_per_hour),
    }


def autotopup_cap_reached(prefs: dict, month_spend_micro: int) -> bool:
    """Whether the next automatic charge would pass the monthly cap, so none fires."""
    return month_spend_micro + prefs["amount_micro"] > prefs["monthly_cap_micro"]


def autotopup_wait_s(org: Org) -> int:
    """Seconds between two automatic charges: an hour after a failed one (a declined card is not
    retried every few minutes), else an hour divided by the team's charges-per-hour."""
    s = get_settings()
    if org.autotopup_failures:
        return s.autotopup_cooldown_s
    return 3600 // max(1, autotopup_prefs(org)["per_hour"])


def validate_per_hour(value) -> int:
    n = int(value)
    if not 1 <= n <= 60:
        raise ValueError("charges per hour must be between 1 and 60")
    return n


def _month_start() -> datetime:
    n = _now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def monthly_autotopup_spend(db: AsyncSession, org_id: int) -> int:
    """How much AUTOMATIC top-up this org has bought since the 1st, in micro-USD.

    Computed from the ledger rather than a counter column, because the ledger is the record that gets
    reconciled against Stripe — a counter can drift, and a cap enforced against a drifted counter is
    either a runaway or a lockout. Manual top-ups are excluded on purpose: the cap exists to bound
    what happens WITHOUT a human, and a human choosing to add funds is not that.
    """
    # The `auto` flag lives in the entry's JSON meta, and JSON containment isn't portable across
    # SQLite and Postgres — so the month's top-ups are narrowed in SQL (indexed on org + created_at)
    # and the flag is matched in Python. A month's top-ups is a handful of rows, not a scan.
    rows = (await db.execute(
        select(LedgerEntry.amount_micro, LedgerEntry.meta).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "topup",
            LedgerEntry.created_at >= _month_start(),
        )
    )).all()
    return sum(int(a) for a, meta in rows if isinstance(meta, dict) and meta.get("auto"))


async def owner_email(db: AsyncSession, org_id: int) -> str:
    """Where a billing email goes: the org's owner, falling back to an admin. Billing is not team
    news — it names a card and a charge — so it goes to whoever can actually act on it, not to
    everyone with a token."""
    rows = (await db.execute(
        select(User.email, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id, Membership.role.in_(("owner", "admin")))
    )).all()
    for email, role in rows:
        if role == "owner":
            return email
    return rows[0][0] if rows else ""


async def get_or_create_customer(db: AsyncSession, org: Org, *, email: str = "") -> str:
    """The org's Stripe Customer id, creating it on first use. Commits when it creates one.

    Stored on the org and never recreated: the Customer is what the saved card hangs off, so a second
    one would orphan the payment method and silently break auto-top-up.
    """
    if org.stripe_customer_id:
        return org.stripe_customer_id
    if not email:
        email = await owner_email(db, org.id)
    customer = await _sdk(
        stripe_adapter.CUSTOMER_CREATE,
        name=org.name or org.slug,
        **({"email": email} if email else {}),
        # The org id travels with the customer so a Stripe-side investigation ("who is this charge
        # for?") resolves without a lookup table.
        metadata={"treg_org_id": str(org.id), "treg_org_slug": org.slug},
    )
    org.stripe_customer_id = customer["id"]
    db.add(org)
    await db.commit()
    return org.stripe_customer_id


async def _set_default_pm(db: AsyncSession, org_id: int, payment_method: str | None, *,
                          paid: bool = False) -> bool:
    """Remember which saved card to charge off-session. An opaque `pm_…` reference — no card data
    ever reaches our database. Returns True when something changed. `paid`: this card just paid."""
    if not payment_method:
        return False
    org = await db.get(Org, org_id)
    if org is None:
        return False
    changed = org.stripe_default_pm != payment_method
    org.stripe_default_pm = payment_method
    # A card arriving is what a consented-but-cardless policy was waiting for, whichever door it came
    # through: the dashboard's top-up modal records consent and then relies on the top-up Checkout to
    # save the card, so the PAYMENT webhook has to arm it, not only the setup one. Checked even when
    # the pm is unchanged (a redelivered webhook after a crash between the two commits). Declines
    # re-arm on a card that just paid or a newly saved one. A manual payment also resolves a 3DS
    # pause; merely saving a card does not. A deliberate off stays off.
    armed = _arm_if_waiting_for_card(org, card_proven=paid or changed, paid=paid)
    if changed or armed:
        db.add(org)
        await db.commit()
    return changed


def _arm_if_waiting_for_card(org: Org, *, card_proven: bool = False, paid: bool = False,
                             auto_paid: bool = False) -> bool:
    """Turn a consented policy back on when the reason it was off is gone. Mutates, does not commit.

    `no_card`: a card now exists. `max_attempts:*` (repeated declines): only when `card_proven`, a
    card that just paid or a newly saved one. Before this a decline never re-armed, so a team paying
    by hand with a working card kept auto top-up off and kept getting balance refusals.
    `stripe_unconfirmed`: only when an automatic top-up was confirmed paid (`auto_paid`). One
    invoice serves each refill until it is credited, so that payment is the one the pause doubted;
    a manual payment is not.
    """
    if not (org.stripe_default_pm and org.autotopup_consented_at) or org.autotopup_enabled:
        return False
    # A deliberate off leaves the reason None with consent still on file, and a later (or
    # redelivered) payment must not switch it back on.
    reason = org.autotopup_disabled_reason or ""
    if not (reason == "no_card" or (card_proven and reason.startswith("max_attempts:"))
            or (paid and reason == "authentication_required")
            or (auto_paid and reason == "stripe_unconfirmed")):
        return False
    org.autotopup_enabled = True
    org.autotopup_disabled_reason = None
    org.autotopup_failures = 0
    org.autotopup_recovery_pi = None
    return True


async def _pm_and_fingerprint(pi_id: str) -> tuple[str | None, str | None]:
    """`(payment_method_id, card_fingerprint)` for a succeeded PaymentIntent, in ONE retrieve.

    The fingerprint is Stripe's stable per-CARD identifier: the same physical card presented under a
    different email, name or Customer produces the same string. That makes it the only durable
    signal the referral program's abuse checks have — an email address is free, a card is not (see
    referrals.qualify). It is not card data: the value is opaque and meaningless outside our own
    Stripe account, so nothing here weakens `Org.stripe_default_pm`'s no-card-data-in-our-DB posture.
    It is stored on the `referral` row alone, never on an org.

    `expand` rather than a second `PaymentMethod.retrieve`: this call already happens on every
    Checkout completion, and one round trip on the webhook path is enough.
    """
    try:
        pi = await _sdk(stripe_adapter.PAYMENT_INTENT_RETRIEVE, id=pi_id, expand=["payment_method"])
    except Exception as e:  # noqa: BLE001 — a card we can't look up just means no auto-top-up yet
        log.warning("billing: could not retrieve PaymentIntent %s: %s", pi_id, e)
        return None, None
    pm = pi.get("payment_method")
    if isinstance(pm, str):  # not expanded (an older API version, or a fake in tests)
        return pm, None
    pm = pm or {}
    fingerprint = ((pm.get("card") or {}).get("fingerprint")) or None
    return pm.get("id"), fingerprint


async def create_topup_checkout(
    db: AsyncSession, org: Org, amount_usd, *, return_base: str = "", email: str = "",
    entry_surface: str = "", checkout_source: str = "",
) -> dict:
    """Start a hosted Checkout for a one-off top-up and return `{url, session_id, amount_micro}`.

    Stripe-hosted (`mode=payment`) rather than an embedded form: it needs no Stripe.js, no card fields
    of ours, and no PCI surface — the payer leaves, pays on Stripe, and comes back.

    `setup_future_usage="off_session"` is what makes auto-top-up possible later: it saves the card
    WITH the mandate signals PSD2/SCA requires for an unattended charge. Asking for it at purchase
    time costs the payer nothing; retrofitting consent afterwards costs them a second card entry.

    `invoice_creation` is what makes the payment expensable. Stripe's card receipt proves a card was
    charged; an invoice is the document with an invoice number, a billing address and a tax ID on it,
    which is what a finance team actually accepts. It is post-purchase — the invoice appears once the
    payment succeeds, not when the session opens — so nothing here changes what the payer sees.

    `billing_address_collection` + `customer_update` are what put a real address on that invoice. The
    address is asked for on the Checkout page and written back to the Customer, so the NEXT top-up —
    and every portal-rendered invoice — already has it. Without `customer_update` Stripe collects the
    address for the payment and then throws it away, which is the failure mode that looks like it
    works.

    `allow_promotion_codes` shows the "Add promotion code" field. Note what a discount does downstream:
    the webhook credits `amount_total`, i.e. what Stripe actually collected, so 20% off means the payer
    pays $40 and is credited $40 of balance — a discount on the price, NOT a bonus on the balance. A
    "pay $40, get $50" promotion is a different mechanism (`ledger.grant`) and is not built.

    Currency is pinned to USD explicitly — the Stripe account's own default is AUD, and inheriting it
    would charge a number the ledger would then credit as dollars.
    """
    attribution = {"entry_surface": analytics.funnel_surface(entry_surface),
                   "checkout_source": analytics.funnel_surface(checkout_source)}
    amount = validate_topup_usd(amount_usd)
    amount_micro = usd_to_micro(amount)
    customer = await get_or_create_customer(db, org, email=email)
    base = (return_base or get_settings().public_url).rstrip("/")
    session = await _sdk(
        stripe_adapter.CHECKOUT_SESSION_CREATE,
        mode="payment",
        customer=customer,
        # No `payment_method_types`: omitting it lets Stripe pick the methods most likely to convert
        # for this payer, configured from the dashboard rather than from our code.
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": micro_to_cents(amount_micro),
                "product_data": {
                    "name": "treg balance top-up",
                    "description": f"${amount} of prepaid API call balance for {org.name or org.slug}",
                },
            },
        }],
        payment_intent_data={
            # The card is saved for auto-top-up (see docstring). The metadata is repeated here because
            # the webhook may arrive as `payment_intent.succeeded`, which carries the PI's metadata and
            # not the session's — the credit path must work from either event.
            "setup_future_usage": "off_session",
            "metadata": {"treg_org_id": str(org.id), "treg_kind": "topup", "treg_auto": "0", **attribution},
            **({"receipt_email": email} if email else {}),
        },
        # The expensable document (see docstring). `invoice_data` carries the same org identity as the
        # session so an invoice found in the Stripe dashboard resolves to a team without a lookup.
        invoice_creation={
            "enabled": True,
            "invoice_data": {
                "description": f"Prepaid API call balance for {org.name or org.slug}",
                "metadata": {"treg_org_id": str(org.id), "treg_kind": "topup"},
            },
        },
        # Billing identity for that invoice (see docstring). "required" asks every payer, not only the
        # ones whose payment method forces it; `customer_update` is what persists the answer onto the
        # Customer instead of onto this one payment.
        billing_address_collection="required",
        customer_update={"address": "auto", "name": "auto"},
        # The promotion-code field. Codes themselves are created in the Stripe dashboard, so a campaign
        # needs no deploy here.
        allow_promotion_codes=True,
        # Idempotent per (org, amount, minute): a double-clicked "Add funds" reuses the same session
        # instead of opening a second one the payer might also complete.
        metadata={"treg_org_id": str(org.id), "treg_kind": "topup", **attribution},
        integration_identifier=INTEGRATION_ID,
        success_url=f"{base}/app?topup=success#billing",
        cancel_url=f"{base}/app?topup=cancelled#billing",
    )
    return {"url": session["url"], "session_id": session["id"],
            "amount_micro": amount_micro, "amount_usd": amount}


async def create_setup_checkout(db: AsyncSession, org: Org, *, return_base: str = "", email: str = "") -> dict:
    """Start a hosted Checkout in `mode=setup` — save a card WITHOUT charging it, so auto-top-up can
    be armed by an org that hasn't bought anything yet.

    Chosen over a bare SetupIntent + Elements for exactly one reason: Elements needs Stripe.js and a
    card form we would own. This is the same hosted page as the top-up flow, so there is one payment
    surface to reason about instead of two.
    """
    customer = await get_or_create_customer(db, org, email=email)
    base = (return_base or get_settings().public_url).rstrip("/")
    session = await _sdk(
        stripe_adapter.CHECKOUT_SESSION_CREATE,
        mode="setup",
        customer=customer,
        # Required in setup mode when `payment_method_types` is unset; the cards it lets the payer
        # save are the ones that can pay our USD invoices. The account's default is AUD.
        currency="usd",
        # Checkout's setup-mode SetupIntent is already `usage=off_session`, so the bank collects the SCA
        # mandate now. Checkout rejects `setup_intent_data[usage]`; it accepts only description,
        # metadata and on_behalf_of there.
        setup_intent_data={"metadata": {"treg_org_id": str(org.id), "treg_kind": "autotopup_card"}},
        metadata={"treg_org_id": str(org.id), "treg_kind": "autotopup_card"},
        integration_identifier=INTEGRATION_ID,
        success_url=f"{base}/app?card=saved#billing",
        cancel_url=f"{base}/app?card=cancelled#billing",
    )
    return {"url": session["url"], "session_id": session["id"], "mode": "setup"}


async def create_portal_session(db: AsyncSession, org: Org, *, return_base: str = "") -> dict:
    """A one-time link into Stripe's hosted Customer Portal, where the payer manages their own
    billing details: card, billing address, tax ID, and the invoice history with its PDFs.

    Hosted rather than rebuilt here for the same reason Checkout is: every one of those fields is a
    form we would otherwise own, and the tax-ID one carries validation rules per country that go
    stale. The portal also renders the invoice archive we now generate, so "download an old invoice"
    needs no page of ours at all.

    Requires a portal CONFIGURATION saved in the Stripe dashboard (Settings → Billing → Customer
    portal); without one Stripe rejects the call, which surfaces here as the usual Stripe error.

    Refuses an org with no Stripe customer instead of creating one. A customer exists once someone has
    paid or saved a card; minting one to open an empty portal would fill the Stripe account with
    customers that never bought anything, and the portal would have nothing to show anyway.
    """
    _require_configured()
    if not org.stripe_customer_id:
        raise TopupRejected("no billing details yet — add funds once and the portal opens after that")
    base = (return_base or get_settings().public_url).rstrip("/")
    session = await _sdk(
        stripe_adapter.PORTAL_SESSION_CREATE,
        customer=org.stripe_customer_id,
        return_url=f"{base}/app#billing",
    )
    return {"url": session["url"]}


# Split in two so the DB session (see `get_payment_history`) can close before the Stripe round
# trip in `_payment_documents`: a pooled connection must never sit open across an upstream call.
async def _load_payment_rows(
    db: AsyncSession, org: Org, *, limit: int = 24,
    before: tuple[datetime, str] | None = None,
) -> tuple[list[CreditBlock], set[str], dict[str, int]]:
    """The org's completed top-ups, newest first: DB rows, their auto-topup flags, bonus amounts.

    The ROWS come from our own `CreditBlock` table, not from Stripe. That table is what the balance is
    computed from, so a history built on it can never show a payment the balance disagrees with — and
    it needs no network call to render amounts and dates. Read-only: nothing here moves money or
    writes to the ledger.
    """
    query = select(CreditBlock).where(
        CreditBlock.org_id == org.id, CreditBlock.kind == "purchased",
        CreditBlock.stripe_payment_intent.is_not(None),
    )
    if before:
        query = query.where(or_(CreditBlock.created_at < before[0],
                                and_(CreditBlock.created_at == before[0], CreditBlock.id < before[1])))
    blocks = (await db.execute(query.order_by(CreditBlock.created_at.desc(), CreditBlock.id.desc())
                               .limit(max(1, min(int(limit), 100))))).scalars().all()

    # Which of these top-ups were automatic. The flag lives in the ledger entry's JSON meta, and JSON
    # containment isn't portable across SQLite and Postgres — so the rows are narrowed by block id
    # (exactly the page we're rendering) and the flag is read in Python, the same shape as
    # `monthly_autotopup_spend`.
    auto: set[str] = set()
    if blocks:
        rows = (await db.execute(
            select(LedgerEntry.block_id, LedgerEntry.meta).where(
                LedgerEntry.org_id == org.id, LedgerEntry.kind == "topup",
                LedgerEntry.block_id.in_([b.id for b in blocks]),
            )
        )).all()
        auto = {str(bid) for bid, meta in rows if isinstance(meta, dict) and meta.get("auto")}
    # The bonus each payment earned, keyed by its PaymentIntent (stamped into the grant entry's
    # meta by `_credit`). Read the same way as `auto`: the org's grant entries, matched in Python.
    bonus_by_pi: dict[str, int] = {}
    if blocks:
        grants = (await db.execute(
            select(LedgerEntry.amount_micro, LedgerEntry.meta).where(
                LedgerEntry.org_id == org.id, LedgerEntry.kind == "grant")
        )).all()
        for amt, meta in grants:
            if isinstance(meta, dict) and meta.get("source") == "topup_bonus" and meta.get("payment_intent"):
                bonus_by_pi[str(meta["payment_intent"])] = bonus_by_pi.get(str(meta["payment_intent"]), 0) + int(amt)

    return list(blocks), auto, bonus_by_pi


def _assemble_payments(
    blocks: list[CreditBlock], auto: set[str], bonus_by_pi: dict[str, int],
    docs: dict[str, dict], stripe_ok: bool,
) -> dict:
    """Join DB rows with Stripe documents. Pure — no I/O, safe to call after the session is closed."""
    items = []
    for b in blocks:
        d = docs.get(b.stripe_payment_intent or "", {})
        items.append({
            "payment_intent": b.stripe_payment_intent,
            "amount_micro": int(b.amount_micro),
            "amount_usd": ledger.usd(int(b.amount_micro)),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "auto": b.id in auto,
            "bonus_micro": bonus_by_pi.get(b.stripe_payment_intent or "", 0),
            "invoice_number": d.get("number") or "",
            "invoice_pdf": d.get("invoice_pdf") or "",
            "hosted_invoice_url": d.get("hosted_invoice_url") or "",
            "receipt_url": d.get("receipt_url") or "",
        })
    return {"items": items, "stripe_ok": stripe_ok}


async def _payment_documents(org: Org, wanted: int) -> tuple[dict[str, dict], bool]:
    """`{payment_intent_id: {receipt_url, number, invoice_pdf, hosted_invoice_url}}` for one customer.

    Two list calls (charges, invoices) rather than two per row, joined in memory through invoice
    payments (or the charge's legacy `invoice` field). Both windows cap at 100 payments, so a very
    old top-up on a heavily-used account can come back link-less; the portal
    (`create_portal_session`) is the unbounded archive. Returns
    `({}, False)` on any Stripe failure — the caller renders amounts without links rather than a 500.
    """
    if not (org.stripe_customer_id and wanted and configured()):
        return {}, bool(configured())
    window = max(wanted, 100)
    try:
        charges = await _sdk(stripe_adapter.CHARGE_LIST, customer=org.stripe_customer_id, limit=window)
        invoices = await _sdk(stripe_adapter.INVOICE_LIST, customer=org.stripe_customer_id,
                              limit=window, expand=["data.payments"])
    except Exception as e:  # noqa: BLE001 — see docstring: links are optional, the history is not
        log.warning("billing: could not load payment documents for org %s: %s", org.id, e)
        return {}, False

    by_invoice = {}
    by_pi = {}
    for inv in (invoices.get("data") or []):
        if inv.get("id"):
            by_invoice[inv["id"]] = {"number": inv.get("number") or "",
                                     "invoice_pdf": inv.get("invoice_pdf") or "",
                                     "hosted_invoice_url": inv.get("hosted_invoice_url") or ""}
            for entry in (inv.get("payments") or {}).get("data") or []:
                payment = entry.get("payment") or {}
                pi = payment.get("payment_intent")
                pi_id = pi if isinstance(pi, str) else (pi or {}).get("id")
                if pi_id:
                    by_pi[pi_id] = by_invoice[inv["id"]]
    out: dict[str, dict] = {}
    for ch in (charges.get("data") or []):
        pi = ch.get("payment_intent")
        pi = pi if isinstance(pi, str) else (pi or {}).get("id")
        if not pi:
            continue
        inv = ch.get("invoice")
        inv = inv if isinstance(inv, str) else (inv or {}).get("id")
        out[pi] = {"receipt_url": ch.get("receipt_url") or "",
                   **(by_pi.get(pi) or by_invoice.get(inv or "", {}))}
    return out, True


async def _subscription_invoices(
    customer_id: str, org_id: int, subscription_id: str, limit: int, after: str = "",
) -> tuple[list[dict], bool, bool]:
    """SSO invoices across renewals and later re-purchases, excluding prepaid invoices.

    Subscription metadata is copied onto invoice subscription details at creation. The current
    subscription ID also covers an invoice that has not yet received that metadata snapshot.
    """
    rows = []
    payment_intents: dict[str, str] = {}
    scan_after = after
    more = False
    try:
        while len(rows) < limit:
            kw = {"customer": customer_id, "limit": 100}
            if scan_after:
                kw["starting_after"] = scan_after
            page = await _sdk(stripe_adapter.INVOICE_LIST, expand=["data.payments"], **kw)
            invoices = page.get("data") or []
            for inv in invoices:
                if not inv.get("id"):
                    continue
                details = (inv.get("parent") or {}).get("subscription_details") or inv.get("subscription_details") or {}
                metadata = details.get("metadata") or {}
                sub = details.get("subscription") or inv.get("subscription")
                if (metadata.get("treg_kind") != "enterprise_sso" or metadata.get("treg_org_id") != str(org_id)) and sub != subscription_id:
                    continue
                created = inv.get("created")
                rows.append({
                    "id": inv["id"],
                    "number": inv.get("number") or "",
                    "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat() if isinstance(created, int) else None,
                    "amount_micro": cents_to_micro(int(inv.get("total") or 0)),
                    "currency": inv.get("currency") or "usd",
                    "status": inv.get("status") or "",
                    "invoice_pdf": inv.get("invoice_pdf") or "",
                    "hosted_invoice_url": inv.get("hosted_invoice_url") or "",
                    "receipt_url": "",
                })
                if inv.get("status") == "paid":
                    for entry in (inv.get("payments") or {}).get("data") or []:
                        payment = entry.get("payment") or {}
                        pi = payment.get("payment_intent")
                        pi_id = pi if isinstance(pi, str) else (pi or {}).get("id")
                        if pi_id:
                            payment_intents[inv["id"]] = pi_id
                            break
                if len(rows) == limit:
                    break
            if len(rows) == limit:
                more = True
                break
            if not page.get("has_more") or not invoices or not invoices[-1].get("id") or invoices[-1]["id"] == scan_after:
                break
            scan_after = invoices[-1]["id"]
    except Exception as e:  # noqa: BLE001 — keep the billing page usable during a Stripe outage
        log.warning("billing: could not load SSO invoices for org %s: %s", org_id, e)
        return [], False, False
    # A receipt belongs to the successful Charge, not the Invoice. Query by that invoice's
    # PaymentIntent so even an older renewal has its own receipt; missing/failed lookups keep the
    # invoice visible. The semaphore prevents a history page from bursting Stripe with requests.
    async def receipt_for(row: dict) -> None:
        pi_id = payment_intents.get(row["id"])
        if not pi_id:
            return
        async with receipt_slots:
            try:
                charges = await _sdk(stripe_adapter.CHARGE_LIST, customer=customer_id,
                                     payment_intent=pi_id, limit=1)
                row["receipt_url"] = next((charge.get("receipt_url") or ""
                    for charge in charges.get("data") or [] if charge.get("status") == "succeeded"), "")
            except Exception as e:  # noqa: BLE001 — a receipt is optional; the invoice remains
                log.warning("billing: could not load SSO receipt for org %s: %s", org_id, e)

    receipt_slots = asyncio.Semaphore(5)
    await asyncio.gather(*(receipt_for(row) for row in rows))
    return rows, True, more


# Per-org in-process lock. Cheap first line of defence against a burst of concurrent calls all
# noticing the same low balance; the DB cooldown below is what covers the multi-instance case, and
# the Stripe idempotency key is what covers both even if the first two fail.
_locks: dict[int, asyncio.Lock] = {}


def _lock(org_id: int) -> asyncio.Lock:
    lock = _locks.get(org_id)
    if lock is None:
        lock = _locks[org_id] = asyncio.Lock()
    return lock


def _idempotency_key(org_id: int, amount_micro: int, crossing: int, payment_method: str,
                     generation: int = 0) -> str:
    """One key per credited purchase and Stripe-recorded invoice generation.

    `crossing` counts all credited Stripe purchases, not just this month's. A day or month in the
    key would lose an unresolved invoice at midnight. A void invoice advances `generation` and
    permits a new invoice; an uncertain Stripe response keeps the same invoice for reconciliation.

    `payment_method` is in the key because Stripe rejects a reused key whose PARAMETERS changed, with
    an error, not a replay. Without it, one failed attempt would poison the crossing: swap the declined
    card for a working one and the retry dies on "keys for idempotent requests can only be used with
    the same parameters" until the key changes. Including it keeps the collapse-a-burst property
    (same card, same key) while letting a genuinely different charge through.
    """
    raw = f"autotopup:{org_id}:{crossing}:{generation}:{amount_micro}:{payment_method}"
    return "treg_" + hashlib.sha256(raw.encode()).hexdigest()[:40]


async def _attempt_invoice(customer: str, base_key: str, org_id: int,
                           created_gte: int | None = None,
                           last_auto_credit_at: int | None = None,
                           db: AsyncSession | None = None,
                           payment_method: str = "") -> tuple[dict | None, int]:
    """Find the attempt and unresolved prior auto invoices in Stripe's bounded customer list.

    Walk every page: an incomplete lookup cannot prove that a charge was never started. Stripe
    removes idempotency records after 24 hours, but invoices and their attempt metadata persist.
    """
    cursor = ""
    found: dict[int, dict] = {}
    prior_unfinished: list[dict] = []
    prior_recent_paid: list[dict] = []
    while True:
        kw = {"customer": customer, "limit": 100}
        if created_gte is not None:
            kw["created"] = {"gte": created_gte}
        if cursor:
            kw["starting_after"] = cursor
        page = await _sdk(stripe_adapter.INVOICE_LIST, **kw)
        invoices = page.get("data") or []
        for invoice in invoices:
            metadata = invoice.get("metadata") or {}
            if metadata.get("treg_attempt_base") == base_key:
                generation = int(metadata.get("treg_attempt_generation", "0"))
                if generation in found:
                    raise ValueError("multiple Stripe invoices for one auto top-up attempt")
                found[generation] = invoice
            elif (metadata.get("treg_org_id") == str(org_id)
                  and metadata.get("treg_kind") == "topup"
                  and metadata.get("treg_auto") == "1"):
                status = invoice.get("status")
                # A paid invoice created after the latest auto credit may have charged while its
                # reply and webhook were lost. Its old base key cannot prove it was credited.
                created = invoice.get("created")
                paid_after_credit = (status == "paid" and
                                     (last_auto_credit_at is None or not isinstance(created, int)
                                      or created >= last_auto_credit_at))
                if status in ("draft", "open"):
                    prior_unfinished.append(invoice)
                elif paid_after_credit:
                    prior_recent_paid.append(invoice)
        if not page.get("has_more"):
            if prior_unfinished:
                for prior in prior_unfinished:
                    # Re-read before deciding: a payment may have settled since Invoice.list.
                    current = await _sdk(stripe_adapter.INVOICE_RETRIEVE, id=prior["id"])
                    status = current.get("status")
                    if status == "paid":
                        prior_recent_paid.append(current)
                        continue
                    if status == "void":
                        continue
                    if status != "open":
                        raise PriorAutoInvoiceUnresolved(prior["id"])
                    pi_id = await _invoice_payment_intent(current)
                    if not pi_id:
                        raise PriorAutoInvoiceUnresolved(prior["id"])
                    payment = await _sdk(stripe_adapter.PAYMENT_INTENT_RETRIEVE, id=pi_id)
                    payment_status = payment.get("status")
                    old_pm = current.get("default_payment_method")
                    if isinstance(old_pm, dict):
                        old_pm = old_pm.get("id")
                    safe_to_void = payment_status in ("requires_payment_method", "canceled") or (
                        payment_status == "requires_action" and old_pm and payment_method
                        and old_pm != payment_method)
                    if not safe_to_void:
                        raise PriorAutoInvoiceUnresolved(prior["id"])
                    # Voiding an invoice cancels its PaymentIntent. An uncertain void response
                    # must stop this attempt before a new invoice can charge.
                    closed = await _sdk(stripe_adapter.INVOICE_VOID, invoice=prior["id"],
                                        idempotency_key=f"{prior['id']}:void-replaced")
                    if closed.get("status") != "void":
                        raise PriorAutoInvoiceUnresolved(prior["id"])
            # Stripe timestamps are only second precision, so a paid invoice from the latest
            # credited second is ambiguous. Check its PaymentIntent against the local ledger;
            # every query is rolled back before another Stripe call.
            for prior in prior_recent_paid:
                pi_id = await _invoice_payment_intent(prior)
                if not pi_id or db is None:
                    raise PriorAutoInvoiceUnresolved(prior["id"])
                credited = (await db.execute(
                    select(CreditBlock.id).where(CreditBlock.stripe_payment_intent == pi_id)
                )).first() is not None
                await db.rollback()
                if not credited:
                    raise PriorAutoInvoiceUnresolved(prior["id"])
            if not found:
                return None, 0
            active = [invoice for invoice in found.values() if invoice.get("status") != "void"]
            if len(active) > 1:
                raise ValueError("multiple active Stripe invoices for one auto top-up crossing")
            # A changed card, amount, or manual top-up changes the base key, but must not make
            # an older payment in progress (or paid without a confirmed credit) disappear.
            if active:
                invoice = active[0]
                return invoice, int((invoice.get("metadata") or {})["treg_attempt_generation"])
            return None, max(found) + 1
        if not invoices or not invoices[-1].get("id") or invoices[-1]["id"] == cursor:
            raise ValueError("incomplete Stripe invoice pagination")
        cursor = invoices[-1]["id"]


async def _last_auto_credit_at(db: AsyncSession, org_id: int) -> datetime | None:
    """Manual top-ups cannot advance the invoice recovery boundary."""
    offset = 0
    while True:
        rows = (await db.execute(
            select(LedgerEntry.created_at, LedgerEntry.meta).where(
                LedgerEntry.org_id == org_id, LedgerEntry.kind == "topup"
            ).order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
            .limit(100).offset(offset)
        )).all()
        for created_at, meta in rows:
            if isinstance(meta, dict) and meta.get("auto"):
                return created_at
        if len(rows) < 100:
            return None
        offset += 100


async def _attempt_has_item(invoice_id: str, key: str) -> bool:
    """A draft recovered after Stripe's 24-hour key retention must not gain a second line item."""
    cursor = ""
    while True:
        kw = {"invoice": invoice_id, "limit": 100}
        if cursor:
            kw["starting_after"] = cursor
        page = await _sdk(stripe_adapter.INVOICE_ITEM_LIST, **kw)
        items = page.get("data") or []
        if any((item.get("metadata") or {}).get("treg_attempt") == key for item in items):
            return True
        if not page.get("has_more"):
            return False
        if not items or not items[-1].get("id") or items[-1]["id"] == cursor:
            raise ValueError("incomplete Stripe invoice item pagination")
        cursor = items[-1]["id"]


async def _invoice_payment_intent(invoice: dict) -> str:
    """The pinned Stripe API exposes invoice payments, not invoice.payment_intent."""
    payments = (invoice.get("payments") or {}).get("data") or []
    if not payments:
        payments = (await _sdk(stripe_adapter.INVOICE_PAYMENT_LIST,
                               invoice=invoice["id"], limit=10)).get("data") or []
    for entry in payments:
        payment = entry.get("payment") or {}
        pi = payment.get("payment_intent")
        pi_id = pi if isinstance(pi, str) else (pi or {}).get("id")
        if pi_id:
            return pi_id
    return ""


async def _invoice_metadata_for_payment(pi_id: str) -> dict:
    """Invoice-created PaymentIntents do not inherit invoice metadata."""
    matches = await _sdk(stripe_adapter.INVOICE_PAYMENT_LIST,
                         payment={"type": "payment_intent", "payment_intent": pi_id}, limit=1)
    for match in matches.get("data") or []:
        invoice = match.get("invoice")
        invoice_id = invoice if isinstance(invoice, str) else (invoice or {}).get("id")
        if invoice_id:
            invoice = await _sdk(stripe_adapter.INVOICE_RETRIEVE, id=invoice_id)
            return invoice.get("metadata") or {}
    return {}


async def _invoice_metadata_for_known_customer(db: AsyncSession, pi: dict) -> dict:
    """Only a PaymentIntent for one of our Stripe customers needs an invoice lookup."""
    customer = pi.get("customer")
    customer_id = customer if isinstance(customer, str) else (customer or {}).get("id")
    if not customer_id or not pi.get("id"):
        return {}
    org_id = (await db.execute(
        select(Org.id).where(Org.stripe_customer_id == customer_id)
    )).scalar_one_or_none()
    # The lookup started a transaction; do not keep its connection during Stripe I/O.
    await db.rollback()
    if org_id is None:
        return {}
    meta = await _invoice_metadata_for_payment(pi["id"])
    return meta if meta.get("treg_org_id") == str(org_id) else {}


async def _stop_failed_invoice(invoice_id: str, key: str) -> None:
    """No failed automatic invoice may be retried or dunned by Stripe."""
    if not invoice_id:
        return
    try:
        invoice = await _sdk(stripe_adapter.INVOICE_RETRIEVE, id=invoice_id)
        if invoice.get("status") == "open":
            await _sdk(stripe_adapter.INVOICE_VOID, invoice=invoice_id,
                       idempotency_key=f"{key}:void")
        # A draft cannot collect, and a paid invoice must remain available to the webhook.
    except Exception as exc:  # noqa: BLE001 - auto_advance=False is the retry backstop
        log.warning("billing: could not close failed auto top-up invoice %s: %s", invoice_id, exc)


async def _void_auth_invoice(invoice_id: str, pi_id: str = "") -> bool:
    """A confirmed 3DS failure can be abandoned only after Stripe confirms the invoice is void."""
    if not invoice_id:
        return False
    try:
        invoice = await _sdk(stripe_adapter.INVOICE_RETRIEVE, id=invoice_id)
        if invoice.get("status") == "void":
            return True
        if invoice.get("status") != "open":
            return False
        pi_id = pi_id or await _invoice_payment_intent(invoice)
        if not pi_id:
            return False
        payment = await _sdk(stripe_adapter.PAYMENT_INTENT_RETRIEVE, id=pi_id)
        if payment.get("status") not in ("requires_action", "requires_payment_method", "canceled"):
            return False
        closed = await _sdk(stripe_adapter.INVOICE_VOID, invoice=invoice_id,
                            idempotency_key=f"{invoice_id}:void-auth")
        return closed.get("status") == "void"
    except Exception as exc:  # noqa: BLE001 - uncertain void must never permit a new charge
        log.error("billing: cannot confirm void for 3DS auto invoice %s: %s", invoice_id, exc)
        return False


async def _pause_for_auth(db: AsyncSession, org_id: int, invoice_id: str,
                          pi_id: str = "") -> dict:
    if await _void_auth_invoice(invoice_id, pi_id):
        reason = "authentication_required"
    else:
        reason = "stripe_unconfirmed"
        log.error("billing: 3DS auto invoice %s is unresolved for org %s", invoice_id, org_id)
    org = await db.get(Org, org_id)
    if org is not None:
        await _disable_autotopup(db, org, reason)
    return {"ok": False, "reason": reason}


async def _disable_autotopup(db: AsyncSession, org: Org, reason: str, *, recovery_pi: str = "") -> None:
    """Turn auto-top-up off and SAY WHY. A silent disable is how an org finds out from its agents
    failing; the reason is what the dashboard banner and the email are built from."""
    org.autotopup_enabled = False
    org.autotopup_disabled_reason = reason
    org.autotopup_recovery_pi = recovery_pi or None
    db.add(org)
    await db.commit()
    to = await owner_email(db, org.id)
    org_name = org.name or org.slug
    await db.rollback()  # owner lookup must not hold a connection through the email request
    if to:
        await email_mod.send_autotopup_disabled(to, org_name, reason,
                                                recovery_pi=bool(recovery_pi))


async def _record_unconfirmed(db: AsyncSession, org_id: int, invoice_id: str,
                              detail: str) -> dict:
    """An unknown outcome is a failed attempt, but it never rotates the Stripe invoice key."""
    log.error("billing: auto top-up outcome unconfirmed for org %s invoice %s: %s",
              org_id, invoice_id or "unknown", detail)
    return await _record_failure(db, org_id, "stripe_unconfirmed")


async def attempt_auto_topup(db: AsyncSession, org_id: int) -> dict:
    """Charge the saved card off-session to refill a low balance. Returns
    `{"ok": bool, "reason": str, ...}` and never raises at the caller — a failed refill must degrade
    to "the agent gets a 402", never to a 500 on someone else's request.

    Every guard below is a way this can cost money it shouldn't, checked in cheapest-first order:

      consent   — no `autotopup_consented_at` means no mandate; charging anyway is an unauthorized
                  charge, so this refuses even if the enabled flag somehow says yes.
      cooldown  — `autotopup_last_attempt_at` is stamped in the DB *before* the Stripe call, so a
                  second web worker (or a redeployed instance) sees it. This is the cross-instance
                  half of the lock.
      cap       — the month's automatic spend, from the ledger, plus this charge, against the cap.
      attempts  — consecutive failures; hitting the limit disables rather than retrying forever.
      threshold — re-read under the lock, because the balance may have been topped up by the time we
                  got here (a manual top-up, or the winner of a race).
    """
    if not configured():
        return {"ok": False, "reason": "not_configured"}
    async with _lock(org_id):
        org = await db.get(Org, org_id)
        if org is None:
            return {"ok": False, "reason": "no_org"}
        if not org.autotopup_enabled:
            return {"ok": False, "reason": "disabled"}
        if not org.autotopup_consented_at:
            return {"ok": False, "reason": "no_consent"}
        if not (org.stripe_customer_id and org.stripe_default_pm):
            return {"ok": False, "reason": "no_card"}

        prefs = autotopup_prefs(org)
        s = get_settings()
        if org.autotopup_failures >= s.autotopup_max_attempts:
            await _disable_autotopup(db, org, "max_attempts")
            return {"ok": False, "reason": "max_attempts"}
        last = org.autotopup_last_attempt_at
        if last is not None and (_now() - last).total_seconds() < autotopup_wait_s(org):
            return {"ok": False, "reason": "cooldown"}

        balance = await ledger.balance_of(db, org_id)
        if balance >= prefs["threshold_micro"]:
            return {"ok": False, "reason": "above_threshold", "balance_micro": balance}

        amount_micro = prefs["amount_micro"]
        spent = await monthly_autotopup_spend(db, org_id)
        if autotopup_cap_reached(prefs, spent):
            return {"ok": False, "reason": "monthly_cap", "spent_micro": spent,
                    "cap_micro": prefs["monthly_cap_micro"]}

        # Claim the attempt BEFORE talking to Stripe. If this process dies mid-charge, the cooldown
        # has already been recorded — the next attempt waits instead of double-charging blind.
        # The claim is a compare-and-set on the stamp we just read: `_lock` is per process, so
        # every web process and instance that read the same old stamp races here, and only the
        # first UPDATE matches. The rest stop before Stripe instead of piling onto one invoice.
        stamp = _now()
        claimed = await db.execute(
            update(Org).where(
                Org.id == org_id,
                Org.autotopup_last_attempt_at.is_(None) if last is None
                else Org.autotopup_last_attempt_at == last,
            ).values(autotopup_last_attempt_at=stamp)
            .execution_options(synchronize_session=False))
        await db.commit()
        if claimed.rowcount != 1:
            return {"ok": False, "reason": "cooldown"}

        crossing = (await db.execute(select(func.count(CreditBlock.id)).where(
            CreditBlock.org_id == org_id, CreditBlock.stripe_payment_intent.is_not(None)
        ))).scalar_one()
        last_auto_credit = await _last_auto_credit_at(db, org_id)
        created_gte = (int(last_auto_credit.replace(tzinfo=timezone.utc).timestamp()) - 86400
                       if last_auto_credit else None)
        to = await owner_email(db, org_id)
        customer_id = org.stripe_customer_id
        payment_method = org.stripe_default_pm
        org_name = org.name or org.slug
        base_key = _idempotency_key(org_id, amount_micro, crossing, payment_method)
        invoice_id = ""
        cents = micro_to_cents(amount_micro)
        # Reads above opened a new transaction after the cooldown commit. Release its connection
        # before the Stripe list/create/finalize/pay sequence; the session can be reused afterward.
        await db.rollback()
        try:
            invoice, generation = await _attempt_invoice(
                customer_id, base_key, org_id, created_gte,
                int(last_auto_credit.replace(tzinfo=timezone.utc).timestamp())
                if last_auto_credit else None, db=db, payment_method=payment_method)
            key = _idempotency_key(org_id, amount_micro, crossing, payment_method,
                                   generation)
            if invoice is None:
                invoice = await _sdk(
                    stripe_adapter.INVOICE_CREATE,
                    customer=customer_id,
                    currency="usd",  # the Stripe account's default is AUD
                    collection_method="charge_automatically",
                    auto_advance=False,
                    default_payment_method=payment_method,
                    pending_invoice_items_behavior="exclude",
                    discounts="",  # a customer-level coupon must not change the refill amount
                    description=f"Prepaid API call balance for {org_name}",
                    metadata={"treg_org_id": str(org_id), "treg_kind": "topup", "treg_auto": "1",
                              "treg_attempt": key, "treg_attempt_base": base_key,
                              "treg_attempt_generation": str(generation)},
                    idempotency_key=f"{key}:invoice",
                )
            invoice_id = invoice["id"]
            status = invoice.get("status")
            if status == "draft":
                if not await _attempt_has_item(invoice_id, key):
                    await _sdk(stripe_adapter.INVOICE_ITEM_CREATE,
                               customer=customer_id, invoice=invoice_id, amount=cents,
                               currency="usd", description="treg balance top-up", discountable=False,
                               metadata={"treg_attempt": key},
                               idempotency_key=f"{key}:item")
                invoice = await _sdk(stripe_adapter.INVOICE_FINALIZE, invoice=invoice_id,
                                     auto_advance=False, idempotency_key=f"{key}:finalize")
                status = invoice.get("status")
            if status == "open":
                invoice = await _sdk(stripe_adapter.INVOICE_RETRIEVE, id=invoice_id)
                if invoice.get("status") == "open":
                    existing_pi = await _invoice_payment_intent(invoice)
                    if existing_pi:
                        previous_payment = await _sdk(stripe_adapter.PAYMENT_INTENT_RETRIEVE,
                                                      id=existing_pi)
                        previous_status = previous_payment.get("status")
                        if previous_status in ("requires_payment_method", "canceled"):
                            await _stop_failed_invoice(invoice_id, key)
                            return await _record_failure(db, org_id, "card_declined")
                        if previous_status != "requires_confirmation":
                            # Finalizing creates an unconfirmed PaymentIntent before any pay call.
                            # Once it has moved beyond that state, a prior payment may still settle.
                            return await _record_unconfirmed(
                                db, org_id, invoice_id, f"payment_{previous_status}")
                    paid = await _sdk(stripe_adapter.INVOICE_PAY, invoice=invoice_id,
                                      off_session=True, payment_method=payment_method,
                                      idempotency_key=f"{key}:pay")
                elif invoice.get("status") == "void":
                    return await _record_failure(db, org_id, "invoice_void")
                else:
                    paid = invoice
            elif status == "paid":
                paid = invoice
            else:
                raise ValueError(f"unresolved auto top-up invoice status: {status}")
            pi_id = await _invoice_payment_intent(paid)
            if not pi_id:
                raise ValueError("paid auto top-up invoice has no PaymentIntent")
            pi = await _sdk(stripe_adapter.PAYMENT_INTENT_RETRIEVE, id=pi_id)
        except stripe_adapter.CardError as e:
            code = (getattr(e, "code", "") or (e.error.code if getattr(e, "error", None) else "") or "")
            # The declined PaymentIntent hangs off the error as a StripeObject (not a dict), so reach
            # for it defensively — a missing id must not turn a decline into a crash.
            pi_id = ""
            err = getattr(e, "error", None)
            declined = getattr(err, "payment_intent", None) if err is not None else None
            if declined is not None:
                pi_id = str(declined["id"]) if "id" in declined else ""
            if code in AUTH_REQUIRED_CODES:
                return await _pause_for_auth(db, org_id, invoice_id, pi_id)
            await _stop_failed_invoice(invoice_id, key)
            return await _record_failure(db, org_id, code or "card_declined")
        except Exception as e:  # noqa: BLE001 — a Stripe outage must not surface as a 500 elsewhere
            if (isinstance(e, stripe_adapter.InvalidRequestError)
                    and e.param in ("payment_method", "default_payment_method")):
                # Stripe refused the saved card itself (usually removed from the customer through
                # the portal), so nothing was charged. A new card is the fix and re-arms from
                # `no_card`. `key` exists only once `invoice_id` does.
                if invoice_id:
                    await _stop_failed_invoice(invoice_id, key)
                return await _card_gone(db, org_id, payment_method)
            # A timeout can arrive after Stripe charged. Keep the same attempt key and the invoice
            # available for the next lookup; auto_advance=False prevents autonomous collection.
            if getattr(e, "http_status", None) == 409:
                # Stripe is still running another request on this invoice. Nothing was declined,
                # so it is not a failed attempt; the cooldown stamp makes the next lookup wait.
                log.warning("billing: auto top-up for org %s invoice %s overlapped another "
                            "Stripe request", org_id, getattr(e, "invoice_id", invoice_id) or "unknown")
                return {"ok": False, "reason": "stripe_busy"}
            return await _record_unconfirmed(
                db, org_id, getattr(e, "invoice_id", invoice_id), type(e).__name__)

        if (paid.get("status") != "paid" or paid.get("amount_paid") != cents
                or pi.get("status") != "succeeded" or pi.get("currency") != "usd"
                or pi.get("amount_received") != cents):
            # An unfinished payment is not money we can credit. A confirmed 3DS request needs a
            # manual Checkout payment; an uncertain payment must remain untouched.
            if pi.get("status") == "requires_action":
                return await _pause_for_auth(db, org_id, invoice_id, pi_id)
            if pi.get("status") in ("processing", "requires_confirmation", "requires_capture") or (
                    pi.get("status") == "succeeded" and paid.get("status") == "open"):
                return await _record_unconfirmed(
                    db, org_id, invoice_id, f"payment_{pi.get('status')}")
            await _stop_failed_invoice(invoice_id, key)
            return await _record_failure(db, org_id, f"status_{pi.get('status')}")

        # Credit here rather than waiting for the webhook: unlike a browser redirect, this succeeded
        # status came back on OUR request to Stripe, so it is server-authoritative. The webhook's
        # later delivery of the same PaymentIntent id is a no-op (ledger.topup is idempotent on it).
        # topup only STAGES - the commit below lands credit + failure-counter reset in one
        # transaction.
        credited = cents_to_micro(pi["amount_received"])
        already = (await db.execute(
            select(CreditBlock.id).where(CreditBlock.stripe_payment_intent == pi["id"])
        )).first() is not None
        await ledger.topup(db, org_id, credited, pi["id"], meta={"auto": True, "source": "stripe"})
        org = await db.get(Org, org_id)
        org.autotopup_failures = 0
        # A pause recorded while this attempt was paying is resolved by this payment. Blanking the
        # reason without re-arming would leave the policy off with no reason, like a deliberate off.
        _arm_if_waiting_for_card(org, card_proven=True, paid=True, auto_paid=True)
        org_slug = org.slug
        db.add(org)
        await db.commit()
        if not already and (to or analytics.enabled()):
            balance_after = await ledger.balance_of(db, org_id)
            await db.rollback()  # release the read connection before sending the receipt
            if to:
                await email_mod.send_topup_receipt(to, org_name, credited, balance_after, auto=True)
            # The only emitter for an inline auto credit: the webhook finds it credited.
            _capture_topup_completed(to, org_id, org_slug, {"amount_micro": credited, "auto": True,
                                                            "balance_after_micro": balance_after})
        return {"ok": True, "payment_intent": pi["id"], "amount_micro": credited}


async def _card_gone(db: AsyncSession, org_id: int, payment_method: str) -> dict:
    """The saved card can no longer be charged. Forget it and wait for a new one, which re-arms.

    Both the card and the switch may have changed while Stripe was answering: a card saved since
    is already the fix, and a deliberate off must stay one that no future card can undo."""
    log.warning("billing: auto top-up card for org %s is no longer usable", org_id)
    org = await db.get(Org, org_id)
    if org is None or org.stripe_default_pm != payment_method:
        return {"ok": False, "reason": "no_card"}
    org.stripe_default_pm = None
    org.autotopup_failures = 0
    if org.autotopup_enabled:
        await _disable_autotopup(db, org, "no_card")
    else:
        db.add(org)
        await db.commit()
    return {"ok": False, "reason": "no_card"}


async def _record_failure(db: AsyncSession, org_id: int, reason: str) -> dict:
    """Count a failed or unconfirmed attempt and disable auto-top-up at the limit.

    Consecutive, not cumulative: one confirmed success clears the count. An uncertain result keeps
    its invoice key even while this counter rises, so the next lookup cannot start a fresh charge.
    """
    org = await db.get(Org, org_id)
    if org is None:
        return {"ok": False, "reason": reason}
    org.autotopup_failures = int(org.autotopup_failures or 0) + 1
    db.add(org)
    await db.commit()
    if org.autotopup_failures >= get_settings().autotopup_max_attempts:
        disabled_reason = ("stripe_unconfirmed" if reason == "stripe_unconfirmed"
                           else f"max_attempts:{reason}")
        await _disable_autotopup(db, org, disabled_reason)
        return {"ok": False, "reason": "max_attempts", "last_error": reason}
    return {"ok": False, "reason": reason, "failures": org.autotopup_failures}


# In-flight org ids, so a hot call path that fires this on every request doesn't pile up a task per
# call. The lock inside `attempt_auto_topup` would serialize them anyway — this stops them existing.
_scheduled: set[int] = set()


def maybe_schedule_autotopup(org: Org) -> bool:
    """FIRE AND FORGET a refill check for this org. Returns whether a task was actually started.

    Deliberately synchronous and non-awaitable: the caller is on a request path (phase 3 calls it
    right after `reserve`), and a Stripe round-trip is hundreds of milliseconds that must never land
    in an agent's call latency. The cheap in-memory checks happen here so the common case — auto-top-up
    off, or balance healthy — costs a dict lookup and no task at all.
    """
    if not (configured() and org and org.id and org.autotopup_enabled and org.autotopup_consented_at):
        return False
    if not (org.stripe_customer_id and org.stripe_default_pm):
        return False
    if org.balance_micro >= autotopup_prefs(org)["threshold_micro"]:
        return False
    # The DB re-checks under its lock; this only spares a task per call while the wait runs, which
    # matters because a REFUSED call schedules too.
    last = org.autotopup_last_attempt_at
    if last is not None and (_now() - last).total_seconds() < autotopup_wait_s(org):
        return False
    org_id = int(org.id)
    if org_id in _scheduled:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # no event loop (a sync CLI/test context) — nothing to schedule onto
    _scheduled.add(org_id)
    loop.create_task(_run_autotopup(org_id))
    return True


async def _run_autotopup(org_id: int) -> None:
    """The scheduled task's body: its OWN session, because the request that scheduled it will have
    closed its session (and committed or rolled back) long before this runs."""
    from ..infra.db import session_maker
    try:
        async with session_maker() as db:
            result = await attempt_auto_topup(db, org_id)
        if not result.get("ok") and result.get("reason") not in (
                "above_threshold", "cooldown", "disabled", "no_consent", "no_card", "not_configured",
                "stripe_busy"):  # stripe_busy already logged its warning
            log.info("billing: auto top-up for org %s did not fund: %s", org_id, result)
    except Exception as e:  # noqa: BLE001 — a background task's exception must not vanish silently
        log.warning("billing: auto top-up task for org %s crashed: %s", org_id, e)
    finally:
        _scheduled.discard(org_id)


def verify_event(payload: bytes, signature: str) -> dict:
    """Verify a Stripe webhook signature and return the parsed event. Raises `ValueError` on a bad
    signature or payload.

    Uses the SDK's `verify_header` rather than our own HMAC: it is the code Stripe maintains, and it
    already handles the timestamp tolerance that stops a captured delivery being replayed, plus the
    several-signatures case during secret rotation.

    Deliberately NOT `construct_event`, which additionally deserializes into the SDK's typed Event
    model and raises on an envelope it doesn't recognize. Authenticity and shape are separate
    questions: a genuine event of a type this SDK version predates must still be accepted (and then
    ignored by the dispatcher), not rejected as forged.
    """
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise ValueError("no webhook secret configured")
    return stripe_adapter.verify_event(payload, signature, secret)


def _org_id_from(obj: dict) -> int | None:
    meta = obj.get("metadata") or {}
    raw = meta.get("treg_org_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def handle_webhook_event(db: AsyncSession, event: dict) -> dict:
    """Dispatch ONE verified Stripe event. Returns a small dict for the response and the logs.

    Everything here must be safe to run twice: Stripe delivers at least once, retries for days, and
    the CLI's `stripe events resend` exists. Idempotency comes from `ledger.topup` keying on the
    PaymentIntent id, which is why both `checkout.session.completed` and `payment_intent.succeeded`
    can credit the same purchase without doubling it — whichever arrives first does the work.
    """
    kind = event.get("type") or ""
    obj = ((event.get("data") or {}).get("object") or {})
    if (obj.get("metadata") or {}).get("treg_kind") == "enterprise_sso":
        return await handle_event(db, event)
    if kind == "checkout.session.completed":
        return await _on_checkout_completed(db, obj)
    if kind == "payment_intent.succeeded":
        return await _on_payment_succeeded(db, obj)
    if kind == "payment_intent.payment_failed":
        return await _on_payment_failed(db, obj)
    if kind == "setup_intent.succeeded":
        return await _on_setup_succeeded(db, obj)
    if kind in ("charge.dispute.created", "charge.refunded"):
        return await _on_payment_reversed(db, obj, kind)
    # Everything else acknowledged and dropped — deliberately, not by omission. `invoice_creation` on
    # the top-up Checkout makes Stripe emit `invoice.created` / `invoice.paid` for every purchase, and
    # crediting on those too would be a second door onto the same money. The invoice is a document; the
    # PaymentIntent is the payment.
    return {"handled": False, "type": kind}


def _capture_topup_completed(to: str | None, org_id: int, org_slug: str, props: dict) -> None:
    """One `topup_completed` shape for the webhook credit and the inline auto-top-up credit."""
    analytics.capture(to or f"org:{org_id}", "topup_completed",
                      {"entry_surface": analytics.funnel_surface(""),
                       "checkout_source": analytics.funnel_surface(""),
                       "bonus_micro": 0, "bonus_pct": 0, **props,
                       "amount_usd": props["amount_micro"] / 1_000_000,  # display-only, never computed against
                       "org": org_slug},
                      groups={"team": org_slug})


async def _credit(db: AsyncSession, org_id: int, amount_micro: int, pi_id: str, *, auto: bool,
                  fingerprint: str | None = None, attribution: dict | None = None) -> dict:
    """Credit a paid PaymentIntent to the org's balance, once. Emails a receipt only when this
    delivery is the one that actually moved money — a redelivery must not re-notify.

    `fingerprint` is the paying card's Stripe fingerprint, used only by the referral abuse checks;
    None is always safe (it costs a gate, not a payout).
    """
    # "Has this PaymentIntent already been credited?" is asked BEFORE the credit rather than inferred
    # from a balance change afterwards — a concurrent reserve moves the balance too, and mistaking that
    # for a fresh credit would email a receipt on every webhook redelivery.
    attribution = {key: analytics.funnel_surface((attribution or {}).get(key, ""))
                   for key in ("entry_surface", "checkout_source")}
    already = (await db.execute(
        select(CreditBlock.id).where(CreditBlock.stripe_payment_intent == pi_id)
    )).first() is not None
    block = await ledger.topup(db, org_id, amount_micro, pi_id,
                              meta={"auto": auto, "source": "stripe", **attribution})
    block_id = block.id  # captured now: a later rollback (the ad-conversion except below) expires
                        # every object this session is tracking, `block` included, and reading an
                        # expired attribute outside an awaited call raises MissingGreenlet.
    # THE durability line, and the credit's ONLY commit (ledger.topup stages): committed before
    # anything else in this handler can fail - every later rollback (bonus, adsconv, qualify,
    # sweep) rolls back to here.
    await db.commit()
    fresh = not already
    bonus_micro, bonus_pct = 0, 0
    if fresh and not auto:
        # The tiered bonus on a MANUAL top-up, as its own promotional block: it burns first and is
        # never refundable, so it must not inflate the purchased (refundable) block above. `fresh` is
        # the idempotency guard — a webhook redelivery finds `already` and grants nothing. Swallowed
        # like the referral qualification below: a failure here must not 500 the handler and make
        # Stripe retry a payment that has already credited.
        bonus_micro, bonus_pct = bonus_for_topup(amount_micro)
        if bonus_micro > 0:
            try:
                await ledger.grant(db, org_id, amount_micro=bonus_micro, kind="bonus", once=False,
                                   meta={"source": "topup_bonus", "payment_intent": pi_id,
                                         "pct": bonus_pct, "topup_block_id": block_id})
                await db.commit()
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                bonus_micro, bonus_pct = 0, 0
                log.warning("billing: bonus grant failed for org %s on %s: %s", org_id, pi_id, e)
    after = await ledger.balance_of(db, org_id)
    if fresh:
        org = await db.get(Org, org_id)
        if org is not None and (org.autotopup_failures or org.autotopup_disabled_reason):
            # A payment that went through clears the failure history: whatever was wrong with the card
            # isn't wrong now. A confirmed automatic charge also ends an unconfirmed pause; otherwise
            # the disabled FLAG is left to `_set_default_pm`'s rules.
            org.autotopup_failures = 0
            if auto:
                _arm_if_waiting_for_card(org, auto_paid=True)
            db.add(org)
            await db.commit()
        to = await owner_email(db, org_id)
        if to:
            await email_mod.send_topup_receipt(to, (org.name or org.slug) if org else "", amount_micro,
                                              after, auto=auto, bonus_micro=bonus_micro)
        # Rides `fresh` like the receipt above, so a webhook redelivery re-emits nothing. The webhook
        # is org-scoped, so the owner's email is the best available person (the payer may differ);
        # the `team` group is what makes org-level revenue exact. capture() never raises — an
        # exception here would 500 the webhook and make Stripe retry an already-credited payment.
        _capture_topup_completed(to, org_id, org.slug if org else "",
                                 {**attribution, "amount_micro": amount_micro, "auto": auto,
                                  "balance_after_micro": after, "bonus_micro": bonus_micro,
                                  "bonus_pct": bonus_pct})
        # First top-up only: `fresh` is already the "this delivery moved money" branch, and the
        # outbox's unique (org_id, action) makes a second top-up a silent no-op. We measure becoming
        # a payer, not revenue — value-based bidding needs volume treg does not have yet. Guarded like
        # analytics.capture above: an exception here must not 500 the webhook and make Stripe retry an
        # already-credited payment.
        if org is not None:
            try:
                await adsconv.queue(db, org, adsconv.ACTION_PAID,
                                    value_usd_micro=amount_micro, dedupe_key=pi_id)
                await db.commit()
            except Exception as e:  # noqa: BLE001 — see comment above
                await db.rollback()
                log.warning("billing: could not queue ad conversion for org %s: %s", org_id, e)
        # A paid top-up is the referral program's qualifying event, so this `fresh` branch is the
        # only correct place to ask: it is the one point that knows money genuinely moved for the
        # first time. Swallowed for the same reason as the two above — a raise here would 500 the
        # handler and make Stripe retry a payment that has already credited, and a bonus is never
        # worth double-charging someone's card. Rolled back for the same reason too: `qualify`
        # commits internally, so a failure part-way can leave the session pending-rollback and
        # break `_set_default_pm` below.
        try:
            await referrals.qualify(db, org_id=org_id, payment_intent=pi_id,
                                    amount_micro=amount_micro, fingerprint=fingerprint)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            log.warning("billing: referral qualification failed for org %s: %s", org_id, e)
    # Pay out whatever has cleared its hold. Lazy and caller-paid, exactly like ledger's stale-hold
    # reaper: there is no scheduler in treg, and every top-up anywhere is a fine clock to advance a
    # queue this small. `sweep` never raises, but the belt stays on — this is a money webhook.
    try:
        await referrals.sweep(db)
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        log.warning("billing: referral sweep failed: %s", e)
    return {"handled": True, "credited": fresh, "block_id": block_id,
            "amount_micro": amount_micro, "balance_micro": after}


async def _on_checkout_completed(db: AsyncSession, session: dict) -> dict:
    """A hosted Checkout finished. For `mode=payment` this is the manual top-up landing; for
    `mode=setup` it is a card being saved for auto-top-up."""
    org_id = _org_id_from(session)
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    if session.get("mode") == "setup":
        si = session.get("setup_intent")
        si_id = si if isinstance(si, str) else (si or {}).get("id")
        if si_id:
            try:
                si_obj = await _sdk(stripe_adapter.SETUP_INTENT_RETRIEVE, id=si_id)
            except Exception as e:  # noqa: BLE001
                log.warning("billing: could not retrieve SetupIntent %s: %s", si_id, e)
                return {"handled": False, "reason": "setup_intent unreadable"}
            return await _on_setup_succeeded(db, si_obj)
        return {"handled": False, "reason": "no setup_intent on session"}
    if session.get("payment_status") != "paid":
        return {"handled": False, "reason": f"payment_status={session.get('payment_status')}"}
    pi = session.get("payment_intent")
    pi_id = pi if isinstance(pi, str) else (pi or {}).get("id")
    if not pi_id:
        return {"handled": False, "reason": "no payment_intent on session"}
    # `amount_total` is what Stripe actually collected, in cents. We do NOT trust the session's
    # metadata for the amount — the charged number is the only one allowed to become balance.
    amount_micro = cents_to_micro(session.get("amount_total") or 0)
    if amount_micro <= 0:
        return {"handled": False, "reason": "zero amount"}
    # Read the card BEFORE crediting: `_credit` is where a referral qualifies, and the fingerprint is
    # the abuse gate that stops one card claiming a bounty under a second email.
    pm_id, fingerprint = await _pm_and_fingerprint(pi_id)
    result = await _credit(db, org_id, amount_micro, pi_id, auto=False, fingerprint=fingerprint,
                           attribution=session.get("metadata") or {})
    # The Checkout saved the card (setup_future_usage); remember it so auto-top-up can be armed
    # without asking for a second card entry.
    await _set_default_pm(db, org_id, pm_id, paid=True)
    return result


async def _on_payment_succeeded(db: AsyncSession, pi: dict) -> dict:
    """A PaymentIntent succeeded. Fires for the manual Checkout purchase AND the off-session
    auto-charge; `treg_kind` on the metadata is what tells our top-ups apart from anything else the
    account does (the landing-page demo charges, for one)."""
    meta = pi.get("metadata") or {}
    if not meta and pi.get("id"):
        meta = await _invoice_metadata_for_known_customer(db, pi)
    if meta.get("treg_kind") != "topup":
        return {"handled": False, "reason": "not a treg top-up"}
    org_id = _org_id_from({"metadata": meta})
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    amount_micro = cents_to_micro(pi.get("amount_received") or pi.get("amount") or 0)
    if amount_micro <= 0:
        return {"handled": False, "reason": "zero amount"}
    result = await _credit(db, org_id, amount_micro, pi["id"], auto=meta.get("treg_auto") == "1", attribution=meta)
    pm = pi.get("payment_method")
    await _set_default_pm(db, org_id, pm if isinstance(pm, str) else (pm or {}).get("id"), paid=True)
    return result


async def _on_payment_reversed(db: AsyncSession, charge_or_dispute: dict, kind: str) -> dict:
    """A funded payment was disputed or refunded. Cancels any referral bonus it earned that has not
    landed yet — and does NOTHING to the balance itself.

    That split is deliberate and it is the reason the hold window exists. treg has never reversed a
    top-up automatically: `domain/money` has no path that drives a balance negative, blocks burn
    promotional-first precisely to keep the disputable pool small, and refund policy is a human
    decision. What IS safe to automate is not paying a third party a bounty funded by money that
    just went away. A bonus already granted is logged for a human instead (see
    `referrals.reject_for_payment`); referral credit burns first, so by then it is usually spent.

    Both event shapes carry the PaymentIntent: a Charge has `payment_intent`, and a Dispute has it
    directly too (as well as `charge`). We key on it because it is what `CreditBlock` and
    `Referral` already store.
    """
    pi = charge_or_dispute.get("payment_intent")
    pi_id = pi if isinstance(pi, str) else (pi or {}).get("id")
    if not pi_id:
        return {"handled": False, "reason": "no payment_intent on event"}
    try:
        cancelled = await referrals.reject_for_payment(db, pi_id, kind)
    except Exception as e:  # noqa: BLE001 — a webhook must not 500 over a bonus
        log.warning("billing: referral clawback failed for %s: %s", pi_id, e)
        return {"handled": False, "reason": "clawback failed"}
    if cancelled:
        log.warning("billing: %s on %s cancelled %s pending referral bonus(es)", kind, pi_id, cancelled)
    # The top-up bonus is in the same position as an already-granted referral bonus: it burns first,
    # so by now it is usually spent, and reversing it would need the balance-reducing path the ledger
    # deliberately does not have. Logged for a human, with the unspent remainder so they can decide.
    bonus_rows = (await db.execute(
        select(CreditBlock.id, CreditBlock.org_id, CreditBlock.amount_micro, CreditBlock.remaining_micro)
        .join(LedgerEntry, LedgerEntry.block_id == CreditBlock.id)
        .where(CreditBlock.kind == "bonus", LedgerEntry.kind == "grant")
    )).all()
    bonus_flagged = 0
    for bid, oid, amt, rem in bonus_rows:
        meta_row = (await db.execute(
            select(LedgerEntry.meta).where(LedgerEntry.block_id == bid, LedgerEntry.kind == "grant")
        )).first()
        meta = (meta_row[0] if meta_row else None) or {}
        if isinstance(meta, dict) and meta.get("payment_intent") == pi_id:
            bonus_flagged += 1
            log.warning("billing: %s on %s — org %s holds top-up bonus block %s (%s micro-USD, %s "
                        "unspent); not reversed, needs a human", kind, pi_id, oid, bid, amt, rem)
    return {"handled": True, "type": kind, "referrals_cancelled": cancelled,
            "bonus_blocks_flagged": bonus_flagged}


async def _on_payment_failed(db: AsyncSession, pi: dict) -> dict:
    """A top-up PaymentIntent failed. Only the AUTOMATIC ones need bookkeeping: a human who saw a
    decline on the hosted page can just try again, but an unattended charge that keeps failing has to
    stop by itself."""
    meta = pi.get("metadata") or {}
    if not meta and pi.get("id"):
        meta = await _invoice_metadata_for_known_customer(db, pi)
    if meta.get("treg_kind") != "topup":
        return {"handled": False, "reason": "not a treg top-up"}
    org_id = _org_id_from({"metadata": meta})
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    if meta.get("treg_auto") != "1":
        return {"handled": True, "auto": False}  # manual decline — nothing to disable
    code = ((pi.get("last_payment_error") or {}).get("code")) or "card_declined"
    if code in AUTH_REQUIRED_CODES:
        org = await db.get(Org, org_id)
        if org is None:
            return {"handled": False, "reason": "no org"}
        if meta.get("treg_attempt"):
            # This invoice-made PI has no useful metadata of its own. A delayed failure event for
            # an already-void invoice must not switch auto top-up off after a manual recovery.
            await db.rollback()
            matches = await _sdk(stripe_adapter.INVOICE_PAYMENT_LIST,
                                 payment={"type": "payment_intent", "payment_intent": pi["id"]},
                                 limit=1)
            invoice = (matches.get("data") or [{}])[0].get("invoice")
            invoice_id = invoice if isinstance(invoice, str) else (invoice or {}).get("id")
            if not invoice_id:
                return {"handled": False, "reason": "invoice not found"}
            current = await _sdk(stripe_adapter.INVOICE_RETRIEVE, id=invoice_id)
            if current.get("status") == "void":
                return {"handled": True, "disabled": False}
            result = await _pause_for_auth(db, org_id, invoice_id, pi["id"])
            return {"handled": True, "disabled": result["reason"]}
        await _disable_autotopup(db, org, "authentication_required", recovery_pi=pi.get("id") or "")
        return {"handled": True, "disabled": "authentication_required"}
    return {"handled": True, **await _record_failure(db, org_id, code)}


async def _on_setup_succeeded(db: AsyncSession, si: dict) -> dict:
    """A card was saved (SetupIntent). Store the PaymentMethod id so auto-top-up has something to
    charge, and clear a `no_card`-shaped disable — the reason it was off is now gone."""
    org_id = _org_id_from(si)
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    pm = si.get("payment_method")
    pm_id = pm if isinstance(pm, str) else (pm or {}).get("id")
    # Arming a consented-and-waiting org happens inside `_set_default_pm` (shared with the payment
    # path). A disable for any reason other than `no_card` (a decline, 3DS) stays off there too.
    changed = await _set_default_pm(db, org_id, pm_id)
    return {"handled": True, "payment_method_saved": bool(pm_id), "changed": changed}


async def billing_state(db: AsyncSession, org: Org) -> dict:
    """Everything the dashboard/CLI needs to render billing in one round-trip: is Stripe on, is there
    a customer and a card, what the auto-top-up policy is, and how much of the monthly cap the
    automatic charges have used so far."""
    prefs = autotopup_prefs(org)
    s = get_settings()
    spent = await monthly_autotopup_spend(db, org.id) if org.id else 0
    return {
        "org_id": org.id,
        "configured": configured(),
        "balance_micro": int(org.balance_micro or 0),
        "balance_usd": ledger.usd(int(org.balance_micro or 0)),
        "customer": bool(org.stripe_customer_id),
        "card_on_file": bool(org.stripe_default_pm),
        # Whether the hosted portal can be opened at all. It hangs off the Stripe customer, which only
        # exists once someone has paid — so the button stays hidden rather than 422-ing a new team.
        "portal": configured() and bool(org.stripe_customer_id),
        "topup": {"min_usd": s.topup_min_usd, "max_usd": TOPUP_MAX_USD,
                  # Per-org: one preset above the last manual top-up, capped (see next_default_usd).
                  "default_usd": await next_default_usd(db, org.id),
                  "presets": list(s.topup_presets),
                  # {usd: percent}; JSON turns the keys into strings — the UI compares numerically.
                  "bonus_tiers": {int(k): int(v) for k, v in (s.topup_bonus_tiers or {}).items()}},
        "autotopup": {
            "enabled": bool(org.autotopup_enabled),
            "consented_at": org.autotopup_consented_at.isoformat() if org.autotopup_consented_at else None,
            "threshold_micro": prefs["threshold_micro"],
            "threshold_usd": ledger.usd(prefs["threshold_micro"]),
            "amount_micro": prefs["amount_micro"],
            "amount_usd": ledger.usd(prefs["amount_micro"]),
            "monthly_cap_micro": prefs["monthly_cap_micro"],
            "monthly_cap_usd": ledger.usd(prefs["monthly_cap_micro"]),
            "month_spend_micro": spent,
            "month_spend_usd": ledger.usd(spent),
            "cap_reached": autotopup_cap_reached(prefs, spent),
            "disabled_reason": org.autotopup_disabled_reason,
            "failures": int(org.autotopup_failures or 0),
            "last_attempt_at": org.autotopup_last_attempt_at.isoformat() if org.autotopup_last_attempt_at else None,
            "recovery_payment_intent": org.autotopup_recovery_pi,
            "cooldown_s": s.autotopup_cooldown_s,
            "per_hour": prefs["per_hour"],
            "wait_s": autotopup_wait_s(org),
            "max_attempts": s.autotopup_max_attempts,
        },
    }


async def set_autotopup(
    db: AsyncSession, org: Org, *, enabled: bool, consent: bool,
    threshold_usd=None, amount_usd=None, monthly_cap_usd=None, per_hour=None,
) -> None:
    """Store the org's auto-top-up preferences and its consent timestamp. Commits.

    Enabling REQUIRES `consent` on this very request when there is no mandate on file: the timestamp
    has to correspond to a specific human agreeing to a specific threshold and amount, so it is
    stamped here, next to the numbers it agreed to — not inferred later from the fact that the feature
    is on.
    """
    if threshold_usd is not None:
        org.autotopup_threshold_micro = usd_to_micro(validate_threshold_usd(threshold_usd))
    if amount_usd is not None:
        org.autotopup_amount_micro = usd_to_micro(validate_topup_usd(amount_usd))
    if monthly_cap_usd is not None:
        # 0 clears a chosen cap back to the default (`autotopup_prefs`).
        org.autotopup_monthly_cap_micro = (
            0 if monthly_cap_usd == 0 else usd_to_micro(validate_topup_usd(monthly_cap_usd)))
    if per_hour is not None:
        org.autotopup_max_per_hour = validate_per_hour(per_hour)
    prefs = autotopup_prefs(org)
    if prefs["monthly_cap_micro"] < prefs["amount_micro"]:
        raise TopupRejected(f"the monthly limit (${ledger.usd(prefs['monthly_cap_micro']):g}) is below one "
                            f"refill (${ledger.usd(prefs['amount_micro']):g}), so auto top-up could never charge")
    if enabled:
        if consent:
            org.autotopup_consented_at = _now()
        org.autotopup_enabled = bool(org.stripe_default_pm)  # armed only once there's a card to charge
        org.autotopup_disabled_reason = None if org.stripe_default_pm else "no_card"
        org.autotopup_failures = 0
    else:
        org.autotopup_enabled = False
        org.autotopup_disabled_reason = None  # a deliberate off is not a failure — clear the banner
    db.add(org)
    await db.commit()


async def _journey_org(db: AsyncSession, org_id: int) -> Org:
    org = await db.get(Org, org_id)
    if org is None:
        raise RuntimeError("billing organization disappeared")
    return org


async def get_billing_state(org_id: int) -> dict:
    async with _db.session_maker() as db:
        org = await _journey_org(db, org_id)
        state = await billing_state(db, org)
        # Merged HERE rather than inside `billing_state`, so billing keeps its one job (Stripe) and
        # does not grow a second reason to know about referrals. This is the screen where a referred
        # team is already deciding how much to add, so it is the only place the minimum actually
        # changes a decision — see referrals.offer_for_org.
        state["referral_offer"] = await referrals.offer_for_org(db, org.id)
        return state


async def start_topup(
    org_id: int, amount_usd: float | None, *, return_base: str, email: str,
    entry_surface: str = "", checkout_source: str = "",
) -> dict:
    async with _db.session_maker() as db:
        org = await _journey_org(db, org_id)
        amount = amount_usd if amount_usd is not None else await next_default_usd(db, org.id)
        try:
            out = await create_topup_checkout(
                db, org, amount, return_base=return_base, email=email,
                entry_surface=entry_surface, checkout_source=checkout_source)
        except BillingNotConfigured as e:
            raise BillingJourneyError("not_configured", str(e)) from e
        except TopupRejected as e:
            raise BillingJourneyError("rejected", str(e)) from e
        # The one place the actual payer's identity exists — the webhook that later credits the
        # balance is org-scoped, so the started/completed funnel joins on the team group.
        analytics.capture(email, "topup_started",
                          {"amount_usd": amount, "org": org.slug,
                           "entry_surface": analytics.funnel_surface(entry_surface),
                           "checkout_source": analytics.funnel_surface(checkout_source)},
                          groups={"team": org.slug})
        return out


async def configure_autotopup(
    org_id: int, *, enabled: bool, consent: bool, threshold_usd: float | None,
    amount_usd: float | None, monthly_cap_usd: float | None, return_base: str, email: str,
    setup_url: bool = True, per_hour: int | None = None,
) -> dict:
    async with _db.session_maker() as db:
        org = await _journey_org(db, org_id)
        if enabled and not consent and not org.autotopup_consented_at:
            raise BillingJourneyError(
                "rejected",
                "enabling auto top-up requires consent: true (you are authorizing charges to a "
                "saved card without being present)")
        try:
            await set_autotopup(
                db, org, enabled=enabled, consent=consent,
                threshold_usd=threshold_usd, amount_usd=amount_usd,
                monthly_cap_usd=monthly_cap_usd, per_hour=per_hour)
        except (TopupRejected, ValueError) as e:
            raise BillingJourneyError("rejected", str(e)) from e
        state = await billing_state(db, org)
        if enabled and setup_url and not org.stripe_default_pm:
            try:
                state["setup_url"] = (await create_setup_checkout(
                    db, org, return_base=return_base, email=email))["url"]
            except BillingNotConfigured as e:
                raise BillingJourneyError("not_configured", str(e)) from e
            except stripe_adapter.InvalidRequestError as e:
                log.error("billing: Stripe rejected setup checkout: %s", e)
                raise BillingJourneyError("rejected", "payment provider rejected the request") from e
        return state


def _history_cursor(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _read_history_cursor(value: str) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(data, dict) or set(data) - {"t", "i", "o"}:
            raise ValueError("invalid cursor")
        if data.get("t"):
            dt, block_id = data["t"]
            datetime.fromisoformat(dt)
            if not isinstance(block_id, str):
                raise ValueError("invalid cursor")
        if data.get("i") and not isinstance(data["i"], str):
            raise ValueError("invalid cursor")
        if data.get("o") and not isinstance(data["o"], int):
            raise ValueError("invalid cursor")
        return data
    except (ValueError, TypeError, KeyError, IndexError, binascii.Error) as e:
        raise ValueError("invalid billing history cursor") from e


async def get_payment_history(org_id: int, *, limit: int, kind: str = "all", cursor: str = "") -> dict:
    """One newest-first timeline; cursors advance only the source rows actually displayed."""
    if kind not in {"all", "manual", "auto", "subscription"}:
        raise ValueError("invalid billing history filter")
    position = _read_history_cursor(cursor)
    topup_pos = position.get("t")
    topup_before = (datetime.fromisoformat(topup_pos[0]), topup_pos[1]) if topup_pos else None
    stripe_after = position.get("i") or ""
    page_size = max(1, min(limit, 100))
    blocks: list[CreditBlock] = []
    auto: set[str] = set()
    bonus_by_pi: dict[str, int] = {}
    # The database connection closes before any Stripe call. Filtered top-up pages scan in bounded
    # chunks because the auto flag is ledger JSON and must behave identically on SQLite/Postgres.
    async with _db.session_maker() as db:
        org = await _journey_org(db, org_id)
        sso = await db.get(EnterpriseSSO, org_id)
        subscription_id = sso.stripe_subscription_id if sso else None
        if kind != "subscription":
            scan_before = topup_before
            while len(blocks) <= page_size:
                batch, batch_auto, batch_bonus = await _load_payment_rows(
                    db, org, limit=100, before=scan_before)
                if not batch:
                    break
                for block in batch:
                    is_auto = block.id in batch_auto
                    if kind == "all" or (kind == "auto") == is_auto:
                        blocks.append(block)
                        if is_auto:
                            auto.add(block.id)
                        if block.stripe_payment_intent in batch_bonus:
                            bonus_by_pi[block.stripe_payment_intent] = batch_bonus[block.stripe_payment_intent]
                    if len(blocks) > page_size:
                        break
                if len(blocks) > page_size or len(batch) < 100:
                    break
                scan_before = (batch[-1].created_at, batch[-1].id)

    invoices: list[dict] = []
    invoices_ok = True
    invoices_more = False
    if kind in {"all", "subscription"} and subscription_id:
        if org.stripe_customer_id and configured():
            invoices, invoices_ok, invoices_more = await _subscription_invoices(
                org.stripe_customer_id, org_id, subscription_id, page_size + 1, stripe_after)
        else:
            invoices_ok = False
    # Document lookup needs only the top-ups that can appear on this page.
    candidate_blocks = blocks[:page_size]
    docs, stripe_ok = await _payment_documents(org, len(candidate_blocks))
    topups = _assemble_payments(candidate_blocks, auto, bonus_by_pi, docs, stripe_ok)["items"]
    for item, block in zip(topups, candidate_blocks, strict=True):
        item["kind"] = "auto" if block.id in auto else "manual"
        item["_position"] = [block.created_at.isoformat(), block.id]
        item["id"] = block.id
        item["status"] = "paid"
    for item in invoices:
        item["kind"] = "subscription"
    combined = topups + invoices
    combined.sort(key=lambda row: (row.get("created_at") or "", row["id"]), reverse=True)
    shown = combined[:page_size]
    next_pos = dict(position)
    for item in shown:
        if item["kind"] == "subscription":
            next_pos["i"] = item["id"]
        else:
            next_pos["t"] = item["_position"]
    more = len(combined) > page_size or len(blocks) > page_size or invoices_more
    # Stripe errors make the mixed ordering unknowable; offer retry, not a misleading next page.
    if not invoices_ok:
        more = False
    for item in shown:
        item.pop("_position", None)
    return {
        "items": shown,
        "stripe_ok": stripe_ok,
        "subscription_invoices_ok": invoices_ok,
        "next_cursor": _history_cursor(next_pos) if more else None,
    }


async def open_billing_portal(org_id: int, *, return_base: str) -> dict:
    async with _db.session_maker() as db:
        org = await _journey_org(db, org_id)
        try:
            return await create_portal_session(db, org, return_base=return_base)
        except BillingNotConfigured as e:
            raise BillingJourneyError("not_configured", str(e)) from e
        except TopupRejected as e:
            raise BillingJourneyError("rejected", str(e)) from e


async def process_webhook(
    payload_factory: Callable[[], Awaitable[bytes]], signature: str,
) -> dict:
    if not get_settings().stripe_webhook_secret:
        raise BillingJourneyError("webhook_unconfigured")
    payload = await payload_factory()
    try:
        event = verify_event(payload, signature)
    except ValueError as e:
        # Deliberately terse: a signature oracle should not explain itself.
        raise BillingJourneyError("bad_signature", "bad signature") from e
    async with _db.session_maker() as db:
        try:
            return await handle_webhook_event(db, event)
        except Exception as e:
            # 500 tells Stripe to retry, which is what we want for a transient failure — but log
            # loudly: an event that never succeeds is money someone paid and didn't get.
            log.exception("webhook %s failed: %s", event.get("type"), e)
            raise BillingJourneyError("webhook_failed", "webhook handling failed") from e


# Enterprise subscription billing lives alongside the existing Stripe use cases.
PLANS = {"sso", "sso_directory"}
ACTIVE = {"active", "trialing"}
ENTITLED = ACTIVE | {"past_due"}


class EnterpriseBillingError(Exception):
    pass


async def catalog() -> list[dict]:
    settings = get_settings()
    if not settings.stripe_secret_key:
        return []
    plans = []
    for plan in ("sso", "sso_directory"):
        if plan == "sso" and settings.enterprise_sso_bundle_only:
            continue
        price_id = _enterprise_price_id(plan, settings)
        if not price_id:
            continue
        try:
            price = await stripe_adapter.call(stripe_adapter.PRICE_RETRIEVE,
                api_key=settings.stripe_secret_key, id=price_id)
            amount_cents = _validate_enterprise_price(price, price_id=price_id,
                minimum_cents=_enterprise_min_price_cents(plan, settings))
        except (stripe_adapter.StripeError, EnterpriseBillingError):
            log.warning("SSO plan %s has no usable Stripe Price", plan, exc_info=True)
            continue
        plans.append({"id": plan, "price_id": price_id, "usd_monthly_cents": amount_cents,
                      "directory_sync": plan == "sso_directory"})
    return plans


def _enterprise_price_id(plan: str, settings) -> str:
    return (settings.enterprise_sso_bundle_price_id if plan == "sso_directory"
            else settings.enterprise_sso_only_price_id if plan == "sso" else "")


def _enterprise_min_price_cents(plan: str, settings) -> int:
    minimum_usd = (settings.enterprise_sso_bundle_min_price_usd if plan == "sso_directory"
                   else settings.enterprise_sso_only_min_price_usd)
    return max(0, minimum_usd) * 100


def _validate_enterprise_price(price: dict, *, price_id: str, minimum_cents: int = 0) -> int:
    recurring = price.get("recurring") or {}
    amount_cents = price.get("unit_amount")
    if (price.get("id") != price_id or price.get("active") is not True
            or price.get("currency") != "usd" or price.get("billing_scheme") != "per_unit"
            or not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0
            or recurring.get("interval") != "month" or recurring.get("interval_count", 1) != 1
            or recurring.get("usage_type") != "licensed"):
        raise EnterpriseBillingError("SSO Stripe price must be an active flat monthly USD price")
    if amount_cents < minimum_cents:
        raise EnterpriseBillingError("SSO Stripe price is below the configured minimum")
    return amount_cents


async def status(org_id: int, *, include_plans: bool = True) -> dict:
    settings = get_settings()
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        result = {"configured": bool(settings.workos_api_key and settings.workos_client_id),
                "billing_configured": bool(settings.stripe_secret_key and settings.stripe_webhook_secret),
                "plan": row.plan if row else None,
                "subscription_status": row.subscription_status if row else None,
                "sso_ready": bool(row and row.connection_id),
                "directory_ready": bool(row and row.directory_id and row.directory_active),
                "enforced": bool(row and row.enforced),
                "cleanup_pending": bool(row and row.subscription_status in {"canceled", "incomplete_expired"}
                                        and row.workos_org_id and row.workos_cleanup_completed_at is None),
                "cancel_at_period_end": bool(row and row.cancel_at_period_end)}
    # Existing subscribers need management even when Stripe's Price API is unavailable. Only
    # teams shopping for a plan need live sale prices, and no DB connection spans that call.
    result["plans"] = (await catalog() if include_plans and result["subscription_status"] not in ENTITLED
                       else [])
    return result


async def start_checkout(org_id: int, plan: str, *, price_id: str, base: str, email: str) -> dict:
    settings = get_settings()
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        raise EnterpriseBillingError("Stripe billing is not configured")
    if not settings.workos_api_key or not settings.workos_client_id:
        raise EnterpriseBillingError("WorkOS is not configured")
    if (plan not in PLANS or (plan == "sso" and settings.enterprise_sso_bundle_only)
            or not _enterprise_price_id(plan, settings)):
        raise EnterpriseBillingError("plan is not available")
    if price_id != _enterprise_price_id(plan, settings):
        raise EnterpriseBillingError("SSO price changed; refresh the page before checkout")
    now = int(time.time())
    checkout_token = uuid4().hex
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        row = await db.get(EnterpriseSSO, org_id)
        if org is None or org.suspended:
            raise EnterpriseBillingError("team is unavailable")
        if row is not None and row.stripe_subscription_id and row.subscription_status not in {"canceled", "incomplete_expired"}:
            raise EnterpriseBillingError("manage the existing subscription in Billing")
        if (row is not None and row.subscription_status in {"canceled", "incomplete_expired"}
                and row.workos_org_id and row.workos_cleanup_completed_at is None):
            raise EnterpriseBillingError("previous SSO setup is being removed; retry shortly")
        if row is not None and row.checkout_pending_until > now:
            if row.checkout_url:
                return {"url": row.checkout_url}
            raise EnterpriseBillingError("checkout is being prepared; retry in a moment")
        customer_id, name, slug = org.stripe_customer_id, org.name, org.slug
        try:
            if row is None:
                db.add(EnterpriseSSO(org_id=org_id, checkout_token=checkout_token,
                                     checkout_pending_until=now + 1800))
                await db.commit()
            else:
                claimed = await db.execute(update(EnterpriseSSO).where(
                    EnterpriseSSO.org_id == org_id,
                    EnterpriseSSO.checkout_pending_until <= now,
                ).values(checkout_token=checkout_token, checkout_pending_until=now + 1800,
                         checkout_url=None))
                if claimed.rowcount != 1:
                    raise EnterpriseBillingError("checkout is being prepared; retry in a moment")
                await db.commit()
        except (IntegrityError, OperationalError) as exc:
            await db.rollback()
            raise EnterpriseBillingError("checkout is being prepared; retry in a moment") from exc
    try:
        try:
            price = await stripe_adapter.call(stripe_adapter.PRICE_RETRIEVE,
                api_key=settings.stripe_secret_key, id=price_id)
        except stripe_adapter.StripeError as exc:
            raise EnterpriseBillingError("SSO Stripe price is unavailable") from exc
        _validate_enterprise_price(price, price_id=price_id,
            minimum_cents=_enterprise_min_price_cents(plan, settings))
        if not customer_id:
            customer = await stripe_adapter.call(stripe_adapter.CUSTOMER_CREATE,
                api_key=settings.stripe_secret_key, name=name or slug, email=email,
                metadata={"treg_org_id": str(org_id), "treg_org_slug": slug},
                idempotency_key=f"enterprise-customer-{org_id}")
            customer_id = customer["id"]
            async with session_maker() as db:
                org = await db.get(Org, org_id)
                if org is None:
                    raise EnterpriseBillingError("team is unavailable")
                if not org.stripe_customer_id:
                    org.stripe_customer_id = customer_id
                    db.add(org)
                    await db.commit()
                else:
                    customer_id = org.stripe_customer_id
        metadata = {"treg_kind": "enterprise_sso", "treg_org_id": str(org_id), "treg_plan": plan}
        base = base.rstrip("/")
        session = await stripe_adapter.call(stripe_adapter.CHECKOUT_SESSION_CREATE,
            api_key=settings.stripe_secret_key, mode="subscription", customer=customer_id,
            idempotency_key=f"enterprise-checkout-{checkout_token}", expires_at=now + 1800,
            line_items=[{"quantity": 1, "price": price_id}],
            metadata=metadata, subscription_data={"metadata": metadata},
            success_url=f"{base}/app?sso=success#billing",
            cancel_url=f"{base}/app?sso=cancelled#billing")
        async with session_maker() as db:
            row = await db.get(EnterpriseSSO, org_id)
            if row is None or row.checkout_token != checkout_token:
                raise EnterpriseBillingError("checkout reservation changed; retry")
            row.checkout_url = session["url"]
            db.add(row)
            await db.commit()
        return {"url": session["url"]}
    except Exception:
        async with session_maker() as db:
            row = await db.get(EnterpriseSSO, org_id)
            if row is not None and row.checkout_token == checkout_token:
                row.checkout_pending_until = 0
                row.checkout_token = None
                row.checkout_url = None
                db.add(row)
                await db.commit()
        raise


async def cancel_at_period_end(org_id: int) -> dict:
    settings = get_settings()
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        if row is None or not row.stripe_subscription_id or row.subscription_status not in ACTIVE:
            raise EnterpriseBillingError("there is no active SSO subscription")
        subscription_id = row.stripe_subscription_id
    if not settings.stripe_secret_key:
        raise EnterpriseBillingError("Stripe billing is not configured")
    result = await stripe_adapter.schedule_subscription_cancel(
        subscription_id, api_key=settings.stripe_secret_key)
    if result.get("id") != subscription_id or not result.get("cancel_at_period_end"):
        raise EnterpriseBillingError("Stripe did not schedule cancellation")
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        if row is not None and row.stripe_subscription_id == subscription_id:
            row.cancel_at_period_end = True
            db.add(row)
            await db.commit()
    return {"cancel_at_period_end": True}


async def handle_event(db, event: dict) -> dict:
    """Called only after the shared Stripe webhook verifies its signature."""
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return {"handled": False, "type": kind}
    meta = obj.get("metadata") or {}
    if meta.get("treg_kind") != "enterprise_sso":
        return {"handled": False, "type": kind}
    try:
        org_id = int(meta["treg_org_id"])
    except (KeyError, ValueError, TypeError):
        return {"handled": False, "type": kind}
    org = await db.get(Org, org_id)
    if org is None or (obj.get("customer") and obj["customer"] != org.stripe_customer_id):
        return {"handled": False, "type": kind}
    row = await db.get(EnterpriseSSO, org_id)
    try:
        created = int(event.get("created") or 0)
    except (TypeError, ValueError):
        created = 0
    if row and created and row.stripe_event_created and created < row.stripe_event_created:
        return {"handled": True, "type": kind, "stale": True}
    if kind == "checkout.session.completed":
        subscription_id = obj.get("subscription")
        plan = meta.get("treg_plan")
        if obj.get("mode") != "subscription" or not subscription_id or plan not in PLANS:
            return {"handled": False, "type": kind}
        if obj.get("payment_status") not in {"paid", "no_payment_required"}:
            return {"handled": False, "type": kind}
        if (row and row.stripe_subscription_id and row.stripe_subscription_id != subscription_id
                and row.subscription_status not in {"canceled", "incomplete_expired"}):
            raise EnterpriseBillingError("multiple SSO subscriptions for one team")
        if row and row.stripe_subscription_id == subscription_id and row.subscription_status == "canceled":
            return {"handled": True, "type": kind, "stale": True}
        row = row or EnterpriseSSO(org_id=org_id)
        row.plan = plan
        row.stripe_subscription_id = subscription_id
        row.subscription_status = "active"
        row.cancel_at_period_end = False
    elif kind in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        subscription_id = obj.get("id")
        plan = meta.get("treg_plan")
        if not subscription_id or plan not in PLANS:
            return {"handled": False, "type": kind}
        if (row and row.stripe_subscription_id and row.stripe_subscription_id != subscription_id
                and row.subscription_status not in {"canceled", "incomplete_expired"}):
            return {"handled": False, "type": kind}
        if row and row.stripe_subscription_id == subscription_id and row.subscription_status == "canceled":
            return {"handled": True, "type": kind, "stale": True}
        row = row or EnterpriseSSO(org_id=org_id)
        row.plan = plan
        row.stripe_subscription_id = subscription_id
        row.subscription_status = "canceled" if kind.endswith(".deleted") else str(obj.get("status") or "incomplete")
        row.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    else:
        return {"handled": False, "type": kind}
    row.stripe_event_created = created
    if row.stripe_subscription_id and row.subscription_status in ENTITLED:
        row.workos_cleanup_completed_at = None
        row.checkout_token = None
        row.checkout_pending_until = 0
        row.checkout_url = None
    if row.subscription_status not in ENTITLED and row.enforced:
        row.enforced = False
        enterprise_sso._audit(db, org_id, "sso.enforcement_disabled")
    db.add(row)
    if row.subscription_status in {"canceled", "incomplete_expired"}:
        await enterprise_sso.remove_sso_created_memberships(db, org_id)
    await db.commit()
    return {"handled": True, "type": kind}


async def cleanup_canceled_workos(org_id: int) -> dict:
    """Remove billable WorkOS resources after entitlement ends, outside any DB transaction.

    A canceled team's WorkOS organization is dedicated to that treg team. Keep the organization
    for a later purchase, but remove every connection and directory it owns, including resources
    made in Admin Portal before a first sign-in or activation webhook recorded their IDs here.
    """
    import httpx
    from ..infra import workos

    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        if row is None or row.subscription_status not in {"canceled", "incomplete_expired"}:
            return {"org_id": org_id, "skipped": "subscription is not ended"}
        if row.workos_cleanup_completed_at is not None:
            return {"org_id": org_id, "skipped": "already cleaned"}
        organization = row.workos_org_id
        subscription_id = row.stripe_subscription_id
    if organization:
        key = get_settings().workos_api_key
        if not key:
            raise EnterpriseBillingError("WorkOS API key is not configured")
        async with httpx.AsyncClient() as client:
            details = await workos.get_organization(client, key=key, organization=organization)
            if details.get("external_id") != str(org_id):
                raise EnterpriseBillingError("WorkOS organization does not belong to this team")
            directories = await workos.directories_for_organization(client, key=key,
                organization=organization)
            connections = await workos.connections_for_organization(client, key=key,
                organization=organization)
            for kind, rows in (("directory", directories), ("connection", connections)):
                for resource in rows:
                    expected = "directory_" if kind == "directory" else "conn_"
                    if (resource.get("organization_id") != organization or
                            not isinstance(resource.get("id"), str) or
                            not resource["id"].startswith(expected)):
                        raise EnterpriseBillingError("WorkOS resource does not belong to this team")
            for resource in directories:
                await workos.delete_directory(client, key=key, directory=resource["id"])
            for resource in connections:
                await workos.delete_connection(client, key=key, connection=resource["id"])
            if (await workos.directories_for_organization(client, key=key, organization=organization)
                    or await workos.connections_for_organization(client, key=key, organization=organization)):
                raise EnterpriseBillingError("WorkOS resources remain; retry cleanup")
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id, with_for_update=True)
        if (row is None or row.subscription_status not in {"canceled", "incomplete_expired"}
                or row.stripe_subscription_id != subscription_id or row.workos_org_id != organization):
            raise EnterpriseBillingError("SSO subscription changed during cleanup")
        row.connection_id = None
        row.directory_id = None
        row.directory_active = False
        row.workos_cleanup_completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.execute(delete(EnterpriseSSOIdentity).where(EnterpriseSSOIdentity.org_id == org_id))
        await db.execute(delete(EnterpriseDirectoryUser).where(EnterpriseDirectoryUser.org_id == org_id))
        enterprise_sso._audit(db, org_id, "sso.workos_cleanup_completed")
        db.add(row)
        await db.commit()
    return {"org_id": org_id, "directories_deleted": len(directories) if organization else 0,
            "connections_deleted": len(connections) if organization else 0}
