"""Caller identity, token, role, and authorization resolution."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto
from ...config import get_settings
from ...infra.db import get_admin_session, get_session
from ...models import ROLE_RANK, ApiKey, Membership, Org, User
from . import api_keys as managed_keys
from . import session as sess


@dataclass
class Caller:
    """The resolved caller: their membership (org + role + token), identity, and org row.
    A token identifies a (user, org) pair, so `org_id`/`email`/`role` all come from here.
    """

    membership: Membership
    user: User
    org: Org
    api_key: ApiKey | None

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
    token_hash = crypto.hash_token(token)
    key = (await db.execute(select(ApiKey).where(ApiKey.key_hash == token_hash))).scalar_one_or_none()
    if key is not None:
        # A known managed record always wins. Never continue to Membership.token_hash after a
        # disable or revoke, because that would make the compatibility column a revoke bypass.
        if key.state == managed_keys.DISABLED:
            raise HTTPException(status_code=401, detail="disabled key")
        if key.state == managed_keys.REVOKED:
            raise HTTPException(status_code=401, detail="revoked key")
        membership = await db.get(Membership, key.membership_id) if key.membership_id else None
        if membership is None:
            raise HTTPException(status_code=403, detail="membership removed or suspended")
        return membership
    return (
        await db.execute(select(Membership).where(Membership.token_hash == token_hash))
    ).scalar_one_or_none()


async def _membership_and_key_by_token(
    token: str, db: AsyncSession,
) -> tuple[Membership | None, ApiKey | None]:
    if not token:
        return None, None
    token_hash = crypto.hash_token(token)
    membership = await _membership_by_token(token, db)
    if membership is None:
        return None, None
    key = (await db.execute(select(ApiKey).where(ApiKey.key_hash == token_hash))).scalar_one_or_none()
    if key is not None:
        return membership, key
    # Staged-release compatibility only. The migration backfills every stored hash. Directly-built
    # test and old rolling-deploy rows can still reach this path until coverage is complete.
    user = await db.get(User, membership.user_id)
    if user is None:
        return membership, None
    key = await managed_keys.register_membership_token(db, membership, user, token)
    return membership, key


def _require_active_key(key: ApiKey | None) -> None:
    if key is None or key.state == managed_keys.ACTIVE:
        return
    if key.state == managed_keys.DISABLED:
        raise HTTPException(status_code=401, detail="disabled key")
    raise HTTPException(status_code=401, detail="revoked key")


async def _user_from_session(cookie: str, db: AsyncSession) -> User | None:
    claims = sess.read_session_claims(cookie)
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
    x_treg_org: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> User:
    """Just *who* the caller is (no org): a token's user, or a session user. 401 otherwise."""
    if x_treg_token:
        m, key = await _membership_and_key_by_token(x_treg_token, db)
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
            claims = sess.read_identity_claims(x_treg_token)
            oauth_bridge = bool(
                claims
                and claims.get("aud") == sess.IDENTITY_AUDIENCE
                and claims.get("exp") is not None
            )
            if m is None and claims and not oauth_bridge:
                # Validate a typed Default key against the membership it names. The requested
                # X-Treg-Org is deliberately handled by the endpoint afterward (for example,
                # `org use` may exchange team A's valid key for team B's Default key). Comparing
                # team A's generation with team B's control row would reject valid switches when
                # the two teams have rotated a different number of times.
                auth_org_ref = (
                    claims.get("org", "")
                    if claims.get("scope") == sess.TEAM_SCOPE
                    else x_treg_org or claims.get("org", "")
                )
                org = await _resolve_org(auth_org_ref, db)
                if org is not None:
                    m = (await db.execute(select(Membership).where(
                        Membership.user_id == user.id, Membership.org_id == org.id,
                    ))).scalar_one_or_none()
                    if m is not None:
                        key = await managed_keys.ensure_default_key(db, m, user)
                        _require_active_key(key)
                        if claims.get("org") and key is not None:
                            if int(claims.get("kg", 0)) != key.default_generation:
                                raise HTTPException(status_code=401, detail="revoked key")
            # Release the auth read transaction before a handler opens an application session;
            # holding this pool slot while waiting for a second one can deadlock a bounded pool.
            await db.commit()
            managed_keys.touch(key)
            return user
        raise HTTPException(status_code=401, detail="invalid token")
    user = await _user_from_session(treg_session, db)
    if user is not None:
        await db.commit()
        return user
    raise HTTPException(status_code=401, detail="not authenticated")


async def _user_from_identity_token(token: str, db: AsyncSession) -> User | None:
    """Resolve a signed identity bearer, including safely distinguishable legacy tokens."""
    claims = sess.read_identity_claims(token)
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
    membership, api_key = (
        await _membership_and_key_by_token(x_treg_token, db)
        if x_treg_token else (None, None)
    )
    if membership is not None:  # per-org token — the org is baked in
        user = await db.get(User, membership.user_id)
        org = await db.get(Org, membership.org_id)
    else:
        # identity token (CLI `treg login`) or a browser session — pick the org via X-Treg-Org
        user = (await _user_from_identity_token(x_treg_token, db)) if x_treg_token else await _user_from_session(treg_session, db)
        if user is None and x_treg_token:
            # Hash-backed membership tokens reached the explicit suspended-user guard below. Keep
            # that 403 contract when a new signed Default token identifies the same suspended user;
            # a token-version mismatch remains an ordinary invalid-token 401.
            suspended_claims = sess.read_identity_claims(x_treg_token)
            suspended_user = (
                await db.get(User, suspended_claims["uid"])
                if suspended_claims is not None else None
            )
            if (
                suspended_user is not None
                and suspended_user.suspended
                and suspended_claims["tv"] == suspended_user.token_version
            ):
                raise HTTPException(status_code=403, detail="account suspended")
        if user is None:
            raise HTTPException(status_code=401, detail="invalid token" if x_treg_token else "not authenticated")
        # A team-pinned identity token (org baked into its claim) resolves as a BARE bearer where no
        # header can travel — an MCP server's Authorization. New typed Default keys make that claim
        # authoritative. The header-first rule survives only for untyped credentials minted before
        # scopes shipped, keeping a rolling deploy from revoking existing CLI/MCP installations.
        identity_claims = sess.read_identity_claims(x_treg_token) if x_treg_token else None
        if (identity_claims or {}).get("scope") == sess.BOOTSTRAP_SCOPE:
            raise HTTPException(status_code=403, detail=(
                "finish team setup first — this temporary login token cannot access team resources"))
        claimed_org = (identity_claims or {}).get("org", "")
        # New Default keys are explicitly team-scoped, so their signed team is authoritative.
        # Older unmarked identity tokens retain the header-first behavior during migration.
        if (identity_claims or {}).get("scope") == sess.TEAM_SCOPE:
            if x_treg_org:
                requested = await _resolve_org(x_treg_org, db)
                claimed = await _resolve_org(claimed_org, db)
                if requested is None or claimed is None or requested.id != claimed.id:
                    raise HTTPException(status_code=403, detail=(
                        "this key belongs to another team — use this team's Default key; "
                        "if you use the treg CLI, run `treg update`, then `treg login`"
                    ))
            org_ref = claimed_org
        else:
            org_ref = x_treg_org or claimed_org
        org = await _resolve_org(org_ref, db)
        if org is None:
            # A team-pinned Default token whose team was deleted is no longer a valid credential.
            # Keep the ordinary 400 for callers that simply omitted or mistyped their team header.
            if x_treg_token and not x_treg_org and (identity_claims or {}).get("org"):
                raise HTTPException(status_code=401, detail="invalid token")
            raise HTTPException(status_code=400, detail="choose an org (send X-Treg-Org)")
        membership = (
            await db.execute(
                select(Membership).where(Membership.user_id == user.id, Membership.org_id == org.id)
            )
        ).scalar_one_or_none()
        if membership is None:
            # Membership removal revokes and detaches its keys for audit. A signed Default token has
            # no hash to find, so consult that retained control row before answering as though this
            # identity had never belonged to the team.
            if x_treg_token and identity_claims and identity_claims.get("org") == org.slug:
                retained_default = (await db.execute(select(ApiKey).where(
                    ApiKey.org_id == org.id,
                    ApiKey.membership_id.is_(None),
                    ApiKey.identity_label == user.email,
                    ApiKey.kind == managed_keys.DEFAULT_KIND,
                ).order_by(ApiKey.id.desc()))).scalars().first()
                _require_active_key(retained_default)
            raise HTTPException(status_code=403, detail="not a member of this org")
        # Browser sessions are not API keys. A signed identity bearer maps to this membership's
        # default control, except the short-lived typed identity token used only by MCP OAuth.
        claims = identity_claims
        oauth_bridge = bool(
            claims
            and claims.get("aud") == sess.IDENTITY_AUDIENCE
            and claims.get("exp") is not None
        )
        if x_treg_token and not oauth_bridge:
            api_key = await managed_keys.ensure_default_key(db, membership, user)
            _require_active_key(api_key)
            # Only team-pinned signed credentials are dashboard Default keys. Org-less login and
            # short-lived bridge tokens are intentionally outside per-team Default rotation.
            if claims and claims.get("org") and api_key is not None:
                if int(claims.get("kg", 0)) != api_key.default_generation:
                    raise HTTPException(status_code=401, detail="revoked key")
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
    managed_keys.touch(api_key)
    return Caller(membership=membership, user=user, org=org, api_key=api_key)


async def require_superadmin(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_admin_session),
) -> str:
    """Cross-tenant gate for /admin/*. Authorized by the env admin token, a token whose user is
    is_superadmin, OR a session whose user is is_superadmin. Returns a principal (for audit).

    On the admin pool, and it must name the same dependency callable its handlers do — FastAPI
    caches dependencies per request by identity, so a gate on `get_session` would put admin traffic
    back on the API pool through the back door."""
    admin = get_settings().admin_token
    if x_treg_token and admin and hmac.compare_digest(x_treg_token, admin):
        await db.commit()
        return "env-admin"
    user: User | None = None
    if x_treg_token:
        m, _ = await _membership_and_key_by_token(x_treg_token, db)
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


# ---- the email-domain blocklist ------------------------------------------------------------------
# Entirely configuration: `TREG_BLOCKED_EMAIL_DOMAINS` and nothing else. An unset variable blocks
# nothing, which is the default. Two rules:
#   - match the DOMAIN only, never the whole address. Matching the address false-flags real people
#     whose USERNAME happens to contain a listed string.
#   - walk parent domains, whole labels off the front only and never the bare last label, because
#     registering `<random>.<listed-domain>` is otherwise a one-line bypass. The walk is safe
#     because no entry can be a bare public suffix: `config._blocked_email_domains` drops dotless
#     entries, so a typed `com` cannot refuse the world.
# A PURE classifier: refusing, logging and skipping a perk are the caller's decisions
# (`application.signup.blocked_email`).
#
# There is deliberately no list in the code. A blocklist is a speed bump — a new domain costs the
# other side minutes — so its only value is being editable in the same minutes, which a deploy is
# not. Substring rules on the domain were tried and removed: measured against a public
# throwaway-domain corpus they matched 0.17% of it, added nothing over the exact entries, and
# refused a real company whose domain merely contained one of the strings.


def _email_domain(email: str) -> str:
    """The lowercased domain part of an address, "" when there is none. The ONLY part of an address
    the blocklist ever looks at."""
    return _norm_email(email).rpartition("@")[2]


def _is_blocked_email(email: str) -> bool:
    """Pure classifier: is this address on a configured domain, or on a subdomain of one? An unset
    `TREG_BLOCKED_EMAIL_DOMAINS` blocks nothing."""
    blocked = get_settings().blocked_email_domain_set
    if not blocked:
        return False
    domain = _email_domain(email)
    if not domain:
        return False
    labels = domain.split(".")
    for i in range(len(labels) - 1):  # every parent domain, never the bare last label
        if ".".join(labels[i:]) in blocked:
            return True
    return False
