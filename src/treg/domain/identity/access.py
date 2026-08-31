"""Caller identity, token, role, and authorization resolution."""

from __future__ import annotations

from dataclasses import dataclass
import hmac

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto
from ...config import get_settings
from ...infra.db import get_session
from ...models import ROLE_RANK, Membership, Org, User
from . import session as sess


@dataclass
class Caller:
    """The resolved caller: their membership (org + role + token), identity, and org row.
    A token identifies a (user, org) pair, so `org_id`/`email`/`role` all come from here.
    """

    membership: Membership
    user: User
    org: Org

    @property
    def org_id(self) -> int:
        return self.membership.org_id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def role(self) -> str:
        return self.membership.role


async def _membership_by_token(token: str, db: AsyncSession) -> Membership | None:
    if not token:
        return None
    return (
        await db.execute(select(Membership).where(Membership.token_hash == crypto.hash_token(token)))
    ).scalar_one_or_none()


async def _user_from_session(cookie: str, db: AsyncSession) -> User | None:
    claims = sess.read_claims(cookie)
    if claims is None:
        return None
    user = await db.get(User, claims["uid"])
    if user is None or user.suspended or claims["tv"] != user.token_version:  # revoked = tv mismatch
        return None
    return user


async def _resolve_org(ref: str, db: AsyncSession) -> Org | None:
    """Resolve an X-Treg-Org header (a slug, or a numeric id) to an Org. Slug wins first: an
    all-digit slug is producible (`_slugify("2024") == "2024"`), so an id-first lookup would
    reinterpret a member's own slug as a primary key and lock them out of their org."""
    if not ref:
        return None
    by_slug = (await db.execute(select(Org).where(Org.slug == ref))).scalar_one_or_none()
    if by_slug is not None:
        return by_slug
    # int() of a huge all-digit ref would overflow SQLite's 64-bit INTEGER → 500 inside the auth
    # dependency; bound it so an out-of-range X-Treg-Org just falls through to the 400.
    return await db.get(Org, int(ref)) if (ref.isdigit() and int(ref) < 2**63) else None


async def require_identity(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> User:
    """Just *who* the caller is (no org): a token's user, or a session user. 401 otherwise."""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        if m is not None:
            # A published public-demo token must never act as a USER — user-level endpoints mint
            # identity tokens (/auth/cli-token), create real orgs, and accept invites, all of which
            # would let a stranger escape the demo org. Admin+ (the real operator) is exempt.
            org = await db.get(Org, m.org_id)
            if org is not None and org.public_demo and not _role_at_least(m.role, "admin"):
                raise HTTPException(status_code=403, detail=(
                    "this is a public demo token — it can only call the demo team's tools"))
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        # A machine token must never act as a USER. `create_org` depends on THIS dependency, so an
        # agent could otherwise create a fresh org in which it is the OWNER — and owners are exempt
        # from `_require_tool_access` and `_require_local_run`, escaping every limit set on it.
        if user is not None and _is_machine_email(user.email):
            raise HTTPException(status_code=403, detail=(
                "this token belongs to a machine identity — it can call this team's tools, "
                "but cannot act as a user"))
        if user is not None and not user.suspended:
            # Release the auth read transaction before a handler opens an application session;
            # holding this pool slot while waiting for a second one can deadlock a bounded pool.
            await db.commit()
            return user
        raise HTTPException(status_code=401, detail="invalid token")
    user = await _user_from_session(treg_session, db)
    if user is not None:
        await db.commit()
        return user
    raise HTTPException(status_code=401, detail="not authenticated")


async def _user_from_identity_token(token: str, db: AsyncSession) -> User | None:
    """A signed identity token (from `treg login`) — same format as the session cookie, sent as a
    bearer by the CLI. Returns the user if valid + not suspended."""
    claims = sess.read_claims(token)
    if claims is None:
        return None
    user = await db.get(User, claims["uid"])
    if user is None or user.suspended or claims["tv"] != user.token_version:  # revoked = tv mismatch
        return None
    return user


async def require_member(
    request: Request,
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> Caller:
    """A caller acting in a specific org. Two ways in:
    - **token** (agents/CLI): the token IS a membership, so the org is baked in.
    - **session** (dashboard): the cookie identifies the user; the org is chosen via `X-Treg-Org`.
    """
    membership = await _membership_by_token(x_treg_token, db) if x_treg_token else None
    if membership is not None:  # per-org token — the org is baked in
        user = await db.get(User, membership.user_id)
        org = await db.get(Org, membership.org_id)
    else:
        # identity token (CLI `treg login`) or a browser session — pick the org via X-Treg-Org
        user = (await _user_from_identity_token(x_treg_token, db)) if x_treg_token else await _user_from_session(treg_session, db)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid token" if x_treg_token else "not authenticated")
        # The X-Treg-Org header wins; a team-pinned identity token (org baked into its claim) is the
        # fallback, so a copyable "API key" resolves as a BARE bearer where no header can travel — an
        # MCP server's Authorization. The header still overrides, so one token can act on another team
        # when the caller can set it (the CLI does). Only the token owner could sign it, so trusting
        # its own org claim grants nothing they could not already reach.
        org_ref = x_treg_org or ((sess.read_claims(x_treg_token) or {}).get("org", "") if x_treg_token else "")
        org = await _resolve_org(org_ref, db)
        if org is None:
            raise HTTPException(status_code=400, detail="choose an org (send X-Treg-Org)")
        membership = (
            await db.execute(
                select(Membership).where(Membership.user_id == user.id, Membership.org_id == org.id)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=403, detail="not a member of this org")
    if user is None or org is None:
        raise HTTPException(status_code=401, detail="invalid token")
    if user.suspended:
        raise HTTPException(status_code=403, detail="account suspended")
    if org.suspended:
        raise HTTPException(status_code=403, detail="org suspended")
    # Public-demo lockdown: the published token (non-admin roles) may ONLY call tools and read.
    # Centralized here — not per-endpoint — so every mutation (tools, secrets, skills, members,
    # leave, runs) is frozen no matter what routes are added later. Admin+ keeps full control.
    if org.public_demo and not _role_at_least(membership.role, "admin"):
        if not (request.url.path.startswith("/call/") or request.method in ("GET", "HEAD", "OPTIONS")):
            raise HTTPException(status_code=403, detail=(
                "this is a public demo team — its token can only call tools and read"))
    await db.commit()
    return Caller(membership=membership, user=user, org=org)


async def require_superadmin(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> str:
    """Cross-tenant gate for /admin/*. Authorized by the env admin token, a token whose user is
    is_superadmin, OR a session whose user is is_superadmin. Returns a principal (for audit)."""
    admin = get_settings().admin_token
    if x_treg_token and admin and hmac.compare_digest(x_treg_token, admin):
        await db.commit()
        return "env-admin"
    user: User | None = None
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
    else:
        user = await _user_from_session(treg_session, db)
    if user is not None and user.is_superadmin and not user.suspended:
        await db.commit()
        return user.email
    if not x_treg_token and not treg_session:  # nothing presented → not authenticated
        raise HTTPException(status_code=401, detail="not authenticated")
    raise HTTPException(status_code=403, detail="super-admin required")


def _role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, 99)


def _can_manage(caller: Caller, resource) -> bool:
    """Admin/owner may manage any resource in the org; a member only what they created."""
    return _role_at_least(caller.role, "admin") or resource.owner == caller.email


def _require_can_register(caller: Caller) -> None:
    """Registering (secrets/tools/skills/oauth) needs member+. A viewer may only call + read."""
    if not _role_at_least(caller.role, "member"):
        raise HTTPException(status_code=403, detail="viewers can call and read, but cannot register")


def _norm_email(email: str) -> str:
    """Canonical email identity: trimmed + lowercased. One human = one identity regardless of the
    case they type. Applied at every identity door + every invite comparison so `Bob@X.com` and
    `bob@x.com` never fork into two users / two orgs and an invite is always redeemable."""
    return email.strip().lower()


# ---- machine identities: the publishable demo token, and agents ----------------------------
# Both are Users on an UNROUTABLE domain, which is what makes them machines rather than people: no
# login door can ever resolve one (guarded in `_find_or_create_user`) and neither may act as a USER
# (guarded in `require_identity`). Everything else they inherit from Membership for free.
PUBLIC_DEMO_DOMAIN = "public-demo.treg.local"  # unroutable — the public identity can never log in
# NOTE: "agent" here is an IDENTITY — a coding agent / automation that calls treg. It is NOT the
# skill-directory table in `agents.py` (which answers "where does each coding agent keep its skills").
# The words collide, the concepts don't; kept apart deliberately.
AGENT_DOMAIN = "agents.treg.local"  # unroutable — an agent acts only by its token


def _is_agent_email(email: str) -> bool:
    return _norm_email(email).endswith(f"@{AGENT_DOMAIN}")


def _is_machine_email(email: str) -> bool:
    """An identity minted by an admin for a machine — never a person who can sign in."""
    return _is_agent_email(email) or _norm_email(email).endswith(f"@{PUBLIC_DEMO_DOMAIN}")
