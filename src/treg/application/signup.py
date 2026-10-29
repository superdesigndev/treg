"""Identity provisioning and first-team creation use cases."""

import logging
from datetime import timedelta
from urllib.parse import quote, unquote

from sqlalchemy import delete, inspect as sa_inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import adsconv, analytics, health, policy
from ..config import get_settings
from ..domain import money as ledger
from ..domain import referrals
from ..domain.governance.teams import _make_org_membership, _slugify, reserved_reason
from ..domain.identity.access import _email_domain, _is_blocked_email, _is_machine_email, _norm_email
from ..domain.identity.promotions import claim_signup_promo, promo_inbox, signup_promo_email
from ..infra.db import session_maker
from ..models import AdClickStash, Membership, Org, User
from ..timeutil import utcnow_naive as _utcnow_naive


def reserved_team_names() -> frozenset[str]:
    """Every catalog provider slug (a team may not read as one) and every platform slug, marked "="
    (only the whole name: platforms are ordinary words like `people` or `web`)."""
    from ..domain.catalog import store as catalog_store
    cat = catalog_store.load()
    providers = {e["provider"] for e in cat.endpoints}
    return frozenset(providers | {"=" + p for p in (getattr(cat, "platforms", {}) or {}) if p not in providers})


class SignupError(Exception):
    """A framework-neutral signup refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class MachineIdentityError(Exception):
    """A machine identity reached a human identity-provisioning command."""


class BlockedEmailError(Exception):
    """An address on a blocked email domain reached an identity door."""


class UnverifiedExistingUserError(Exception):
    """SSO cannot attach a verified assertion to an unverified existing account."""


def blocked_email(email: str, door: str) -> bool:
    """The blocklist DECISION for the identity doors, over the pure classifier `_is_blocked_email`:
    True means refuse. One structured line per block, so a burst of refusals is countable from the
    logs — the refusal itself tells the caller nothing, so the log is the only detection. Fails OPEN:
    a classifier error is logged and the door stays open, because a misconfiguration must never break
    a real sign-in. `door` names the endpoint for the log only; it never reaches the caller."""
    log = logging.getLogger("treg.auth")
    try:
        if not _is_blocked_email(email):
            return False
    except Exception as exc:  # noqa: BLE001 - fail open, by design
        log.error("event=blocklist_error door=%s error=%s", door, exc)
        return False
    log.warning("event=signup_blocked_domain door=%s domain=%s", door, _email_domain(email))
    return True


async def find_or_create_user(
    db: AsyncSession, email: str, *, door: str = "login",
    created: set[int] | None = None, verified: bool = False, promo_eligible: bool = True,
    proven: set[int] | None = None, reject_unverified_existing: bool = False,
) -> User:
    """Find a user by email, else register them — the user ONLY, **no auto personal org**. The shared
    core of every identity door (GitHub / Google / email OTP). A brand-new user therefore lands with
    zero teams and is asked to NAME + CREATE their first team (the dashboard's mandatory welcome, or
    `treg org create`) — we never spawn a throwaway personal org they didn't ask for. Their identity
    token is user-scoped, so it works before they have any org (org chosen per-request via X-Treg-Org).
    Only trusted email-proof callers pass verified=True; `proven` collects a user this call verified
    for the first time (see `grant_signup_promo_on_proof`). promo_eligible=False creates the user
    without a signup-credit claim; it never touches an existing user. SSO can refuse to link an
    existing account that has not proved its email. Caller commits."""
    email = _norm_email(email)
    # Machine identities (agents, the published demo token) are minted by an admin and act ONLY by
    # their token. This is the single choke point every identity door shares, so blocking here means
    # no door — GitHub, Google, email OTP, invite sign-in — can hand a human an agent's identity.
    # (The domains are unroutable, so a code could never be delivered anyway; this makes it explicit.)
    if _is_machine_email(email):
        raise MachineIdentityError
    # Same choke point for the domain blocklist, and BEFORE the lookup on purpose: a blocked domain
    # gets no session whether or not it already has a row (sign-in, not just sign-up).
    if blocked_email(email, door):
        raise BlockedEmailError
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, signup_promo_available=promo_eligible)
        db.add(user)
        try:
            await db.flush()  # surfaces the unique-email violation on a concurrent first-login race
        except IntegrityError:
            await db.rollback()  # another worker just created this same new user — reuse theirs
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            if reject_unverified_existing and user.email_verified_at is None:
                raise UnverifiedExistingUserError from None
        else:
            if created is not None:
                created.add(user.id)
    elif reject_unverified_existing and user.email_verified_at is None:
        raise UnverifiedExistingUserError
    if verified and user.email_verified_at is None:
        # Conditional, so of two proofs racing for one unverified user only the one that flips the
        # column is the first proof; the other sees no row and is an ordinary sign-in.
        flipped = await db.execute(
            update(User).where(User.id == user.id, User.email_verified_at.is_(None))
            .values(email_verified_at=_utcnow_naive())
        )
        if flipped.rowcount == 1 and proven is not None:
            proven.add(user.id)
    return user


def track_signup(
    user: User, created: set[int], method: str, entry_surface: str, *, captcha: str = "",
) -> None:
    """Call only after commit. Existing users (including a concurrent signup's loser) emit nothing.
    `captcha` is the Turnstile outcome of the code request, when the deployment checks one."""
    if user.id in created:
        properties = {"signup_method": method, "entry_surface": analytics.funnel_surface(entry_surface)}
        if captcha:
            properties["captcha"] = captcha
        analytics.capture(user.email, "signup_completed", properties)


async def grant_signup_promo_on_proof(user_id: int) -> None:
    """Fund the team a user made before proving their inbox. Call after the proving commit.

    The legacy `POST /users` door makes a team for an unverified user, and team creation is the
    only other claim point, so without this the first proof would leave that team at zero until the
    user made a second one. Runs only on the sign-in that first verifies the user, so a deferred
    claim is not re-asked on every sign-in. With no team yet, the claim waits for their first team.
    """
    async with session_maker() as db:
        org = (await db.execute(
            select(Org).join(Membership, Membership.org_id == Org.id)
            .where(Membership.user_id == user_id, Membership.role == "owner",
                   Org.demo.is_(False), Org.public_demo.is_(False), Org.suspended.is_(False))
            .order_by(Org.created_at, Org.id).limit(1)
        )).scalar_one_or_none()
        if org is not None:
            # The team's signup conversion was queued when it was made.
            await _grant_signup_promo(db, org, user_id=user_id, conversion=False)


async def _grant_signup_promo(db: AsyncSession, org: Org, *, user_id: int, conversion: bool = True) -> None:
    """Queue team attribution and grant at most once to its verified creator.

    The user claim, money block, balance and ledger entry share one transaction. A
    failed grant rolls the claim back too; the already-created team remains usable.
    Unverified and legacy accounts get no automatic credit. Ads remain per-team and
    independent of eligibility. Demo teams never receive real funding or conversions.
    """
    if org is None or org.id is None or org.demo or org.public_demo:
        return
    org_id = org.id
    amount = get_settings().promo_grant_micro
    try:
        claimed = amount > 0 and await _policy_grants(db, org_id, user_id) and await claim_signup_promo(db, user_id)
        try:
            if conversion:
                await adsconv.queue(db, org, adsconv.ACTION_SIGNUP)
        except Exception as exc:  # noqa: BLE001 - marketing cannot take away the signup benefit
            logging.getLogger("treg").warning("ad conversion queue failed for org %s: %s", org_id, exc)
        if claimed:
            await ledger.grant(db, org_id, amount_micro=amount, once=False,
                               meta={"source": "signup", "user_id": user_id})
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - team creation already committed
        await db.rollback()
        logging.getLogger("treg").warning("promo grant failed for org %s: %s", org_id, exc)


async def _policy_grants(db: AsyncSession, org_id: int, user_id: int) -> bool:
    """Ask the `signup_credit` decision point, only for a user who could claim right now.

    The read's transaction ends before the decision so no connection waits on the extension. A
    deferral writes nothing: the user keeps eligibility and is asked again on their next team.
    """
    email = await signup_promo_email(db, user_id)
    await db.commit()
    if email is None:
        return False
    decision = await policy.registry.signup_credit(policy.SignupCreditContext(
        user_id=user_id, org_id=org_id, email=email, canonical_inbox=promo_inbox(email),
    ))
    if decision is policy.CreditDecision.DEFER:
        logging.getLogger("treg").info("event=signup_credit_deferred org=%s user=%s", org_id, user_id)
        return False
    return True


def _ad_attribution_from(raw_cookie: str) -> tuple[str, str, str]:
    """Return (click-id field, click-id, landing), with legacy GCLID-cookie compatibility."""
    if not adsconv.enabled():
        return "", "", ""
    if not raw_cookie:
        return "", "", ""
    decoded = unquote(raw_cookie)
    parts = decoded.split("|")
    # The typed format keeps any "|" inside its landing; only the legacy format is exactly two parts.
    if len(parts) < 2 or (len(parts) > 2 and parts[0] not in ("gclid", "gbraid", "wbraid")):
        return "", "", ""
    first, separator, rest = decoded.partition("|")
    if separator and first in ("gclid", "gbraid", "wbraid"):
        click_id, _, landing = rest.partition("|")
        click_field = first
    else:
        # Old cookies were `CLICK_ID|landing` and always held a GCLID.
        click_field, click_id, landing = "gclid", first, rest
    return click_field, click_id.strip()[:255], landing.strip()[:64]


async def stash_ad_click(email: str, raw_cookie: str) -> None:
    """Remember an observed click for this email; never log this first-party association."""
    try:
        kind, click_id, landing = _ad_attribution_from(raw_cookie)
        if not click_id:
            return
        async with session_maker() as db:
            if db.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            now = _utcnow_naive()
            stmt = insert(AdClickStash).values(
                email=email.strip().lower(), raw_cookie=quote(f"{kind}|{click_id}|{landing}", safe=""),
                created_at=now, updated_at=now,
            )
            await db.execute(stmt.on_conflict_do_update(
                index_elements=["email"],
                set_={"raw_cookie": stmt.excluded.raw_cookie, "updated_at": now},
                where=AdClickStash.updated_at <= now,
            ))
            await db.commit()
    except Exception:  # noqa: BLE001 - the ad click stash is optional; sign-in continues
        # Exception text can contain SQL parameters (email and click id), so omit it too.
        logging.getLogger("treg").warning("ad click stash failed; sign-in continues")


# Google accepts a click conversion for 90 days, and the `treg_ad` cookie lives as long.
AD_CLICK_STASH_TTL = timedelta(days=90)


async def _ad_cookie_for(db: AsyncSession, email: str, raw_cookie: str) -> str:
    if raw_cookie or not adsconv.enabled():
        return raw_cookie
    stash = await db.get(AdClickStash, email.strip().lower())
    if stash is not None and stash.updated_at >= _utcnow_naive() - AD_CLICK_STASH_TTL:
        return stash.raw_cookie
    return ""


async def _forget_ad_click(db: AsyncSession, email: str) -> None:
    """The team now holds the click itself, so the email association has no further use."""
    await db.execute(delete(AdClickStash).where(AdClickStash.email == email.strip().lower()))


async def purge_expired_ad_clicks(*, session_factory=session_maker) -> dict:
    """Delete stash rows past the lookup window; run daily by `treg-worker admin purge-ad-clicks`."""
    async with session_factory() as db:
        result = await db.execute(
            delete(AdClickStash).where(AdClickStash.updated_at < _utcnow_naive() - AD_CLICK_STASH_TTL))
        await db.commit()
    return {"deleted": result.rowcount or 0}


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
        if blocked_email(email, "register"):  # unverified registration still checks the domain
            raise SignupError("blocked_domain")
        if webhook_url and not health.safe_webhook_url(webhook_url):  # SSRF guard on the alert URL
            raise SignupError("unsafe_webhook")
        if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
            raise SignupError("email_exists")
        user = User(email=email)
        db.add(user)
        await db.flush()
        # The default team is named after the email; one that reads as reserved gets a plain slug.
        base = _slugify(email) if not reserved_reason(email, reserved_team_names()) else f"team-{user.id}"
        org, token = await _make_org_membership(
            db, user, name=email, slug_base=base, role="owner", webhook_url=webhook_url,
        )
        click_field, gclid, landing = _ad_attribution_from(
            await _ad_cookie_for(db, user.email, ad_cookie))
        if gclid:
            org.ad_gclid = gclid
            org.ad_click_id_type = click_field
            org.ad_landing = landing or None
            # asyncpg rejects aware datetimes for this TIMESTAMP WITHOUT TIME ZONE column.
            org.ad_click_at = _utcnow_naive()
            db.add(org)
            await _forget_ad_click(db, user.email)
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
        await _grant_signup_promo(db, org, user_id=user.id)
        # Both org-creating doors redeem because both end with a person owning a fresh team.
        await _redeem_referral(db, referral_cookie, user, org)
        return response


async def create_org(
    *, user: User, name: str, ad_cookie: str, utm_cookie: str, referral_cookie: str,
) -> dict:
    async with session_maker() as db:
        # Previously registered identities may still hold tokens, so check this door too.
        if blocked_email(user.email, "create_org"):
            raise SignupError("blocked_domain")
        if not user.is_superadmin and reserved_reason(name, reserved_team_names()):
            raise SignupError("reserved_name")
        click_field, gclid, landing = _ad_attribution_from(
            await _ad_cookie_for(db, user.email, ad_cookie))
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
                await _forget_ad_click(db, user.email)  # inside the loop: a rollback undoes it
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
        await _grant_signup_promo(db, org, user_id=user.id)
        await _redeem_referral(db, referral_cookie, user, org)
        return response
