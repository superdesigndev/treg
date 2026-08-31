"""Identity provisioning and first-team creation use cases."""

import logging
from urllib.parse import unquote

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import adsconv, health, sandbox as demo_sandbox
from ..domain import money as ledger
from ..domain import referrals
from ..domain.governance.teams import _make_org_membership, _slugify
from ..domain.identity.access import _is_machine_email, _norm_email
from ..infra.db import session_maker
from ..models import Org, User
from ..timeutil import utcnow_naive as _utcnow_naive


class SignupError(Exception):
    """A framework-neutral signup refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class MachineIdentityError(Exception):
    """A machine identity reached a human identity-provisioning command."""


async def find_or_create_user(db: AsyncSession, email: str) -> User:
    """Find a user by email, else register them — the user ONLY, **no auto personal org**. The shared
    core of every identity door (GitHub / Google / email OTP). A brand-new user therefore lands with
    zero teams and is asked to NAME + CREATE their first team (the dashboard's mandatory welcome, or
    `treg org create`) — we never spawn a throwaway personal org they didn't ask for. Their identity
    token is user-scoped, so it works before they have any org (org chosen per-request via X-Treg-Org).
    Caller commits."""
    email = _norm_email(email)
    # Machine identities (agents, the published demo token) are minted by an admin and act ONLY by
    # their token. This is the single choke point every identity door shares, so blocking here means
    # no door — GitHub, Google, email OTP, invite sign-in — can hand a human an agent's identity.
    # (The domains are unroutable, so a code could never be delivered anyway; this makes it explicit.)
    if _is_machine_email(email):
        raise MachineIdentityError
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email)
        db.add(user)
        try:
            await db.flush()  # surfaces the unique-email violation on a concurrent first-login race
        except IntegrityError:
            await db.rollback()  # another worker just created this same new user — reuse theirs
            return (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return user


async def _grant_signup_promo(db: AsyncSession, org: Org) -> None:
    """Give a BRAND-NEW org its promotional balance, so an agent's first call needs no key and no card
    (`settings.promo_grant_micro`, $1 by default). Called after the org is committed, from every door
    that creates a real team — `ledger.grant` is idempotent per (org, kind), so a retried signup or a
    second door can't double-grant, and existing orgs are never backfilled.

    Demo/sandbox teams are created elsewhere (demo.py / sandbox.py) and deliberately get nothing: a
    published demo token must not be able to spend real money. A grant failure must not fail the
    signup — the org exists, and it can be topped up — so it is logged, not raised.
    """
    if org is None or org.id is None or org.demo or org.public_demo:
        return
    # Read now: the rollback below expires every object this session tracks, and a lazy attribute
    # load after it is implicit async I/O (MissingGreenlet) - same idiom as money._ClaimedHold.
    org_id = org.id
    try:
        # Queue and grant both only STAGE: adsconv.queue() adds a row inside a SAVEPOINT and
        # ledger.grant() stages the block, balance and entry on this session. The ONE commit below
        # is what lands the event and its conversion together (see adsconv.queue's docstring).
        # The queue-first order is kept for the inner-except rationale, not for commit mechanics.
        # Same door, same once-only guarantee: this function is already the single place a brand-new
        # real team comes into existence.
        try:
            await adsconv.queue(db, org, adsconv.ACTION_SIGNUP)
        except Exception as exc:  # noqa: BLE001 — its OWN guard, deliberately, not the outer one
            # Sharing the outer except would mean an unexpected failure here (anything but the
            # IntegrityError queue() already absorbs) skips the grant entirely and costs the team
            # its $1 promotional credit. A marketing metric must not be able to take away a product
            # benefit: swallow it here so the grant still runs.
            logging.getLogger("treg").warning("ad conversion queue failed for org %s: %s", org_id, exc)
        await ledger.grant(db, org_id)  # stages only; the commit below lands grant + conversion together
        # Unconditional, even when grant returned None (retried signup): the queue no-oped too, so
        # the commit is empty and harmless - simpler than making it conditional.
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — the team is already created; don't 500 the signup over credit
        # End the transaction the failure poisoned so the referral redemption that follows still has
        # a working session. The rollback also expires every object this session tracks, which is
        # why both doors read their response fields BEFORE calling here and why _redeem_referral
        # revives its arguments.
        await db.rollback()
        logging.getLogger("treg").warning("promo grant failed for org %s: %s", org_id, exc)


def _ad_attribution_from(raw_cookie: str) -> tuple[str, str, str]:
    """Return (click-id field, click-id, landing), with legacy GCLID-cookie compatibility."""
    if not adsconv.enabled():
        return "", "", ""
    if not raw_cookie:
        return "", "", ""
    first, separator, rest = unquote(raw_cookie).partition("|")
    if separator and first in ("gclid", "gbraid", "wbraid"):
        click_id, _, landing = rest.partition("|")
        click_field = first
    else:
        # Old cookies were `CLICK_ID|landing` and always held a GCLID.
        click_field, click_id, landing = "gclid", first, rest
    return click_field, click_id.strip()[:255], landing.strip()[:64]


_UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_referrer")


def _utm_attribution_from(raw_cookie: str) -> dict[str, str]:
    """First-touch traffic source from the `treg_utm` cookie (set by web/sitetrack.js):
    `source|medium|campaign|term|content|referring-host`, URL-encoded. Missing/short cookies yield
    fewer fields; anything unparseable yields nothing. Values are capped so a hostile cookie cannot
    bloat the row."""
    if not raw_cookie:
        return {}
    parts = [part.strip()[:100] for part in unquote(raw_cookie).split("|")]
    return {key: value for key, value in zip(_UTM_FIELDS, parts) if value}


def _stamp_utm(org: Org, raw_cookie: str) -> None:
    """Persist the first-touch source on a brand-new team. Independent of the Google-Ads `treg_ad`
    path: a sponsor link or a newsletter has no click id, and this is what lets us count its
    signups. Called from both signup doors, like `_ad_attribution_from`."""
    for key, value in _utm_attribution_from(raw_cookie).items():
        setattr(org, key, value)


async def _redeem_referral(
    db: AsyncSession, raw_cookie: str, user: User, org: Org,
) -> None:
    """Attribute a brand-new team to whoever's link brought them here. Owes nothing yet; the bonus
    is earned at the team's first paid top-up, not at signup (see referrals.py).

    Team creation is the right and only redemption point: `find_or_create_user` deliberately makes
    no org, so this is where a person first becomes a tenant with a balance. It fires on every team
    a user creates, and `referrals.attribute` refuses self-referrals, demo teams, unknown codes, and
    orgs that already carry a referral.

    A referral is a marketing nicety and a signup is not. Nothing here may ever be the reason
    someone cannot make a team.
    """
    org_id = None
    try:
        # A failed promo grant just before this rolled the session back, which expired every object
        # it tracks; revive both before their first attribute read becomes implicit async I/O
        # (MissingGreenlet). No-ops on the happy path.
        for obj in (user, org):
            if sa_inspect(obj).expired:
                await db.refresh(obj)
        org_id = org.id
        code = referrals.normalize_code((raw_cookie or "").strip('"'))
        if code:
            await referrals.attribute(db, user=user, org=org, code=code)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("treg").warning("referral attribution failed for org %s: %s", org_id, exc)


async def register_user(
    *, email: str, webhook_url: str | None, ad_cookie: str, utm_cookie: str, referral_cookie: str,
) -> dict:
    async with session_maker() as db:
        email = _norm_email(email)
        # Open registration predates find_or_create_user, so it needs the same machine-domain block;
        # otherwise a caller could squat an agent address before an admin mints that agent.
        if _is_machine_email(email):
            raise SignupError("machine_identity")
        if webhook_url and not health.safe_webhook_url(webhook_url):  # SSRF guard on the alert URL
            raise SignupError("unsafe_webhook")
        if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
            raise SignupError("email_exists")
        user = User(email=email)
        db.add(user)
        await db.flush()
        org, token = await _make_org_membership(
            db, user, name=email, slug_base=_slugify(email), role="owner", webhook_url=webhook_url,
        )
        click_field, gclid, landing = _ad_attribution_from(ad_cookie)
        if gclid:
            org.ad_gclid = gclid
            org.ad_click_id_type = click_field
            org.ad_landing = landing or None
            # asyncpg rejects aware datetimes for this TIMESTAMP WITHOUT TIME ZONE column.
            org.ad_click_at = _utcnow_naive()
            db.add(org)
        _stamp_utm(org, utm_cookie)
        db.add(org)
        try:
            await db.commit()
        except IntegrityError as exc:
            raise SignupError("email_exists") from exc
        # Read the response now: a failed promo grant below rolls the session back, which expires
        # every tracked object, and a lazy reload after that is implicit async I/O (MissingGreenlet).
        response = {
            "id": user.id,
            "email": user.email,
            "org": org.slug,
            "org_id": org.id,
            "role": "owner",
            "token": token,
        }
        await _grant_signup_promo(db, org)
        # Both org-creating doors redeem because both end with a person owning a fresh team.
        await _redeem_referral(db, referral_cookie, user, org)
        return response


async def create_org(
    *, user: User, name: str, ad_cookie: str, utm_cookie: str, referral_cookie: str,
) -> dict:
    async with session_maker() as db:
        if demo_sandbox.is_sandbox_user(user):  # anonymous sandbox visitors cannot mint real teams
            raise SignupError("sandbox_user")
        click_field, gclid, landing = _ad_attribution_from(ad_cookie)
        # A browser sign-in reaches this door instead of /users, so both doors must read attribution.
        for _ in range(3):  # a concurrent create can claim the slug before commit; retry a fresh lookup
            org, token = await _make_org_membership(
                db, user, name=name, slug_base=_slugify(name), role="owner",
            )
            if gclid:
                org.ad_gclid = gclid
                org.ad_click_id_type = click_field
                org.ad_landing = landing or None
                # asyncpg rejects aware datetimes for this TIMESTAMP WITHOUT TIME ZONE column.
                org.ad_click_at = _utcnow_naive()
                db.add(org)
            _stamp_utm(org, utm_cookie)
            db.add(org)
            try:
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
        else:
            raise SignupError("slug_conflict")
        # Read the response now, before the grant can roll back and expire it (see register_user).
        response = {
            "org": org.slug,
            "org_id": org.id,
            "name": org.name,
            "role": "owner",
            "token": token,
        }
        await _grant_signup_promo(db, org)
        await _redeem_referral(db, referral_cookie, user, org)
        return response
