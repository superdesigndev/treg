"""Authentication use cases and their transaction boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import secrets as _secrets
from typing import Any
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import audit, crypto, email as email_sender, ratestore
from ..application.onboard import demo as demo_seed
from ..infra import db as database
from ..config import get_settings
from ..domain.identity import session as sess
from ..domain.identity import mcp_oauth
from ..domain.identity.access import (
    _is_machine_email,
    _membership_by_token,
    _norm_email,
    _resolve_org,
    _user_from_identity_token,
    _user_from_session,
)
from ..domain.identity.mcp_oauth import (
    _ensure_grant,
    _family_org,
    _issue_refresh,
    _refresh_is_live,
    _revoke_refresh_family,
)
from ..models import Invite, Membership, OAuthClient, OAuthCode, OAuthRefresh, Org, Tool, User
from ..timeutil import as_naive as _as_naive
from ..timeutil import utcnow_naive as _utcnow_naive
from . import signup


CLI_TOKEN_TTL = 30 * 24 * 3600      # identity token lifetime for the CLI
EMAIL_CODE_TTL = 10 * 60  # seconds a code stays valid
MAX_OTP_ATTEMPTS = 5  # invalidate a code after this many wrong guesses (brute-force guard)
OTP_NS = "otp"
OTP_START_NS = "otp_start"
OTP_START_WINDOW_S = 900      # 15 minutes
OTP_START_MAX_PER_EMAIL = 5   # code requests for one inbox per window (caps bombing a single victim)
OTP_START_MAX_PER_IP = 30     # code requests from one IP per window (looser — offices/NAT share an IP)
AUTH_CODE_TTL_S = 300   # a code is redeemed within seconds; five minutes is generous, not a window


# In-memory handshake state for `treg login` (single-instance; short-lived, fine to lose on restart).
# Both carry a created-at so abandoned handshakes (unauthenticated, attacker-chosen keys) are swept
# rather than accumulating forever — the results map holds live 30-day tokens, so it must not leak.
_cli_states: dict[str, tuple[str, datetime]] = {}   # oauth state -> (login_id, created_at)
_cli_results: dict[str, tuple[dict, datetime]] = {}  # login_id -> (result, created_at) — a completed login
# login_id -> (pairing_code, attempts_left, created_at). Created by POST /auth/cli/start; the browser must
# echo the code back at approve time (validated server-side) before a token is issued. This is the phishing
# guard: a login the user didn't start has no matching code, and the poll endpoint carries no code to
# brute-force. The code is shown ONLY in the terminal, never in the /login URL.
_cli_pending: dict[str, tuple[str, int, datetime]] = {}
HANDSHAKE_TTL = 600                  # seconds an abandoned login handshake lingers before eviction
CLI_APPROVE_MAX_TRIES = 8           # wrong pairing-code attempts before a pending login is discarded
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous (no O/0/I/1); matches the CLI's charset


def _prune_handshakes() -> None:
    cutoff = _utcnow_naive() - timedelta(seconds=HANDSHAKE_TTL)
    for k in [k for k, (_, t) in _cli_states.items() if t < cutoff]:
        _cli_states.pop(k, None)
    for k in [k for k, (_, t) in _cli_results.items() if t < cutoff]:
        _cli_results.pop(k, None)
    for k in [k for k, (_, _, t) in _cli_pending.items() if t < cutoff]:
        _cli_pending.pop(k, None)


class EmailAuthError(Exception):
    """A framework-neutral email-auth refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class CliPairingError(Exception):
    """A framework-neutral CLI pairing refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class SocialLoginError(Exception):
    """A framework-neutral social-login outcome translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class IdentityLookupError(Exception):
    """The presented browser or token identity did not resolve to an active user."""


class InviteSigninError(Exception):
    """A framework-neutral invite sign-in refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class OAuthServerError(Exception):
    """A framework-neutral OAuth refusal translated by the HTTP router."""

    def __init__(
        self, error: str, description: str, *, status: int = 400, redirect: bool = False,
    ):
        self.error = error
        self.description = description
        self.status = status
        self.redirect = redirect
        super().__init__(error)


class OAuthGrantError(Exception):
    """A framework-neutral grant-management refusal translated by the HTTP router."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class VerifiedEmail:
    token: str
    email: str
    session_cookie: str


@dataclass(frozen=True)
class SocialLoginStart:
    state: str
    url: str


@dataclass(frozen=True)
class SocialLoginProof:
    user: User
    cli_state: tuple | None


@dataclass(frozen=True)
class RevokedIdentityTokens:
    token: str
    email: str
    session_cookie: str


@dataclass(frozen=True)
class CurrentIdentity:
    email: str
    is_superadmin: bool
    onboarded: bool
    github: bool
    org_id: int | None = None
    org: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class InviteEmailConfirmation:
    email: str
    org_name: str
    invited_by: str
    role: str
    switch_email: str | None


@dataclass(frozen=True)
class InviteCodePrefill:
    email: str


@dataclass(frozen=True)
class InviteSigninProof:
    destination: str
    session_cookie: str


@dataclass(frozen=True)
class OAuthAuthorizationView:
    client_id: str
    client_name: str
    client_uri: str
    client_kind: str
    user_email: str
    teams: list[dict]


async def start_email_login(email: str, client_ip: str) -> dict:
    """Issue and deliver an email OTP, committing its rate and one-time state atomically."""
    email = _norm_email(email)
    if email.endswith("@" + demo_seed.DEMO_DOMAIN):
        raise EmailAuthError("demo_address")
    if _is_machine_email(email):
        raise EmailAuthError("machine_identity")

    async with database.session_maker() as db:
        await ratestore.sweep(db, OTP_START_NS)
        if not await ratestore.rate_check(
            db, OTP_START_NS,
            [(f"e:{email}", OTP_START_MAX_PER_EMAIL), (f"i:{client_ip}", OTP_START_MAX_PER_IP)],
            OTP_START_WINDOW_S,
        ):
            await db.commit()
            raise EmailAuthError("rate_limited")
        code = f"{_secrets.randbelow(1_000_000):06d}"
        await ratestore.kv_put(
            db, OTP_NS, email,
            {"hash": crypto.hash_token(code), "attempts": MAX_OTP_ATTEMPTS}, EMAIL_CODE_TTL,
        )
        await db.commit()

    result = {"sent": True, "email": email}
    if get_settings().expose_dev_code:
        print(f"[email-otp] {email} -> {code}")
        result["dev_code"] = code
    else:
        await email_sender.send_otp(email, code, ttl_minutes=EMAIL_CODE_TTL // 60)
    return result


async def verify_email_login(email: str, code: str) -> VerifiedEmail:
    """Consume an email OTP and return both CLI and browser credentials for the proven identity."""
    email = _norm_email(email)
    async with database.session_maker() as db:
        entry = await ratestore.kv_get(db, OTP_NS, email)
        if entry is None:
            await db.commit()
            raise EmailAuthError("invalid_code")
        if not hmac.compare_digest(entry["hash"], crypto.hash_token(code.strip())):
            entry["attempts"] -= 1
            if entry["attempts"] <= 0:
                await ratestore.kv_pop(db, OTP_NS, email)
            else:
                await ratestore.kv_put(db, OTP_NS, email, entry, ttl_s=None)
            await db.commit()
            raise EmailAuthError("invalid_code")
        await ratestore.kv_pop(db, OTP_NS, email)
        try:
            user = await signup.find_or_create_user(db, email)
        except signup.MachineIdentityError as exc:
            raise EmailAuthError("machine_identity") from exc
        if user.suspended:
            raise EmailAuthError("suspended")
        await db.commit()
        token = sess.make(user.id, CLI_TOKEN_TTL, user.token_version)
        session_cookie = sess.make(user.id, token_version=user.token_version)
        return VerifiedEmail(token=token, email=user.email, session_cookie=session_cookie)


def _norm_pair_code(code: str | None) -> str:
    """Normalise a login pairing code for comparison: strip, uppercase, drop separators/whitespace so
    `7f3k`, `7F3K`, ` 7F3K ` all match. Empty stays empty (an empty code never matches)."""
    return "".join((code or "").split()).replace("-", "").upper()


async def start_cli_login() -> dict:
    """Mint and retain the server side of a CLI pairing handshake."""
    _prune_handshakes()
    login_id = _secrets.token_urlsafe(18)
    code = "".join(_secrets.choice(_PAIR_ALPHABET) for _ in range(4))
    _cli_pending[login_id] = (code, CLI_APPROVE_MAX_TRIES, _utcnow_naive())
    return {"login_id": login_id, "code": code}


async def poll_cli_login(login_id: str) -> dict:
    """Return a completed handshake exactly once, otherwise report it pending."""
    _prune_handshakes()  # sweep abandoned results (they hold live tokens) so the map can't leak
    entry = _cli_results.pop(login_id, None)
    return entry[0] if entry is not None else {"status": "pending"}


async def _orgs_brief(user: User, db: AsyncSession) -> list[dict]:
    """The user's teams for the /login picker: slug, name, role, tool_count, personal. Sorted so the
    team a CLI login should default to sits first (a real team over the personal org, then most tools).
    `personal` mirrors the dashboard's rule: the auto-created org named after the user's email."""
    memberships = (await db.execute(
        select(Membership).where(Membership.user_id == user.id))).scalars().all()
    org_ids = [m.org_id for m in memberships]
    if not org_ids:
        return []
    orgs = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_(org_ids)))).scalars().all()}
    counts = dict((await db.execute(
        select(Tool.org_id, func.count(Tool.id)).where(Tool.org_id.in_(org_ids)).group_by(Tool.org_id))).all())
    out = []
    for m in memberships:
        o = orgs.get(m.org_id)
        if o is None:
            continue
        out.append({"slug": o.slug, "name": o.name, "role": m.role,
                    "tool_count": counts.get(o.id, 0), "personal": o.name == user.email})
    out.sort(key=lambda r: (r["personal"], -r["tool_count"], r["name"].lower()))
    return out


async def cli_orgs(session_cookie: str) -> dict:
    async with database.session_maker() as db:
        user = await _user_from_session(session_cookie, db)
        if user is None:
            return {"email": None, "orgs": []}
        return {"email": user.email, "orgs": await _orgs_brief(user, db)}


async def approve_cli_login(
    session_cookie: str, login_id: str, code: str | None, requested_org: str | None,
) -> dict:
    async with database.session_maker() as db:
        user = await _user_from_session(session_cookie, db)
        if user is None:
            raise CliPairingError("no_session")
        # The pairing code proves the approver is the same person who ran `treg login` (the code is
        # shown only in that terminal). Validate it after session resolution and before org lookup so
        # a phished login link cannot complete and the poll endpoint stays codeless.
        pending = _cli_pending.get(login_id)
        if pending is None:
            raise CliPairingError("expired")
        expected, tries_left, started_at = pending
        typed = _norm_pair_code(code)
        if not typed or not hmac.compare_digest(expected.encode(), typed.encode()):
            if tries_left <= 1:  # discard first when the final permitted miss is consumed
                _cli_pending.pop(login_id, None)
                raise CliPairingError("too_many_wrong_codes")
            _cli_pending[login_id] = (expected, tries_left - 1, started_at)
            raise CliPairingError("wrong_code")
        active_org: str | None = None
        if requested_org:
            org = await _resolve_org(requested_org, db)
            membership = (await db.execute(select(Membership).where(
                Membership.user_id == user.id,
                Membership.org_id == org.id,
            ))).scalar_one_or_none() if org else None
            if org is None or membership is None:
                raise CliPairingError("not_member")
            active_org = org.slug
        _cli_pending.pop(login_id, None)  # code matched, so consume the pending login before publishing
        result = {"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version), "email": user.email}
        if active_org:
            result["active_org"] = active_org
        _cli_results[login_id] = (result, _utcnow_naive())
        return {"ok": True, "email": user.email, "active_org": active_org}


async def cli_session_email(session_cookie: str) -> str | None:
    async with database.session_maker() as db:
        user = await _user_from_session(session_cookie, db)
        return user.email if user else None


async def issue_cli_token(
    *, user_id: int, email: str, token_version: int, org_ref: str,
) -> dict:
    async with database.session_maker() as db:
        org_slug = None
        if org_ref:
            org = await _resolve_org(org_ref, db)
            if org is not None:
                membership = (await db.execute(select(Membership).where(
                    Membership.user_id == user_id,
                    Membership.org_id == org.id,
                ))).scalar_one_or_none()
                if membership is not None:
                    org_slug = org.slug
        return {
            "token": sess.make(user_id, CLI_TOKEN_TTL, token_version, org=org_slug),
            "email": email,
            "org": org_slug,
        }


async def revoke_identity_tokens(user_id: int) -> RevokedIdentityTokens:
    async with database.session_maker() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise IdentityLookupError
        user.token_version += 1
        await db.commit()
        return RevokedIdentityTokens(
            token=sess.make(user.id, CLI_TOKEN_TTL, user.token_version),
            email=user.email,
            session_cookie=sess.make(user.id, token_version=user.token_version),
        )


def start_github_login(cli: str, callback_base: Callable[[], str]) -> SocialLoginStart:
    s = get_settings()
    if not s.github_client_id:
        raise SocialLoginError("github_not_configured")
    redirect = f"{callback_base()}/auth/github/callback"
    state = crypto.new_token()
    if cli:  # this is a `treg login` handshake, not a browser session
        _prune_handshakes()  # evict abandoned handshakes so this map can't grow unbounded
        _cli_states[state] = (cli, _utcnow_naive())
    url = (f"{s.github_authorize_url}?client_id={s.github_client_id}"
           f"&redirect_uri={quote(redirect, safe='')}&scope={quote('read:user user:email')}&state={state}")
    return SocialLoginStart(state=state, url=url)


def start_google_login(cli: str, callback_base: Callable[[], str]) -> SocialLoginStart:
    s = get_settings()
    if not s.google_client_id:
        raise SocialLoginError("google_not_configured")
    redirect = f"{callback_base()}/auth/google/callback"
    state = crypto.new_token()
    if cli:  # a `treg login` handshake, not a browser session
        _prune_handshakes()
        _cli_states[state] = (cli, _utcnow_naive())
    url = (f"{s.google_authorize_url}?client_id={s.google_client_id}"
           f"&redirect_uri={quote(redirect, safe='')}&response_type=code"
           f"&scope={quote('openid email profile')}&state={state}&prompt=select_account")
    return SocialLoginStart(state=state, url=url)


async def _provision_social_user(email: str, state: str) -> SocialLoginProof:
    async with database.session_maker() as db:
        try:
            user = await signup.find_or_create_user(db, email)  # first login = registration (user only; no auto org)
        except signup.MachineIdentityError as exc:
            raise SocialLoginError("machine_identity") from exc
        if user.suspended:  # a banned account may prove its email but must not receive a live session
            raise SocialLoginError("suspended")
        await db.commit()
        # Browser session OR `treg login` handshake — both go through the /login team picker now.
        return SocialLoginProof(user=user, cli_state=_cli_states.pop(state, None))


async def complete_github_login(
    client_factory: Callable[[], Any], code: str, state: str, cookie_state: str,
    callback_base: Callable[[], str],
) -> SocialLoginProof:
    if not code or not state or state != cookie_state:
        raise SocialLoginError("bad_state")
    client = client_factory()
    s = get_settings()
    try:
        tok = (await client.post(
            s.github_token_url, headers={"Accept": "application/json"},
            data={"client_id": s.github_client_id, "client_secret": s.github_client_secret,
                  "code": code, "redirect_uri": f"{callback_base()}/auth/github/callback"},
        )).json()
        access = tok.get("access_token")
        if not access:
            raise SocialLoginError("github_no_access_token")
        gh = {"Authorization": f"Bearer {access}", "Accept": "application/json", "User-Agent": "treg"}
        prof = (await client.get(f"{s.github_api_url}/user", headers=gh)).json()
        email = prof.get("email")
        if not email:
            emails = (await client.get(f"{s.github_api_url}/user/emails", headers=gh)).json()
            if isinstance(emails, list):
                email = (next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
                         or next((e["email"] for e in emails if e.get("verified")), None))
        if not email:
            raise SocialLoginError("github_no_verified_email")
    except SocialLoginError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] github callback error: {exc}")  # keep internals server-side, not in the response
        raise SocialLoginError("callback_failed") from exc
    return await _provision_social_user(email, state)


async def complete_google_login(
    client_factory: Callable[[], Any], code: str, state: str, cookie_state: str,
    callback_base: Callable[[], str],
) -> SocialLoginProof:
    if not code or not state or state != cookie_state:
        raise SocialLoginError("bad_state")
    client = client_factory()
    s = get_settings()
    try:
        tok = (await client.post(
            s.google_token_url, headers={"Accept": "application/json"},
            data={"client_id": s.google_client_id, "client_secret": s.google_client_secret,
                  "code": code, "grant_type": "authorization_code",
                  "redirect_uri": f"{callback_base()}/auth/google/callback"},
        )).json()
        access = tok.get("access_token")
        if not access:
            raise SocialLoginError("google_no_access_token")
        prof = (await client.get(
            s.google_userinfo_url,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})).json()
        email = prof.get("email")
        if not email:
            raise SocialLoginError("google_no_email")
        # Identity is keyed by email, so we must only trust a VERIFIED one — else an unverified Google
        # address equal to a victim's registered email would resolve to the victim (account takeover).
        # (Google's userinfo returns email_verified; the GitHub door already filters for verified.)
        if not prof.get("email_verified"):
            raise SocialLoginError("google_unverified_email")
    except SocialLoginError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] google callback error: {exc}")  # keep internals server-side, not in the response
        raise SocialLoginError("callback_failed") from exc
    return await _provision_social_user(email, state)


async def current_identity(x_treg_token: str, session_cookie: str) -> CurrentIdentity:
    async with database.session_maker() as db:
        membership = None
        if x_treg_token:
            membership = await _membership_by_token(x_treg_token, db)
            user = (await db.get(User, membership.user_id) if membership
                    else await _user_from_identity_token(x_treg_token, db))
            if user is not None and user.suspended:
                user = None
        else:
            user = await _user_from_session(session_cookie, db)
        if user is None:
            raise IdentityLookupError
        org_id = None
        org_slug = None
        role = None
        if membership is not None:
            # The org this token IS. A machine identity cannot call GET /orgs — `require_identity`
            # refuses it on purpose, since `create_org` hangs off that dependency and an agent could
            # otherwise mint an org it owns. But it still has to learn its OWN org id to reach any
            # /orgs/{id}/... route, and being unable to told `treg balance` there was "no active org".
            org = await db.get(Org, membership.org_id)
            org_id = membership.org_id
            org_slug = org.slug if org else None
            role = membership.role
        return CurrentIdentity(
            email=user.email,
            is_superadmin=user.is_superadmin,
            onboarded=user.onboarded,
            github=bool(get_settings().github_client_id),
            org_id=org_id,
            org=org_slug,
            role=role,
        )


async def _live_invite_by_email_token(db: AsyncSession, t: str) -> Invite | None:
    """Resolve an emailed invite-link token to a live invite: pending, unexpired, unconsumed
    (email_token_hash is nulled on first use), and not pointing at a platform-locked org."""
    t = (t or "").strip()
    if not t:
        return None
    invite = (await db.execute(select(Invite).where(Invite.email_token_hash == crypto.hash_token(t)))
              ).scalar_one_or_none()
    if (invite is None or invite.status != "pending"
            or (invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive())):
        return None
    org = await db.get(Org, invite.org_id)
    if org is None or org.suspended:
        return None
    return invite


async def invite_signin_landing(
    email_token: str, code: str, session_cookie: str,
) -> InviteEmailConfirmation | InviteCodePrefill:
    async with database.session_maker() as db:
        if email_token:
            invite = await _live_invite_by_email_token(db, email_token)
            if invite is None:
                raise InviteSigninError("expired")
            org = await db.get(Org, invite.org_id)
            switch_email = None
            uid = sess.read(session_cookie)
            if uid is not None:
                current = await db.get(User, uid)
                if current is not None and current.email != invite.email:
                    switch_email = current.email
            return InviteEmailConfirmation(
                email=invite.email,
                org_name=org.name if org else "the team",
                invited_by=invite.invited_by or "A teammate",
                role=invite.role,
                switch_email=switch_email,
            )
        code = (code or "").strip()
        invite = (await db.execute(select(Invite).where(Invite.code_hash == crypto.hash_token(code)))
                  ).scalar_one_or_none() if code else None
        if (invite is None or invite.status != "pending"
                or (invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive())):
            raise InviteSigninError("expired")
        return InviteCodePrefill(email=invite.email)


async def confirm_invite_signin(email_token: str) -> InviteSigninProof:
    async with database.session_maker() as db:
        invite = await _live_invite_by_email_token(db, email_token)
        if invite is None:  # consumed / expired / revoked / suspended org → the SPA's expired banner
            raise InviteSigninError("expired")
        try:
            user = await signup.find_or_create_user(db, invite.email)  # first click = registration (user only, no auto org)
        except signup.MachineIdentityError as exc:
            raise InviteSigninError("machine_identity") from exc
        if user is None or user.suspended:  # a banned account may hold the link but must not get a session
            raise InviteSigninError("suspended")
        invite.email_token_hash = None  # consume: one sign-in per emailed link
        db.add(invite)
        await db.commit()
        # A share-born invite lands on the shared page itself (the SPA auto-accepts + switches org);
        # a plain invite lands on the dashboard with the accept banner, as before. `landing` was
        # allowlist-validated at create time, so this can never redirect off-app.
        destination = (
            f"{invite.landing}?invite_org={invite.org_id}"
            if invite.landing else f"/?invite_org={invite.org_id}"
        )
        return InviteSigninProof(
            destination=destination,
            session_cookie=sess.make(user.id, token_version=user.token_version),
        )


async def register_oauth_client(
    *, client_name: str, redirect_uris: list[str], client_uri: str, logo_uri: str, scope: str,
) -> dict:
    uris = [u for u in redirect_uris if mcp_oauth.valid_redirect_uri(u)]
    if not uris:
        raise OAuthServerError(
            "invalid_redirect_uri",
            "at least one https redirect_uri is required (http is accepted only for 127.0.0.1 / localhost, for CLI clients)",
        )
    async with database.session_maker() as db:
        client = OAuthClient(
            client_id=mcp_oauth.new_client_id(), kind="dcr",
            client_name=(client_name or "unnamed client")[:200],
            client_uri=client_uri[:500], logo_uri=logo_uri[:500],
            redirect_uris=uris[:20], scope=scope[:200])
        db.add(client)
        await db.commit()
        return {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }


async def _resolve_oauth_client(client_id: str, db: AsyncSession) -> OAuthClient | None:
    """One client row, whichever door it came through.

    A registered client is a lookup. A client_id that is an https URL is a metadata document: fetched
    on first sight, cached as a row, and refreshed when stale — documents change, and a cache that
    never expires would pin a client to redirect URIs it has since retired.
    """
    row = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
           ).scalar_one_or_none()
    fresh_enough = row is not None and (
        row.kind != "cimd" or (row.refreshed_at is not None and
                               (datetime.now(timezone.utc).replace(tzinfo=None) - row.refreshed_at
                                ).total_seconds() < mcp_oauth._CIMD_REFRESH_S))
    if fresh_enough:
        return row
    if not client_id.startswith("https://"):
        return row  # a dcr client we do not know is simply unknown
    doc = await mcp_oauth.fetch_client_id_metadata(client_id)
    if doc is None:
        return row  # keep a stale copy over nothing: a transient fetch failure is not a revocation
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None:
        row = OAuthClient(client_id=client_id, kind="cimd")
        db.add(row)
    row.kind, row.refreshed_at = "cimd", now
    row.client_name, row.client_uri = doc["client_name"], doc["client_uri"]
    row.logo_uri, row.redirect_uris, row.scope = doc["logo_uri"], doc["redirect_uris"], doc["scope"]
    await db.commit()
    await db.refresh(row)
    return row


def _wrong_resource(resource: str) -> str | None:
    """Is this `resource` one we actually protect? Returns an error message, or None if fine.

    Refusing early matters more than it looks. `resource` becomes the token's audience, and the MCP
    server accepts only its own — so accepting a resource we do not serve mints a token that is
    valid, well-formed, and silently useless. That failure surfaces later, at the first tool call,
    as "not signed in", which points the reader at authentication when the real problem was the
    audience. Found exactly that way: an independent MCP client sent the URL it was connecting to
    rather than the canonical identifier from our metadata, and got a token that could never work.

    Empty is allowed: a client that omits `resource` gets our canonical one, which is what it would
    have discovered anyway.
    """
    if not resource:
        return None
    canonical = mcp_oauth.mcp_resource_url()
    # Host aliases and slash variants stay valid within one surface. V1 and V2 stay distinct.
    version = mcp_oauth.mcp_resource_version(resource)
    if version == "v2" and not get_settings().claude_connector_enabled:
        return "the Claude catalog connector is not enabled on this deployment"
    if version is not None:
        return None
    return (f"this server issues tokens for {canonical} or {mcp_oauth.mcp_resource_url('v2')} only "
            "— use the `resource` value from "
            f"/.well-known/oauth-protected-resource")


def _effective_mcp_resource(resource: str, scope: str) -> str:
    """Use an explicit resource, or the V2 scope marker when hosted Claude omits it."""
    if resource:
        return mcp_oauth.normalize_resource(resource)
    requested_scopes = set((scope or "").split())
    version = "v2" if mcp_oauth.DIRECTORY_SCOPE in requested_scopes else "v1"
    return mcp_oauth.mcp_resource_url(version)


def _same_mcp_resource(a: str, b: str) -> bool:
    """Whether two `resource` values name this same MCP server. Exact match, slash-variant match,
    or BOTH normalize into the canonical+legacy audience set — the domain move renamed the
    resource without changing it, so a grant consented on one name must stay exchangeable and
    refreshable by a client re-based onto the other (in either direction)."""
    na, nb = mcp_oauth.normalize_resource(a), mcp_oauth.normalize_resource(b)
    if a == b or na == nb:
        return True
    a_version = mcp_oauth.mcp_resource_version(na)
    b_version = mcp_oauth.mcp_resource_version(nb)
    return a_version is not None and a_version == b_version


async def _authorize_request(client_id: str, redirect_uri: str, response_type: str,
                             code_challenge: str, code_challenge_method: str,
                             db: AsyncSession) -> OAuthClient:
    """Validate the client and redirect before any refusal is allowed to redirect.

    Order matters and is deliberate: identify the client and its redirect FIRST, because until both
    are known-good there is nowhere safe to send an error. Everything after that can be reported to
    the client properly.
    """
    client = await _resolve_oauth_client(client_id, db) if client_id else None
    if client is None:
        raise OAuthServerError(
            "invalid_client",
            "unknown client_id — register first, or serve a client-id metadata document",
        )
    if not mcp_oauth.redirect_uri_allowed(client, redirect_uri):
        # NOT a redirect: we do not bounce errors to a URI the client has not proven is theirs.
        raise OAuthServerError(
            "invalid_request",
            "redirect_uri does not exactly match one registered for this client",
        )
    return client


def _redirect_refusal(error: str, description: str) -> OAuthServerError:
    return OAuthServerError(error, description, redirect=True)


async def prepare_oauth_authorization(
    *, client_id: str, redirect_uri: str, response_type: str, code_challenge: str,
    code_challenge_method: str, resource: str, scope: str, session_cookie: str,
) -> OAuthAuthorizationView | None:
    async with database.session_maker() as db:
        client = await _authorize_request(
            client_id, redirect_uri, response_type, code_challenge, code_challenge_method, db,
        )
        if response_type != "code":
            raise _redirect_refusal(
                "unsupported_response_type", "only the authorization code flow is supported",
            )
        if not code_challenge or code_challenge_method != "S256":
            raise _redirect_refusal(
                "invalid_request", "PKCE with code_challenge_method=S256 is required",
            )
        effective_resource = _effective_mcp_resource(resource, scope)
        if (bad_target := _wrong_resource(effective_resource)) is not None:
            raise _redirect_refusal("invalid_target", bad_target)

        user = await _user_from_session(session_cookie, db)
        if user is None:
            return None

        memberships = (await db.execute(
            select(Membership).where(Membership.user_id == user.id))).scalars().all()
        teams = []
        for membership in memberships:
            org = await db.get(Org, membership.org_id)
            if org is not None and not org.suspended:
                # The BALANCE belongs on this list. Choosing a team here decides which balance the
                # client spends for the life of the grant, and a list of slugs makes the one question
                # that matters — "which of these can actually pay?" — invisible at the moment of
                # choosing. Unclecode picked a $0.00 team on the first real ChatGPT connect and the call
                # was refused; nothing on the page could have told him.
                teams.append({"org_id": org.id, "slug": org.slug, "role": membership.role,
                              "balance_usd": round((org.balance_micro or 0) / 1_000_000, 4)})
        if not teams:
            raise _redirect_refusal(
                "access_denied", "this account is not a member of any team",
            )
        return OAuthAuthorizationView(
            client_id=client.client_id,
            client_name=client.client_name,
            client_uri=client.client_uri,
            client_kind=client.kind,
            user_email=user.email,
            teams=teams,
        )


async def approve_oauth_authorization(
    *, decision: str, client_id: str, redirect_uri: str, response_type: str, scope: str,
    code_challenge: str, code_challenge_method: str, resource: str, org_id: int,
    session_cookie: str,
) -> str:
    async with database.session_maker() as db:
        client = await _authorize_request(
            client_id, redirect_uri, response_type, code_challenge, code_challenge_method, db,
        )
        if decision != "allow":
            # Cancel is a real answer and the client is entitled to hear it, rather than hang.
            raise _redirect_refusal("access_denied", "the user declined")
        effective_resource = _effective_mcp_resource(resource, scope)
        if (bad_target := _wrong_resource(effective_resource)) is not None:
            raise _redirect_refusal("invalid_target", bad_target)
        if not code_challenge or code_challenge_method != "S256":
            raise _redirect_refusal(
                "invalid_request", "PKCE with code_challenge_method=S256 is required",
            )

        user = await _user_from_session(session_cookie, db)
        if user is None:
            raise OAuthServerError("access_denied", "not signed in", status=401)
        # The chosen team must be one this user actually belongs to — the field is client-supplied.
        membership = (await db.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
        if membership is None:
            raise _redirect_refusal("access_denied", "choose a team you are a member of")

        code = OAuthCode(
            code=_secrets.token_urlsafe(32), client_id=client.client_id, user_id=user.id, org_id=org_id,
            redirect_uri=redirect_uri, code_challenge=code_challenge,
            resource=effective_resource,
            scope=scope,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=AUTH_CODE_TTL_S))
        db.add(code)
        await db.commit()
        return code.code


async def _refresh_grant(*, refresh_token: str, client_id: str, resource: str) -> dict:
    """Exchange a refresh token for a new access token, ROTATING the refresh token as we go.

    The retired row is kept, not deleted. That is what makes a replay recognisable: a deleted token
    looks merely unknown, while a retired one tells us somebody used a credential that had already
    been spent — and at that point the safe reading is that it was copied.
    """
    async with database.session_maker() as db:
        if not refresh_token:
            raise OAuthServerError("invalid_request", "refresh_token is required")
        row = (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.token_hash == crypto.hash_token(refresh_token)))).scalar_one_or_none()
        if row is None:
            raise OAuthServerError("invalid_grant", "unknown refresh token")

        if row.retired_at is not None:
            # Already spent. Either a client retried after a dropped response, or someone else has a
            # copy — indistinguishable from here, so assume the worse one and end the whole family. The
            # cost of being wrong is one sign-in; the cost of the other mistake is somebody's balance.
            killed = await _revoke_refresh_family(row.family_id, "reuse detected", db)
            await db.commit()
            audit.record_call(org_id=row.org_id, user_email="", tool_name="oauth.refresh_reuse",
                              method="POST", path="/oauth/token", status_code=400, client="",
                              telemetry={"family": row.family_id, "revoked": killed})
            raise OAuthServerError(
                "invalid_grant",
                "this refresh token was already used — the grant has been revoked, sign in again",
            )

        if not _refresh_is_live(row):
            raise OAuthServerError("invalid_grant", "refresh token expired")
        if client_id and client_id != row.client_id:
            raise OAuthServerError("invalid_grant", "refresh token was issued to a different client")
        if resource and not _same_mcp_resource(resource, row.resource):
            raise OAuthServerError(
                "invalid_target", "resource does not match the one that was consented to",
            )

        user = await db.get(User, row.user_id)
        if user is None or user.suspended:
            raise OAuthServerError(
                "invalid_grant", "the account behind this grant is no longer active",
            )
        live_org_id = await _family_org(row.family_id, db) or row.org_id
        org = await db.get(Org, live_org_id)
        if org is None or org.suspended:
            raise OAuthServerError(
                "invalid_grant", "the team on this grant is no longer available",
            )
        # STILL a member? The grant is the user's consent to spend a TEAM's balance, and leaving (or
        # being removed from) that team ends the standing they consented with. Without this a grant
        # kept minting tokens forever: every downstream call was refused by `require_member`, so the
        # damage was bounded, but the grant lay dormant and sprang back to life — with no new consent —
        # the day the membership was restored.
        still_in = (await db.execute(select(Membership).where(
            Membership.user_id == row.user_id, Membership.org_id == live_org_id))).scalar_one_or_none()
        if still_in is None:
            await _revoke_refresh_family(row.family_id, "membership ended", db)
            await db.commit()
            raise OAuthServerError(
                "invalid_grant",
                "the account behind this grant is no longer a member of its team — sign in again",
            )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row.retired_at, row.retired_reason = now, "rotated"
        db.add(row)
        replacement = await _issue_refresh(family_id=row.family_id, client_id=row.client_id,
                                           user_id=row.user_id, org_id=live_org_id,
                                           resource=row.resource, scope=row.scope, db=db)
        access = mcp_oauth.make_access_token(
            user_id=row.user_id, org_id=live_org_id, scope=row.scope,
            audience=mcp_oauth.normalize_resource(row.resource),  # heal pre-normalization spellings
            token_version=user.token_version)
        await db.commit()
        return {"access_token": access, "token_type": "Bearer",
                "expires_in": mcp_oauth.ACCESS_TTL_SECONDS, "scope": row.scope,
                "refresh_token": replacement}


async def exchange_oauth_token(
    *, grant_type: str, code: str, redirect_uri: str, client_id: str, code_verifier: str,
    resource: str, refresh_token: str,
) -> dict:
    if grant_type == "refresh_token":
        return await _refresh_grant(
            refresh_token=refresh_token, client_id=client_id, resource=resource,
        )
    if grant_type != "authorization_code":
        raise OAuthServerError(
            "unsupported_grant_type", "supported grants: authorization_code, refresh_token",
        )

    async with database.session_maker() as db:
        row = (await db.execute(select(OAuthCode).where(OAuthCode.code == code))
               ).scalar_one_or_none() if code else None
        if row is None:
            raise OAuthServerError("invalid_grant", "unknown or already-redeemed code")

        # DELETE FIRST. A code is single-use, and holding it while validating leaves a window where two
        # redemptions both read it. Everything below is validated against values already in hand.
        await db.delete(row)
        await db.commit()

        if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            raise OAuthServerError("invalid_grant", "code expired")
        if client_id and client_id != row.client_id:
            raise OAuthServerError("invalid_grant", "code was issued to a different client")
        if redirect_uri != row.redirect_uri:
            raise OAuthServerError(
                "invalid_grant", "redirect_uri does not match the one the code was issued for",
            )
        if not mcp_oauth.verify_pkce(code_verifier, row.code_challenge):
            raise OAuthServerError(
                "invalid_grant", "code_verifier does not match the code_challenge",
            )
        if resource and not _same_mcp_resource(resource, row.resource):
            raise OAuthServerError(
                "invalid_target", "resource does not match the one that was consented to",
            )

        user = await db.get(User, row.user_id)
        if user is None or user.suspended:
            raise OAuthServerError(
                "invalid_grant", "the account behind this grant is no longer active",
            )

        token = mcp_oauth.make_access_token(
            user_id=row.user_id, org_id=row.org_id, scope=row.scope,
            audience=mcp_oauth.normalize_resource(row.resource),  # heal pre-normalization spellings
            token_version=user.token_version)
        refresh = await _issue_refresh(family_id=_secrets.token_urlsafe(16), client_id=row.client_id,
                                       user_id=row.user_id, org_id=row.org_id, resource=row.resource,
                                       scope=row.scope, db=db)
        await db.commit()
        return {"access_token": token, "token_type": "Bearer",
                "expires_in": mcp_oauth.ACCESS_TTL_SECONDS, "scope": row.scope,
                "refresh_token": refresh}


async def revoke_oauth_token(token: str) -> None:
    async with database.session_maker() as db:
        if token:
            row = (await db.execute(select(OAuthRefresh).where(
                OAuthRefresh.token_hash == crypto.hash_token(token)))).scalar_one_or_none()
            if row is not None:
                await _revoke_refresh_family(row.family_id, "revoked by client", db)
                await db.commit()


async def list_oauth_grants(user_id: int) -> list[dict]:
    async with database.session_maker() as db:
        rows = (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.user_id == user_id, OAuthRefresh.retired_at.is_(None)
        ).order_by(OAuthRefresh.created_at.desc()))).scalars().all()
        rows = [row for row in rows if _refresh_is_live(row)]
        seen: set[str] = set()
        out: list[dict] = []
        for row in rows:                      # one entry per GRANT, not per rotation of its token
            if row.family_id in seen:
                continue
            seen.add(row.family_id)
            # Listing and refresh must read the SAME family authority. A token row's org_id is immutable
            # issue provenance and may legitimately name the team used before a later move.
            grant = await _ensure_grant(row.family_id, db)
            org_id = grant.current_org_id if grant is not None else None
            org = await db.get(Org, org_id) if org_id is not None else None
            client = (await db.execute(select(OAuthClient).where(
                OAuthClient.client_id == row.client_id))).scalar_one_or_none()
            out.append({
                "grant": row.family_id,
                "client": (client.client_name if client else "") or row.client_id,
                "team": org.slug if org else None,
                "team_name": org.name if org else None,
                "granted": grant.granted_at.isoformat(timespec="seconds") if grant else None,
            })
        # GET normally reads only, but repairing a family created by an old rolling-deploy instance is
        # a durable compatibility backfill. Without this commit the response looks healed once while
        # the inserted authority row is rolled back when the request session closes.
        await db.commit()
        return out


async def move_oauth_grant(*, user_id: int, family_id: str, team: str) -> dict:
    async with database.session_maker() as db:
        rows = (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.family_id == family_id, OAuthRefresh.user_id == user_id,
            OAuthRefresh.retired_at.is_(None)))).scalars().all()
        rows = [row for row in rows if _refresh_is_live(row)]
        if not rows:
            raise OAuthGrantError(f"no live grant {family_id!r} on this account")
        org = await _resolve_org(team, db)
        member = (await db.execute(select(Membership).where(
            Membership.user_id == user_id, Membership.org_id == org.id))).scalar_one_or_none() if org else None
        # ONE answer for "no such team" and "a team that isn't yours". Told apart, this route reports
        # whether an arbitrary slug exists on treg — a slug-existence oracle any signed-in account could
        # walk. The caller's own teams are already listed to them by `treg org ls`, so the distinction
        # buys them nothing they cannot see elsewhere.
        if org is None or org.suspended or member is None:
            raise OAuthGrantError(
                f"no team {team!r} on this account — see `treg org ls` for the teams you can use",
            )
        # Change only family authority. Token rows are evidence of where each historical bearer was
        # issued and must stay immutable, especially retired rows kept for reuse detection.
        grant = await _ensure_grant(family_id, db)
        if grant is None:  # the live-row check above makes this defensive, not a normal outcome
            raise OAuthGrantError(f"no live grant {family_id!r} on this account")
        grant.current_org_id = org.id
        db.add(grant)
        await db.commit()
        return {
            "grant": family_id, "team": org.slug, "team_name": org.name,
            # Access tokens live an hour and carry the old team until the client refreshes. Saying so
            # is the difference between "it didn't work" and "it hasn't taken effect yet".
            "note": (f"new calls spend from {org.slug!r} once the client refreshes its access token "
                     f"(within {mcp_oauth.ACCESS_TTL_SECONDS // 60} minutes)"),
        }
