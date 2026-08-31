"""Team signup and governance HTTP routes."""

import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import crypto, email as email_sender, health, localrun
from .. import providers as _providers
from ..application.onboard import demo as demo_seed
from ..application import signup as signup_use_cases
from ..caller_metadata import TAG_DEFAULT, _MAX_BUDGET_DIMS, _META_KEY_RE, _client_of, _norm_client
from ..config import get_settings
from ..domain import money as ledger
from ..domain.governance import budgets as budget_policy
from ..domain.governance import access as access_policy
from ..domain.governance import usage as usage_policy
from ..domain.governance.budgets import (
    _META_MAX_KEYS,
    _budget_dims_of,
    _effective_daily_cap,
    _primary_dim_of,
)
from ..domain.governance.publicdemo import PUBLIC_DEMO_RATE_MAX, PUBLIC_DEMO_RATE_WINDOW_S
from ..domain.governance import teams
from ..domain.governance.teams import _slugify
from ..domain.identity.access import (
    AGENT_DOMAIN,
    PUBLIC_DEMO_DOMAIN,
    Caller,
    _is_agent_email,
    _is_machine_email,
    _norm_email,
    _role_at_least,
    require_identity,
    require_member,
)
from ..infra.db import get_session
from ..models import (
    ROLE_RANK,
    AdConversion,
    Bundle,
    CallRecord,
    CapabilityPin,
    CreditBlock,
    DenyRule,
    Hold,
    IdempotentCall,
    Invite,
    LedgerEntry,
    Membership,
    OAuthCode,
    OAuthGrant,
    OAuthRefresh,
    Org,
    PendingOAuth,
    Project,
    RunRecord,
    Secret,
    TagBudget,
    TagSpend,
    Tool,
    ToolRequest,
    User,
)
from ..timeutil import as_naive as _as_naive
from ..timeutil import utcnow_naive as _utcnow_naive


_day_start_utc = usage_policy._day_start_utc
count_today = usage_policy.count_today
_deny_match = access_policy._deny_match
_org_deny_rules = access_policy._org_deny_rules
from .auth_helpers import _is_https
from .signup_cookies import REFERRAL_COOKIE


INVITE_TTL_DAYS = 7  # invite codes are one-time AND expire after this many days

# list_cli_deny keeps its original lazy relative import after moving one package level deeper.
sys.modules.setdefault("treg.routers.providers", _providers)


class UserIn(BaseModel):
    email: str
    webhook_url: str | None = None


class OrgIn(BaseModel):
    name: str


class InviteIn(BaseModel):
    email: str
    role: str = "member"
    expires_days: int = INVITE_TTL_DAYS
    # Access to seed onto the membership on accept: tool_access None = all tools, a list = the allowed
    # tool names; local_run may be turned off. Both default to the unrestricted state.
    tool_access: list[str] | None = None
    project_access: list[str | int] | None = None  # None = the whole org; slugs/ids = the scoped set
    local_run_enabled: bool = True
    landing: str | None = None  # a shared detail page ("/app/skills/<name>") to land on after sign-in


# Landing must be one of OUR detail paths — a path-only allowlist so an emailed invite link can never
# become an open redirect (no scheme, no host, no traversal, single trailing name segment).
_LANDING_RE = re.compile(r"^/app/(skills|tools)/[A-Za-z0-9][A-Za-z0-9._%-]*$")


class AcceptIn(BaseModel):
    code: str
    email: str


def _require_admin_of(org_id: int, caller: Caller) -> None:
    """The caller must be acting with THIS org's token (token = a membership) and be admin+."""
    if caller.org_id != org_id or not _role_at_least(caller.role, "admin"):
        raise HTTPException(status_code=403, detail="admin role in this org is required")


async def _known_tool_names(org_id: int, db: AsyncSession) -> set[str]:
    rows = (await db.execute(select(Tool.name).where(Tool.org_id == org_id))).all()
    return {r[0] for r in rows}


async def _known_access_names(org_id: int, db: AsyncSession) -> set[str]:
    """Everything an access list may name: tool names (the call/run gate) plus bundle names (the
    skill-visibility gate) — so a recipe-only skill can be granted even though it has no tool."""
    bundles = (await db.execute(select(Bundle.name).where(Bundle.org_id == org_id))).all()
    return await _known_tool_names(org_id, db) | {r[0] for r in bundles}


def _normalize_tool_access(names: list[str] | None, known: set[str]) -> list[str] | None:
    """Validate a requested access list against the org's tools + skills. None → None (all). A list
    must name only real tools/skills (else 422). A list covering EVERYTHING collapses to None."""
    if names is None:
        return None
    unknown = [t for t in names if t not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown tool/skill(s): {', '.join(sorted(set(unknown)))}")
    chosen = set(names)
    return None if chosen >= known and known else sorted(chosen)  # everything checked → 'all' (NULL)


async def _normalize_project_access(
    refs: list[str | int] | None, org_id: int, db: AsyncSession
) -> list[int] | None:
    """Turn slugs/ids into the stored list of project IDS, mirroring `_normalize_tool_access`:
    validate against the org's own projects (422 on unknown — never silently ignore a typo) and
    **collapse an all-projects selection back to NULL**, so a fully-scoped member keeps
    auto-inheriting projects created later."""
    if refs is None:
        return None
    known = (await db.execute(select(Project).where(Project.org_id == org_id))).scalars().all()
    by_slug = {p.slug: p.id for p in known}
    by_id = {p.id for p in known}
    ids: set[int] = set()
    for ref in refs:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            pid = int(ref)
            if pid not in by_id:
                raise HTTPException(status_code=422, detail=f"unknown project {ref!r} in this team")
            ids.add(pid)
        elif ref in by_slug:
            ids.add(by_slug[ref])
        else:
            raise HTTPException(status_code=422, detail=f"unknown project {ref!r} in this team")
    if known and ids >= by_id:
        return None  # every project selected = unrestricted, so store it as such
    return sorted(ids)


class RoleIn(BaseModel):
    role: str


class CapIn(BaseModel):
    daily_call_cap: int  # per-user, per-day usage cap for the member; -1 = unlimited


class AccessIn(BaseModel):
    # tool_access: None = all tools (clear the restriction); a list = the ONLY tool names allowed.
    tool_access: list[str] | None = None
    # project_access: None = the whole org; a list of project SLUGS or IDS = the only projects allowed.
    # Accepts slugs because that's the human handle; stored as ids (see _normalize_project_access).
    project_access: list[str | int] | None = None
    local_run_enabled: bool = True


# Every model carrying an `org_id`. Deleting the org without clearing these leaves rows pointing at
# a row that no longer exists, and the delete fails with a 500 at the foreign key.
#
# This list has to be kept in step with the schema, and twice it was not: the money tables arrived
# with the prepaid balance and `CapabilityPin` with capability pins, and neither was added here. The
# effect was invisible until someone tried it — since every NEW team is granted $1.00, every team has
# a CreditBlock, so NO team could be deleted at all. `test_org_delete_clears_every_org_scoped_table`
# now walks the models module and fails if a new one is ever missed, rather than trusting this list.
#
# Order matters: LedgerEntry references a CreditBlock, so it goes first.
_ORG_SCOPED_MODELS = (
    Tool, Secret, Bundle, PendingOAuth, CallRecord, RunRecord, Invite, DenyRule, Project,
    CapabilityPin,
    TagBudget,
    TagSpend,  # before the money tables it attributes: its rows reference a Hold that is about to go
    LedgerEntry, Hold, CreditBlock,
    OAuthCode, OAuthRefresh,   # grants naming a team that no longer exists
    IdempotentCall,            # a remembered answer belongs to the team that paid for it
    ToolRequest,  # attribution rows go with the team; anonymous filings carry no org_id and stay
    AdConversion,  # pending Google Ads conversions belong to the team they'd be attributed to
    Membership,   # last: it is what makes the caller a member of the org being deleted
)


async def _cascade_delete_org(org: Org, db: AsyncSession) -> None:
    """Delete every org-scoped row then the org. Shared by owner delete_org + admin force-delete."""
    # OAuthGrant names its mutable team `current_org_id` to distinguish family authority from the
    # immutable `OAuthRefresh.org_id` provenance. A family can name this team on EITHER side: after
    # a move, only a retired provenance row still names the former team. Deleting just that row
    # destroys the replay evidence while leaving the live family authorised elsewhere, so a stolen
    # old token becomes "unknown" instead of revoking every descendant. Revoke the union of both
    # paths; preserving historical provenance across team deletion would need a nullable/soft FK.
    authority_grants = (await db.execute(select(OAuthGrant).where(
        OAuthGrant.current_org_id == org.id))).scalars().all()
    provenance_families = (await db.execute(select(OAuthRefresh.family_id).where(
        OAuthRefresh.org_id == org.id))).scalars().all()
    family_ids = {grant.family_id for grant in authority_grants} | set(provenance_families)
    if family_ids:
        # Delete the WHOLE family, including rows issued under other teams. Keeping only the live
        # destination token would be exactly the partial revocation that reuse detection forbids.
        for token in (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.family_id.in_(family_ids)))).scalars().all():
            await db.delete(token)
        grants = (await db.execute(select(OAuthGrant).where(
            OAuthGrant.family_id.in_(family_ids)))).scalars().all()
    else:
        grants = []
    for grant in grants:
        await db.delete(grant)
    await db.flush()
    for model in _ORG_SCOPED_MODELS:
        for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
            await db.delete(r)
        await db.flush()   # honour the ordering above rather than leaving it to the unit of work
    await db.delete(org)


def _require_owner_of(org_id: int, caller: Caller) -> None:
    """Owner-only actions (change roles, delete org). Token is org-scoped, so must match."""
    if caller.org_id != org_id or caller.role != "owner":
        raise HTTPException(status_code=403, detail="owner role in this org is required")


async def _count_owners(org_id: int, db: AsyncSession) -> int:
    rows = (
        await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.role == "owner"))
    ).scalars().all()
    return len(rows)


async def _used_today_by_user(db: AsyncSession, org_id: int) -> dict[str, int]:
    """{user_email: events today} for every member of the org — one grouped COUNT per table, so the
    members list gets everyone's usage without an N+1 fan-out. Spans all kinds (calls + local + server)."""
    since = _day_start_utc()
    counts: dict[str, int] = {}
    for email, n in (await db.execute(select(CallRecord.user_email, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since).group_by(CallRecord.user_email))).all():
        counts[email] = counts.get(email, 0) + n
    for email, n in (await db.execute(select(RunRecord.user_email, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.user_email))).all():
        counts[email] = counts.get(email, 0) + n
    return counts


async def _drop_member_deny_rules(db: AsyncSession, user_id: int, org_id: int | None = None) -> int:
    """Delete the member-scoped rules that named a member/agent who is going away — the caller they
    were written for no longer exists, so the rule can never fire again. Left behind, they show up in
    the Policy table as a row naming a user id the team can no longer see or clean up. Mirrors how
    `delete_project` sweeps the id it deletes out of every `project_access`.

    `org_id` set = that org only (the member left THIS team but may still be in others). `org_id`
    None = every org, for when the USER row itself is deleted — `DenyRule.user_id` is a foreign key,
    so a surviving rule would dangle, which Postgres rejects outright (SQLite does not enforce it by
    default, which is why only a real deployment would have shown this).

    ORG-wide rules (`user_id` NULL) are untouched: they are about the team, not about one caller.
    The caller commits — this only stages the deletes, so it composes with the removal itself."""
    q = select(DenyRule).where(DenyRule.user_id == user_id)
    if org_id is not None:
        q = q.where(DenyRule.org_id == org_id)
    stale = (await db.execute(q)).scalars().all()
    for rule in stale:
        await db.delete(rule)
    return len(stale)


async def _enforce_deny(
    caller: Caller, url: str, method: str, db: AsyncSession, tool_project_id: int | None = None
) -> None:
    """Block a call the org's policy forbids. Deliberately applies to EVERY role including owner: a
    deny rule is a guardrail, not a permission tier — an owner who disagrees deletes the rule rather
    than quietly bypassing it. The refusal names the rule, mirroring `localrun.check_deny`'s
    "a refusal can name its source".

    `tool_project_id` = the project of the tool this call goes through (every enforcement point has
    resolved a Tool by then). A project-scoped rule (`project_id` set) fires only on that project's
    tools; an org-wide-tool call (`tool_project_id` None) is never caught by one."""
    try:
        await access_policy.enforce_deny(caller, url, method, db, tool_project_id)
    except access_policy.AccessPolicyError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


async def _usage_rollup(db: AsyncSession, org_id: int, since: datetime) -> dict:
    """Aggregate usage since `since` into by-user (with a per-kind split), by-tool, by-day, and totals.
    CallRecord carries `kind` ("call"/"local_run"); every RunRecord is a "server_run". Pure GROUP BY —
    no request/response bodies are read (we don't store them). See docs/USAGE-METERING-PLAN.md."""
    KINDS = ("call", "local_run", "server_run")
    totals = {k: 0 for k in KINDS}
    users: dict[str, dict] = {}

    def _bump(email: str, kind: str, n: int) -> None:
        u = users.setdefault(email, {"user_email": email, **{k: 0 for k in KINDS}})
        u[kind] += n
        totals[kind] += n

    for email, kind, n in (await db.execute(select(CallRecord.user_email, CallRecord.kind, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since
    ).group_by(CallRecord.user_email, CallRecord.kind))).all():
        _bump(email, kind if kind in KINDS else "call", n)  # guard an unexpected kind into "call"
    for email, n in (await db.execute(select(RunRecord.user_email, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.user_email))).all():
        _bump(email, "server_run", n)

    by_user = sorted(
        ({**u, "total": sum(u[k] for k in KINDS)} for u in users.values()),
        key=lambda r: -r["total"])
    totals["total"] = sum(totals[k] for k in KINDS)

    tools: dict[str, int] = {}
    for name, n in (await db.execute(select(CallRecord.tool_name, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since).group_by(CallRecord.tool_name))).all():
        tools[name] = tools.get(name, 0) + n
    for name, n in (await db.execute(select(RunRecord.bundle_name, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.bundle_name))).all():
        tools[name] = tools.get(name, 0) + n
    by_tool = sorted(({"name": k, "total": v} for k, v in tools.items()), key=lambda r: -r["total"])

    days: dict[str, int] = {}  # func.date() → 'YYYY-MM-DD' on sqlite, a date on Postgres; str() both
    for tbl in (CallRecord, RunRecord):
        for d, n in (await db.execute(select(func.date(tbl.created_at), func.count()).where(
                tbl.org_id == org_id, tbl.created_at >= since).group_by(func.date(tbl.created_at)))).all():
            days[str(d)] = days.get(str(d), 0) + n
    by_day = sorted(({"day": k, "total": v} for k, v in days.items()), key=lambda r: r["day"])

    # What those calls COST the team on treg's own keys — read from the ledger (the authority on money)
    # rather than from the audit rows, which are fire-and-forget and may be incomplete. One aggregate.
    spend = await ledger.spend_since(db, org_id, since)
    return {"totals": totals, "by_user": by_user, "by_tool": by_tool, "by_day": by_day, "spend": spend}


class OrgSettingsIn(BaseModel):
    daily_cap_micro: int | None = None
    platform_overflow: bool | None = None  # False = opt out of the overflow relay (ops/capacity.md)
    budget_dims: list[str] | None = None
    primary_dim: str | None = None


class TagBudgetIn(BaseModel):
    daily_cap_micro: int | None = None
    monthly_cap_micro: int | None = None
    calls_per_day: int | None = None
    status: str | None = None
    note: str | None = None


def _tag_budget_view(row: TagBudget) -> dict:
    return {"dim": row.dim, "val": row.val, "is_default": row.val == TAG_DEFAULT,
            "daily_cap_micro": row.daily_cap_micro,
            "monthly_cap_micro": row.monthly_cap_micro, "calls_per_day": row.calls_per_day,
            "status": row.status, "note": row.note,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


class ProjectIn(BaseModel):
    name: str


def _project_view(p: Project, tool_count: int | None = None) -> dict:
    out = {"id": p.id, "name": p.name, "slug": p.slug, "created_by": p.created_by,
           "created_at": p.created_at}
    if tool_count is not None:
        out["tool_count"] = tool_count
    return out


async def _resolve_project(ref: str | int | None, org_id: int, db: AsyncSession) -> Project | None:
    """A project by slug or id, scoped to the org (404 across orgs). None/'' = org-wide."""
    if ref is None or ref == "":
        return None
    q = select(Project).where(Project.org_id == org_id)
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        q = q.where(Project.id == int(ref))
    else:
        q = q.where(Project.slug == _slugify(str(ref)))
    project = (await db.execute(q)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project {ref!r} in this team")
    return project


PROXY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class DenyRuleIn(BaseModel):
    host: str = ""  # a bare netloc or a full URL (we take its host)
    path_prefix: str = ""
    method: str = ""
    user_id: int | None = None  # None = the whole org; set = only that member/agent
    project_id: int | None = None  # None = any tool; set = only calls through that project's tools
    note: str = ""


def _deny_view(r: DenyRule) -> dict:
    return {"id": r.id, "host": r.host, "path_prefix": r.path_prefix, "method": r.method,
            "user_id": r.user_id, "scope": "org" if r.user_id is None else "member",
            "project_id": r.project_id,
            "verdict": r.verdict, "note": r.note, "created_by": r.created_by,
            "created_at": r.created_at}


_SIGNUP_HTTP_ERRORS = {
    "machine_identity": (403, "this address cannot be used to sign in"),
    "unsafe_webhook": (422, "webhook_url must be a public http(s) URL"),
    "email_exists": (409, "email already registered"),
    "sandbox_user": (403, (
        "the demo sandbox can't create a real team — sign in with GitHub, Google, or email to make one"
    )),
    "slug_conflict": (409, "could not allocate a unique org slug — retry"),
}


def _signup_http_error(exc: signup_use_cases.SignupError) -> HTTPException:
    status_code, detail = _SIGNUP_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


# The app alias preserves the handlers' @app decorators and the ordered attachment convention.
app = APIRouter()
signup_router = app


@app.post("/users")
async def register_user(body: UserIn, request: Request) -> dict:
    try:
        return await signup_use_cases.register_user(
            email=body.email,
            webhook_url=body.webhook_url,
            ad_cookie=request.cookies.get("treg_ad") or "",
            utm_cookie=request.cookies.get("treg_utm") or "",
            referral_cookie=request.cookies.get(REFERRAL_COOKIE) or "",
        )
    except signup_use_cases.SignupError as exc:
        raise _signup_http_error(exc) from exc


@app.post("/orgs")
async def create_org(
    body: OrgIn, request: Request,
    user: User = Depends(require_identity),
) -> dict:
    try:
        return await signup_use_cases.create_org(
            user=user,
            name=body.name,
            ad_cookie=request.cookies.get("treg_ad") or "",
            utm_cookie=request.cookies.get("treg_utm") or "",
            referral_cookie=request.cookies.get(REFERRAL_COOKIE) or "",
        )
    except signup_use_cases.SignupError as exc:
        raise _signup_http_error(exc) from exc


app = APIRouter()
org_entry_router = app


@app.get("/orgs")
async def list_orgs(
    user: User = Depends(require_identity),
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await teams.list_user_orgs(
        user_id=user.id,
        x_treg_token=x_treg_token,
        x_treg_org=x_treg_org,
        db=db,
    )


app = APIRouter()
invite_entry_router = app


@app.post("/orgs/{org_id}/invites")
async def create_invite(
    org_id: int, body: InviteIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_admin_of(org_id, caller)
    if body.role not in ("viewer", "member", "admin"):
        raise HTTPException(status_code=422, detail="role must be 'viewer', 'member', or 'admin'")
    # Role assignment is owner-only (see set_member_role); the invite door must honour the same
    # boundary or an admin could mint fellow admins that they can't otherwise create.
    if body.role == "admin" and caller.role != "owner":
        raise HTTPException(status_code=403, detail="only an owner can invite an admin")
    email = _norm_email(body.email)
    # An email already in the org can't accept a new invite (accept would 409) — reject the dead-end up front.
    existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing_user is not None:
        m = (await db.execute(select(Membership).where(
            Membership.user_id == existing_user.id, Membership.org_id == org_id
        ))).scalar_one_or_none()
        if m is not None:
            raise HTTPException(status_code=409, detail="that email is already a member of this org")
    # Supersede any prior pending invite for this email so there's exactly one live code per invitee
    # (re-inviting used to stack duplicate pending rows that all point at the same seat).
    for prior in (await db.execute(select(Invite).where(
        Invite.org_id == org_id, Invite.email == email, Invite.status == "pending"
    ))).scalars().all():
        await db.delete(prior)
    days = max(1, min(body.expires_days, 3650))  # clamp BOTH ends — a huge value overflows datetime → 500
    expires_at = _utcnow_naive() + timedelta(days=days)
    tool_access = _normalize_tool_access(body.tool_access, await _known_access_names(org_id, db))
    project_access = await _normalize_project_access(body.project_access, org_id, db)
    if body.landing is not None and not _LANDING_RE.match(body.landing):
        raise HTTPException(status_code=422, detail="landing must be a detail path like /app/skills/<name>")
    code = crypto.new_token()
    # A SECOND secret for the email link only. The admin gets `code` back (out-of-band relay) so the
    # code can never be a sign-in factor; `email_token` is never returned here — only the inbox sees
    # it, which is what lets /auth/invite-signin treat it like an emailed OTP and mint a session.
    email_token = crypto.new_token()
    invite = Invite(
        org_id=org_id, email=email, role=body.role,
        code_hash=crypto.hash_token(code), email_token_hash=crypto.hash_token(email_token),
        invited_by=caller.email, expires_at=expires_at,
        tool_access=tool_access, project_access=project_access,
        local_run_enabled=body.local_run_enabled, landing=body.landing,
    )
    db.add(invite)
    await db.commit()
    org = await db.get(Org, org_id)  # for the invite email's team name
    if not email.endswith("@" + demo_seed.DEMO_DOMAIN):  # don't email the onboarding's fake teammate domain
        scheme = "https" if _is_https(request) else request.url.scheme
        host = request.headers.get("host", "")
        shared = ""  # share-born invite → the email leads with what was shared
        if body.landing:
            kind, _, name = body.landing.removeprefix("/app/").partition("/")
            shared = f'the {"skill" if kind == "skills" else "tool"} “{name}”'
        await email_sender.send_invite(  # best-effort; the code is also returned for out-of-band relay
            email, caller.email, (org.name if org else email), body.role, code, email_token,
            expires_at.isoformat(), link_base=(f"{scheme}://{host}" if host else ""), shared=shared,
        )
    return {"code": code, "email": email, "role": body.role, "org_id": org_id,
            "expires_at": expires_at.isoformat()}  # email_token deliberately NOT returned (inbox-only)


@app.post("/invites/accept")
async def accept_invite(body: AcceptIn, db: AsyncSession = Depends(get_session)) -> dict:
    # Open endpoint, protected by the unguessable one-time code. Registers the user if new,
    # joins them to the org, and mints their own org-scoped token (the admin never sees it).
    invite = (
        await db.execute(select(Invite).where(Invite.code_hash == crypto.hash_token(body.code)))
    ).scalar_one_or_none()
    email = _norm_email(body.email)
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invalid or already-used invite code")
    if invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive():
        raise HTTPException(status_code=410, detail="invite code expired")
    if invite.email != email:
        raise HTTPException(status_code=403, detail="this invite is for a different email")
    org = await db.get(Org, invite.org_id)
    if org is not None and org.suspended:  # don't let anyone join a platform-locked org
        raise HTTPException(status_code=403, detail="org suspended")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and user.suspended:  # a banned user must not accrue new memberships
        raise HTTPException(status_code=403, detail="account suspended")
    if user is None:
        # Brand-new user → create the user only. Accepting the invite below IS their first team
        # (no auto personal org — consistent with the login doors).
        user = User(email=email)
        db.add(user)
        await db.flush()
    existing = (
        await db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == invite.org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this org")
    token = crypto.new_token()
    db.add(Membership(user_id=user.id, org_id=invite.org_id, role=invite.role, token_hash=crypto.hash_token(token),
                      tool_access=invite.tool_access, project_access=invite.project_access,
                      local_run_enabled=invite.local_run_enabled))
    invite.status = "accepted"
    try:
        await db.commit()  # a concurrent double-accept trips uq_membership_user_org — 409, not 500
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already a member of this org")
    org = await db.get(Org, invite.org_id)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": invite.role, "token": token}


@app.get("/invites/mine")
async def my_invites(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every pending invite addressed to MY email — the code-free door. Proving my email (via any
    login method) is enough to see these; the invite code becomes a shortcut, not a requirement."""
    rows = (
        await db.execute(select(Invite).where(Invite.email == user.email, Invite.status == "pending")
                         .order_by(Invite.created_at.desc()))  # newest first — the invite you just clicked
    ).scalars().all()
    now = _utcnow_naive()
    orgs = {  # batch the org lookup (was one db.get per invite)
        o.id: o for o in (await db.execute(
            select(Org).where(Org.id.in_([inv.org_id for inv in rows]))
        )).scalars().all()
    }
    out = []
    for inv in rows:
        if inv.expires_at is not None and _as_naive(inv.expires_at) < now:
            continue
        org = orgs.get(inv.org_id)
        if org is None or org.suspended:  # a platform-locked org isn't joinable — don't surface it
            continue
        out.append({
            "id": inv.id, "org": org.slug, "org_id": org.id, "name": org.name, "role": inv.role,
            "invited_by": inv.invited_by, "landing": inv.landing,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return out


def _validate_tag_pair(key: str, value: str, *, where: str = "tag") -> tuple[str, str]:
    try:
        return budget_policy._validate_tag_pair(key, value, where=where)
    except budget_policy.BudgetPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _tag_budget(
    db: AsyncSession, org_id: int, dim: str, val: str, create: bool = False,
) -> TagBudget | None:
    try:
        result = await budget_policy._tag_budget(db, org_id, dim, val, create=create)
    except budget_policy.BudgetPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if result.created:
        await db.commit()
    return result.row


app = APIRouter()
invite_management_router = app


@app.post("/invites/{invite_id}/accept")
async def accept_my_invite(
    invite_id: int, user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Accept an invite addressed to my already-proven email — no code needed (the identity token
    proves the email). The code path (`POST /invites/accept`) stays for out-of-band joins."""
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invalid or already-used invite")
    if invite.email != user.email:
        raise HTTPException(status_code=403, detail="this invite is for a different email")
    if invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive():
        raise HTTPException(status_code=410, detail="invite expired")
    org = await db.get(Org, invite.org_id)
    if org is not None and org.suspended:  # don't let anyone join a platform-locked org
        raise HTTPException(status_code=403, detail="org suspended")
    existing = (
        await db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == invite.org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this org")
    token = crypto.new_token()  # return the org-scoped token (was minted-then-discarded → an unusable membership)
    db.add(Membership(
        user_id=user.id, org_id=invite.org_id, role=invite.role, token_hash=crypto.hash_token(token),
        tool_access=invite.tool_access, project_access=invite.project_access,
        local_run_enabled=invite.local_run_enabled,
    ))
    invite.status = "accepted"
    try:
        await db.commit()  # a concurrent double-accept trips uq_membership_user_org — 409, not 500
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already a member of this org")
    org = await db.get(Org, invite.org_id)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": invite.role, "token": token}


@app.get("/orgs/{org_id}/invites")
async def list_invites(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    await health.gc_expired_invites(db, org_id)  # purge dead codes so the list shows only live ones
    await db.commit()
    rows = (
        await db.execute(select(Invite).where(Invite.org_id == org_id, Invite.status == "pending"))
    ).scalars().all()
    return [
        {
            "id": i.id, "email": i.email, "role": i.role, "invited_by": i.invited_by,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "created_at": i.created_at.isoformat(),
        }
        for i in rows
    ]


@app.delete("/orgs/{org_id}/invites/{invite_id}")
async def revoke_invite(
    org_id: int, invite_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_admin_of(org_id, caller)
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.org_id != org_id or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invite not found")  # can't "revoke" an accepted/consumed one
    await db.delete(invite)  # the code can no longer be accepted
    await db.commit()
    return {"revoked_invite": invite_id}


app = APIRouter()
member_list_router = app


@app.get("/orgs/{org_id}/members")
async def list_members(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    memberships = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    users = {  # batch the user lookup (was one db.get per member)
        u.id: u for u in (await db.execute(
            select(User).where(User.id.in_([m.user_id for m in memberships]))
        )).scalars().all()
    }
    used = await _used_today_by_user(db, org_id)  # one grouped query, not N+1
    out: list[dict] = []
    for m in memberships:
        user = users.get(m.user_id)
        if user is not None:
            out.append({"user_id": user.id, "email": user.email, "role": m.role,
                        "daily_call_cap": m.daily_call_cap, "used_today": used.get(user.email, 0),
                        "tool_access": m.tool_access, "project_access": m.project_access,
                        "local_run_enabled": m.local_run_enabled,
                        # so the dashboard can separate people from machines in one roster —
                        # agents carry their short name + owner, so the UI never shows the raw
                        # machine address and can group each agent under its creator
                        "is_agent": _is_agent_email(user.email),
                        "name": (_agent_name(caller.org, user.email)
                                 if _is_agent_email(user.email) else None),
                        "created_by": m.created_by})
    return out


app = APIRouter()
member_management_router = app


@app.patch("/orgs/{org_id}/members/{user_id}/cap")
async def set_member_cap(
    org_id: int, user_id: int, body: CapIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set a member's per-user daily usage cap (admin/owner). `-1` = unlimited; any other negative is
    rejected. Separate from role (owner-only) — capping is a management action, not a privilege change."""
    _require_admin_of(org_id, caller)
    if body.daily_call_cap < -1:
        raise HTTPException(status_code=422, detail="daily_call_cap must be -1 (unlimited) or >= 0")
    membership = (await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    membership.daily_call_cap = body.daily_call_cap
    await db.commit()
    return {"user_id": user_id, "org_id": org_id, "daily_call_cap": body.daily_call_cap}


@app.patch("/orgs/{org_id}/members/{user_id}/access")
async def set_member_access(
    org_id: int, user_id: int, body: AccessIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set which tools a member may call/run (`tool_access`: None = all, else the allowed names) and
    whether they may run locally (`local_run_enabled`). Admin/owner only; an owner can't be restricted."""
    _require_admin_of(org_id, caller)
    membership = (await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner":
        raise HTTPException(status_code=403, detail="an owner always has full access; it can't be restricted")
    membership.tool_access = _normalize_tool_access(body.tool_access, await _known_access_names(org_id, db))
    # Only touch the project scope when the caller actually SENT the field. Without this, any client
    # that PATCHes just tool_access or local_run_enabled (the dashboard's local-run toggle does exactly
    # that) would silently clear the member's project scoping, because the field defaults to None.
    if "project_access" in body.model_fields_set:
        membership.project_access = await _normalize_project_access(body.project_access, org_id, db)
    membership.local_run_enabled = body.local_run_enabled
    await db.commit()
    return {"user_id": user_id, "org_id": org_id, "tool_access": membership.tool_access,
            "project_access": membership.project_access,
            "local_run_enabled": membership.local_run_enabled}


@app.get("/usage/me")
async def my_usage(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's own usage today + cap for the active org — so a member sees 'used / cap' without
    admin access. `cap` is -1 when unlimited."""
    return {"org": caller.org.slug, "used_today": await count_today(db, caller.org_id, caller.email),
            "cap": caller.membership.daily_call_cap}


@app.delete("/orgs/{org_id}/members/{user_id}")
async def remove_member(
    org_id: int, user_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_admin_of(org_id, caller)
    membership = (
        await db.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner":  # only an owner manages owners; an admin cannot remove one
        raise HTTPException(status_code=403, detail="owners cannot be removed")
    await db.delete(membership)  # revokes that user's token for this org
    await _drop_member_deny_rules(db, user_id, org_id)
    await db.commit()
    return {"removed": user_id}


@app.patch("/orgs/{org_id}/members/{user_id}")
async def set_member_role(
    org_id: int, user_id: int, body: RoleIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_owner_of(org_id, caller)  # only an owner changes roles (incl. transferring ownership)
    if body.role not in ROLE_RANK:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ROLE_RANK)}")
    membership = (
        await db.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if body.role == "owner":
        # Owners short-circuit `_tool_allowed` and `_require_local_run`, so an owner machine identity
        # would silently bypass the tool ACL and the local-run gate placed on it.
        target = await db.get(User, user_id)
        if target is not None and _is_machine_email(target.email):
            raise HTTPException(status_code=422, detail="a machine identity cannot be an owner")
    if membership.role == "owner" and body.role != "owner" and await _count_owners(org_id, db) <= 1:
        raise HTTPException(status_code=409, detail="cannot demote the last owner — promote another owner first")
    membership.role = body.role
    await db.commit()
    return {"user_id": user_id, "role": body.role, "org_id": org_id}


@app.post("/orgs/{org_id}/leave")
async def leave_org(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    if caller.org_id != org_id:  # token is org-scoped: you leave the org whose token you present
        raise HTTPException(status_code=403, detail="use this org's token to leave it")
    if caller.role == "owner" and await _count_owners(org_id, db) <= 1:
        raise HTTPException(status_code=409, detail="you are the last owner — transfer ownership or delete the org")
    await db.delete(caller.membership)  # revokes the caller's token for this org
    await _drop_member_deny_rules(db, caller.membership.user_id, org_id)  # same sweep as remove_member
    await db.commit()
    return {"left_org": org_id}


@app.delete("/orgs/{org_id}")
async def delete_org(
    org_id: int, confirm: str = Query(..., min_length=1),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a team and everything in it (owner only). `?confirm=<slug>` must match.

    The confirmation is REQUIRED by the API, not just collected by the clients that already ask for
    it. This route is irreversible and sits one path segment above every other org route, so any
    client that normalizes `..` turns `DELETE /orgs/{id}/<anything>/..` into this call — that is how
    `treg org unpin ..` deleted a team during testing. A request arriving without the slug it is
    about to destroy is not a request anyone meant to send."""
    _require_owner_of(org_id, caller)
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    if confirm != org.slug:
        raise HTTPException(status_code=422, detail=(
            f"to delete this team, confirm with its slug: ?confirm={org.slug}"))
    await _cascade_delete_org(org, db)
    await db.commit()
    return {"deleted_org": org_id}


# ---- machine identities: the publishable demo token, and agents ----------------------------
# Both are Users on an UNROUTABLE domain, which is what makes them machines rather than people: no
# login door can ever resolve one (guarded in `_find_or_create_user`) and neither may act as a USER
# (guarded in `require_identity`). Everything else they inherit from Membership for free.
# NOTE: "agent" here is an IDENTITY — a coding agent / automation that calls treg. It is NOT the
# skill-directory table in `agents.py` (which answers "where does each coding agent keep its skills").
# The words collide, the concepts don't; kept apart deliberately.


def _public_demo_email(org: Org) -> str:
    return f"pub-{org.slug}@{PUBLIC_DEMO_DOMAIN}"


def _agent_email(org: Org, name: str) -> str:
    """Org-SCOPED on purpose: two orgs must each be able to own an agent called `deploy` without
    sharing one User row (`User.email` is unique). Sharing would mean a superadmin suspending or
    deleting one tenant's agent silently killed the other tenant's too."""
    return f"agent-{org.slug}-{_slugify(name)}@{AGENT_DOMAIN}"


def _agent_name(org: Org, email: str) -> str:
    """The friendly name back out of the address (the name isn't stored — the address IS the id)."""
    local = email.split("@", 1)[0]
    prefix = f"agent-{org.slug}-"
    return local[len(prefix):] if local.startswith(prefix) else local


app = APIRouter()
machine_identity_router = app


@app.post("/orgs/{org_id}/public-token")
async def create_public_token(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Mint (or ROTATE) the org's publishable token: flips the org to `public_demo` and returns a
    viewer-role token bound to a dedicated can't-log-in identity. Safe to print on a web page:
    the lockdown in require_member/require_identity limits it to /call + reads, /call is per-IP
    rate-limited, and calling this endpoint again replaces the token (instant revocation of the
    old one). Owner-only — publishing a credential is an org-level decision."""
    _require_owner_of(org_id, caller)
    org = caller.org
    email = _public_demo_email(org)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, demo=True)  # demo: excluded from stats; the domain can't receive mail
        db.add(user)
        await db.flush()
    token = crypto.new_token()
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
    if membership is None:
        db.add(Membership(user_id=user.id, org_id=org_id, role="viewer", token_hash=crypto.hash_token(token)))
    else:
        membership.token_hash = crypto.hash_token(token)  # rotate: the previous published token dies here
    org.public_demo = True
    await db.commit()
    return {"token": token, "org": org.slug, "role": "viewer", "email": email,
            "rate_limit": f"{PUBLIC_DEMO_RATE_MAX} calls per {PUBLIC_DEMO_RATE_WINDOW_S}s per IP",
            "note": "this token can only call this org's tools and read — safe to publish; POST again to rotate"}


@app.delete("/orgs/{org_id}/public-token")
async def delete_public_token(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Revoke the publishable token and lift the org's public_demo lockdown."""
    _require_owner_of(org_id, caller)
    org = caller.org
    user = (await db.execute(select(User).where(User.email == _public_demo_email(org)))).scalar_one_or_none()
    if user is not None:
        membership = (await db.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
        if membership is not None:
            await db.delete(membership)
    org.public_demo = False
    await db.commit()
    return {"public_token_revoked": True, "org": org.slug}


# ---- agents: a member identity for a machine caller ----------------------------------------
# An agent is JUST a Membership whose user lives on AGENT_DOMAIN, which is why this needs no new
# table and no migration: it inherits every per-member control already in place — `daily_call_cap`
# (enforced by `_enforce_daily_cap`), `tool_access` (`_require_tool_access`, on the proxy AND both run
# tiers), `local_run_enabled`, and per-identity audit, since `CallRecord.user_email` already stamps
# every call. Giving the agent its own identity is what makes all of that per-agent.
class AgentIn(BaseModel):
    name: str
    role: str = "member"  # never "owner" (see below); "admin" is owner-granted only
    daily_call_cap: int = -1  # -1 = unlimited, mirroring set_member_cap
    tool_access: list[str] | None = None  # None = every tool, mirroring set_member_access
    project_access: list | None = None  # None = every project; slugs or ids, mirroring set_member_access
    local_run_enabled: bool = True
    # Set by the dashboard's "Scope this agent" promotion: the observed (member, runtime) pair this
    # agent replaces, so the detected roster can drop it while the agent lives.
    promoted_member: str | None = None
    promoted_client: str | None = None
    # Pin this token to one tag value — {"customer": "cust_A"} — for a token that will run on
    # that customer's own machine. The pin then WINS over whatever header the holder sends.
    pinned_tags: dict | None = None


@app.post("/orgs/{org_id}/agents")
async def create_agent(
    org_id: int, body: AgentIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint (or ROTATE) an agent token: a member identity for a machine caller, with its own cap, tool
    ACL and audit trail. Re-POSTing the same name rotates the token — the previous one dies here, the
    same instant-revocation idiom as the public token. Admin+; only an owner may mint an admin agent.

    On a ROTATE, a field the caller did not send is LEFT AS IT IS — a rotate replaces the token, never
    the agent's limits. Reading an absent field as its default would silently widen a scoped agent to
    every tool (`tool_access=None`), to unlimited calls (`daily_call_cap=-1`) and back to local runs
    (`local_run_enabled=True`) — the dashboard's Rotate button sends only {name, role, cap}, so this
    would fire on the ordinary path. Same shape `set_member_access` already uses for `project_access`."""
    _require_admin_of(org_id, caller)
    name = (body.name or "").strip()
    if not name or not _slugify(name):
        raise HTTPException(status_code=422, detail="name is required")
    if body.role not in ROLE_RANK:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ROLE_RANK)}")
    if body.role == "owner":
        raise HTTPException(status_code=422, detail="an agent cannot be an owner")
    if body.role == "admin" and caller.role != "owner":
        raise HTTPException(status_code=403, detail="only an owner can create an admin agent")
    if body.daily_call_cap < -1:
        raise HTTPException(status_code=422, detail="daily_call_cap must be -1 (unlimited) or >= 0")

    email = _agent_email(caller.org, name)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email)  # NOT demo=True: unlike the public token, agent traffic counts in usage
        db.add(user)
        await db.flush()
    token = crypto.new_token()
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
    # A brand-new agent takes the defaults; a rotate keeps whatever it already had unless told otherwise.
    is_new = membership is None
    sent = body.model_fields_set

    def _keep(field: str, current):
        return getattr(body, field) if (is_new or field in sent) else current

    if is_new:
        membership = Membership(user_id=user.id, org_id=org_id, role=body.role,
                                token_hash=crypto.hash_token(token), created_by=caller.email)
        db.add(membership)
    else:
        membership.token_hash = crypto.hash_token(token)  # rotate: the previous token dies here
        membership.role = _keep("role", membership.role)
    membership.daily_call_cap = _keep("daily_call_cap", membership.daily_call_cap)
    if is_new or "tool_access" in sent:  # only re-validate what the caller actually sent
        membership.tool_access = _normalize_tool_access(
            body.tool_access, await _known_access_names(org_id, db))
    if is_new or "project_access" in sent:
        membership.project_access = await _normalize_project_access(body.project_access, org_id, db)
    if is_new or "promoted_member" in sent or "promoted_client" in sent:
        member = _norm_email(body.promoted_member or "")
        client = _norm_client(body.promoted_client or "")
        membership.promoted_from = f"{member}|{client}" if member and client else ""
    membership.local_run_enabled = _keep("local_run_enabled", membership.local_run_enabled)
    pins = _keep("pinned_tags", membership.pinned_tags)
    if pins:
        if len(pins) > _META_MAX_KEYS:
            raise HTTPException(status_code=422, detail=(
                f"a token may be pinned to at most {_META_MAX_KEYS} tags"))
        # Same validation the header path applies. A pin is written straight into the tag bag by
        # `_parse_call_meta`, so an unvalidated one would reach the idempotency scope and the money
        # rows without ever passing the parser.
        pins = dict(_validate_tag_pair(k, v) for k, v in pins.items())
    membership.pinned_tags = pins or None
    await db.commit()
    return {"token": token, "name": name, "email": email, "org": caller.org.slug, "user_id": user.id,
            "role": membership.role, "daily_call_cap": membership.daily_call_cap,
            "tool_access": membership.tool_access, "project_access": membership.project_access,
            "local_run_enabled": membership.local_run_enabled,
            "pinned_tags": membership.pinned_tags,
            "note": "save this token now — it is shown once; POST the same name again to rotate it"}


@app.get("/orgs/{org_id}/agents")
async def list_agents(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every agent identity in the org, with its limits and today's usage. Never the token."""
    _require_admin_of(org_id, caller)
    memberships = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    users = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_([m.user_id for m in memberships]))
    )).scalars().all()}
    used = await _used_today_by_user(db, org_id)
    agent_emails = [u.email for u in users.values() if _is_agent_email(u.email)]
    # "connected" = the agent has EVER called in as itself (checkin or any real call) — what the
    # token card polls to flip to ✓ the moment the setup instruction's final step runs.
    seen = set((await db.execute(
        select(CallRecord.user_email).where(
            CallRecord.org_id == org_id, CallRecord.user_email.in_(agent_emails)).distinct()
    )).scalars().all()) if agent_emails else set()
    out: list[dict] = []
    for m in memberships:
        user = users.get(m.user_id)
        if user is None or not _is_agent_email(user.email):
            continue
        out.append({"user_id": user.id, "name": _agent_name(caller.org, user.email),
                    "email": user.email, "role": m.role, "daily_call_cap": m.daily_call_cap,
                    "used_today": used.get(user.email, 0), "tool_access": m.tool_access,
                    "project_access": m.project_access,  # the dashboard renders this column
                    "local_run_enabled": m.local_run_enabled, "pinned_tags": m.pinned_tags,
                    "created_at": m.created_at,
                    "created_by": m.created_by, "promoted_from": m.promoted_from,
                    "connected": user.email in seen})
    return out


@app.post("/agents/checkin")
async def agent_checkin(
    request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The handshake at the end of the setup instruction: the agent calls in AS ITSELF, proving the
    token landed in the right environment. Audited synchronously (not fire-and-forget) so the
    dashboard's poll sees `connected` flip the moment this returns. Works for any member token —
    for a human it is just a no-op ping."""
    rec = CallRecord(org_id=caller.org_id, user_email=caller.email, tool_name="—",
                     method="CHECKIN", path="agent connected", status_code=200, kind="checkin",
                     client=_client_of(request))
    db.add(rec)
    await db.commit()
    return {"connected": True, "you": caller.email, "org": caller.org.slug}


@app.get("/orgs/{org_id}/agents/observed")
async def list_observed_agents(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """The agents ALREADY running under members' own tokens, discovered from traffic: one row per
    (member, runtime) seen in the last 30 days, e.g. "sam@… / claude-code". The zero-setup half
    of the agents story — nobody mints anything, the roster fills itself from `CallRecord.client`
    (and RunRecord). Self-reported attribution, not authentication, which is why this view only
    informs; scoping one for real = mint it a token ("Scope this agent" in the dashboard).

    Machine identities are excluded — their calls are already attributed to themselves. So is a
    plain terminal (`client` in ('', 'cli')): a roster that lists every human twice teaches nothing.
    """
    _require_admin_of(org_id, caller)
    now = _utcnow_naive()
    since = now - timedelta(days=30)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # A pair that was PROMOTED — it has its own agent identity now — leaves the detected roster;
    # revoking that agent deletes its membership, which resurfaces the pair automatically.
    promoted = {tuple(m.promoted_from.split("|", 1)) for m in (await db.execute(
        select(Membership).where(Membership.org_id == org_id,
                                 Membership.promoted_from != ""))).scalars().all()}
    rows: dict[tuple[str, str], dict] = {}
    for model, email_col, when_col, client_col in (
        (CallRecord, CallRecord.user_email, CallRecord.created_at, CallRecord.client),
        (RunRecord, RunRecord.user_email, RunRecord.created_at, RunRecord.client),
    ):
        # One grouped pass per table: the 30-day totals plus today's slice as a conditional sum,
        # so a second identical GROUP BY isn't needed just to get `used_today`.
        q = (select(email_col, client_col, func.count(), func.max(when_col),
                    func.sum(case((when_col >= today, 1), else_=0)))
             .where(model.org_id == org_id, when_col >= since,
                    client_col.not_in(("", "cli")))
             .group_by(email_col, client_col))
        for email, client, count, last_seen, today_count in (await db.execute(q)).all():
            if _is_machine_email(email) or (email, client) in promoted:
                continue
            cur = rows.setdefault((email, client), {"member": email, "client": client,
                                                    "calls_30d": 0, "used_today": 0,
                                                    "last_seen": last_seen})
            cur["calls_30d"] += count
            cur["used_today"] += today_count
            cur["last_seen"] = max(cur["last_seen"], last_seen)
    return sorted(rows.values(), key=lambda r: (r["member"], r["client"]))


@app.delete("/orgs/{org_id}/agents/{user_id}")
async def revoke_agent(
    org_id: int, user_id: int,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke an agent: delete its membership, which kills its token immediately."""
    _require_admin_of(org_id, caller)
    user = await db.get(User, user_id)
    if user is None or not _is_agent_email(user.email):
        raise HTTPException(status_code=404, detail="unknown agent")
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user_id, Membership.org_id == org_id))).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    email = user.email  # read before the delete — the row is expired after commit
    await db.delete(membership)
    await _drop_member_deny_rules(db, user_id, org_id)  # a rule aimed at a caller that no longer exists
    await db.flush()
    # The identity is org-scoped, so once its last membership is gone the User row has no purpose.
    if (await db.execute(select(Membership).where(
            Membership.user_id == user_id))).scalars().first() is None:
        await db.delete(user)
    await db.commit()
    return {"revoked": True, "email": email}


app = APIRouter()
org_usage_router = app


@app.get("/orgs/{org_id}/usage")
async def org_usage(
    org_id: int, days: int = 30,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Usage rollups for an org over the last `days` (admin/owner): by user (with a call/local/server
    split), by tool, by day, and totals — counts only, no request/response bodies. Powers the dashboard
    Usage view."""
    _require_admin_of(org_id, caller)
    days = max(1, min(days, 365))
    since = _day_start_utc() - timedelta(days=days - 1)  # inclusive of today + the prior days-1
    return {"days": days, "since": since.isoformat(), **await _usage_rollup(db, org_id, since)}


app = APIRouter()
tag_controls_router = app


@app.get("/orgs/{org_id}/tag-keys")
async def list_tag_keys(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Every tag key this team has actually SENT, plus the ones it may budget on.

    Two different questions that had been given one answer. Reporting works on ANY key — the money
    for an undeclared one is folded in Python — while ENFORCEMENT only works on a declared key,
    because it needs an index. Feeding a reporting picker the declared list hid `feature=` and
    friends from the dashboard even though the API served them fine.
    """
    _require_admin_of(org_id, caller)
    seen = (await db.execute(
        select(TagSpend.dim).where(TagSpend.org_id == org_id).distinct())).scalars().all()
    declared = _budget_dims_of(caller.org)
    return {"seen": sorted(set(seen)), "budgetable": declared,
            "primary": _primary_dim_of(caller)}


@app.get("/orgs/{org_id}/usage/by-tag")
async def usage_by_tag(
    org_id: int, key: str | None = None, days: int = 30,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """What each value of one tag consumed — the numbers a reselling builder invoices from.

    MONEY COMES FROM THE LEDGER, never from `CallRecord`. Audit rows are fire-and-forget and the queue
    sheds them under load, which is precisely the traffic a successful builder generates; an invoice
    built on them would under-bill silently and unrecoverably. Call COUNTS come from the audit table,
    where losing a row costs a slightly low count and nothing else.

    `unattributed` is reported explicitly rather than dropped. A builder reconciling this against their
    own ledger has to see the spend they cannot attribute to anyone — silently omitting it is how the
    two sets of books stop agreeing without anybody noticing.
    """
    _require_admin_of(org_id, caller)
    days = max(1, min(days, 365))
    since = _day_start_utc() - timedelta(days=days - 1)
    dim = (key or _primary_dim_of(caller)).strip().lower()

    by_value = await ledger.spend_by_tag(db, org_id, dim, since)
    org_total = (await ledger.spend_since(db, org_id, since))["spend_micro"]
    # Counts come from the tag rows too. `CallRecord` holds only the primary dimension, so counting
    # there reported 0 for every non-primary key while the money column was correct — a report that
    # disagrees with itself is worse than one that admits its grain.
    counts = await ledger.calls_by_tag(db, org_id, dim, since)
    rows = [{"value": val, "charged_micro": micro, "charged_usd": ledger.usd(micro),
             "calls": int(counts.get(val, 0))}
            for val, micro in sorted(by_value.items(), key=lambda kv: -kv[1])]
    attributed = sum(by_value.values())
    return {
        "key": dim, "days": days, "since": since.isoformat(),
        "rows": rows,
        # The identity a builder's invoice rests on: these three reconcile against the team's own
        # settled spend for the window, whichever dimension they slice by.
        "attributed_micro": attributed,
        "unattributed_micro": org_total - attributed,
        "total_micro": org_total, "total_usd": ledger.usd(org_total),
    }


@app.get("/orgs/{org_id}/settings")
async def get_org_settings(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The team's spend ceiling and tag configuration. Readable by any member — a limit nobody can see
    is a limit that turns into a support ticket the first time an agent trips it."""
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="not your org")
    org = caller.org
    return {"daily_cap_micro": _effective_daily_cap(org),
            "daily_cap_set_by_team": int(org.daily_cap_micro or 0) or None,
            "platform_ceiling_micro": get_settings().platform_daily_cap_micro,
            "platform_overflow": not org.platform_overflow_disabled,
            "budget_dims": _budget_dims_of(org), "primary_dim": _primary_dim_of(caller)}


@app.patch("/orgs/{org_id}/settings")
async def set_org_settings(
    org_id: int, body: OrgSettingsIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set the team's own spend ceiling and which tag keys carry budgets. Admin+.

    A team may LOWER its ceiling freely; raising it past the platform ceiling is refused rather than
    silently clamped, because a builder who thinks they set $500/day and actually got $5 discovers it
    as an outage in the middle of their launch.
    """
    _require_admin_of(org_id, caller)
    org = caller.org
    sent = body.model_fields_set
    if "daily_cap_micro" in sent and body.daily_cap_micro is not None:
        ceiling = get_settings().platform_daily_cap_micro
        if body.daily_cap_micro < 0:
            raise HTTPException(status_code=422, detail="daily_cap_micro must be 0 or more")
        if body.daily_cap_micro > ceiling:
            raise HTTPException(status_code=403, detail={
                "error": "above_platform_ceiling", "requested_micro": body.daily_cap_micro,
                "ceiling_micro": ceiling,
                "message": (f"${ledger.usd(ceiling):g}/day is the ceiling we allow for a team. Ask us "
                            f"to raise it — reselling volume is a conversation, not a setting."),
            })
        org.daily_cap_micro = body.daily_cap_micro
    if "platform_overflow" in sent and body.platform_overflow is not None:
        org.platform_overflow_disabled = not body.platform_overflow
    if "budget_dims" in sent and body.budget_dims is not None:
        dims = [d.strip().lower() for d in body.budget_dims if d and d.strip()]
        if len(dims) > _MAX_BUDGET_DIMS:
            raise HTTPException(status_code=422, detail=(
                f"at most {_MAX_BUDGET_DIMS} budget dimensions — each one is an indexed lookup on "
                f"every call and a row per distinct value"))
        for d in dims:
            if not _META_KEY_RE.match(d):
                raise HTTPException(status_code=422, detail=f"{d!r} is not a valid tag key")
        org.budget_dims = dims or None
    if "primary_dim" in sent and body.primary_dim:
        if not _META_KEY_RE.match(body.primary_dim):
            raise HTTPException(status_code=422, detail=f"{body.primary_dim!r} is not a valid tag key")
        org.primary_dim = body.primary_dim
    await db.commit()
    return await get_org_settings(org_id, caller, db)


@app.get("/orgs/{org_id}/budgets")
async def list_tag_budgets(
    org_id: int, dim: str | None = None,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Every per-tag limit this team has SET — the per-dimension defaults plus the overrides.

    Registry rows are excluded. One is created per distinct value so the cardinality check stays a
    cheap lookup, and listing them made a table of eight rows in which six limited nothing: the
    bookkeeping was being presented as if it were policy.

    Admin+, because a budget names the team's customers.
    """
    _require_admin_of(org_id, caller)
    q = select(TagBudget).where(TagBudget.org_id == org_id, TagBudget.auto.is_(False))
    if dim:
        q = q.where(TagBudget.dim == dim)
    rows = (await db.execute(q.order_by(TagBudget.dim, TagBudget.val))).scalars().all()
    # Defaults first within each dimension — they are what everything else is an exception to.
    rows.sort(key=lambda r: (r.dim, r.val != TAG_DEFAULT, r.val))
    return [_tag_budget_view(r) for r in rows]


@app.put("/orgs/{org_id}/budgets/{dim}")
async def set_tag_default(
    org_id: int, dim: str, body: TagBudgetIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set the DEFAULT limit for a whole dimension — what every value inherits without an override.

    Unlimited until this is set: a team that never calls it behaves exactly as before. Changing it
    takes effect on the next call for everyone without an override, since resolution happens per
    call — so lowering a default is a live change across the whole customer base.
    """
    return await set_tag_budget(org_id, dim, TAG_DEFAULT, body, caller, db)


@app.put("/orgs/{org_id}/budgets/{dim}/{val}")
async def set_tag_budget(
    org_id: int, dim: str, val: str, body: TagBudgetIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set (or update) one limit — `PUT /orgs/1/budgets/customer/cust_8123 {"daily_cap_micro": 5000000}`.

    An UPSERT that leaves unsent fields alone, the same `model_fields_set` shape `create_agent` uses:
    a PUT that only flips `status` must not silently wipe the caps someone set last week.
    """
    _require_admin_of(org_id, caller)
    if not _META_KEY_RE.match(dim):
        raise HTTPException(status_code=422, detail=f"{dim!r} is not a valid tag key")
    if val != TAG_DEFAULT:
        _validate_tag_pair(dim, val)  # same rule as the call path — this value becomes a storage key
    declared = _budget_dims_of(caller.org)
    if dim not in declared:
        # Setting a limit IS the declaration. Requiring a separate PATCH first made the common path a
        # hidden two-step: the tag shows up in usage reports, so a person reasonably expects to be
        # able to cap it, and got a 422 telling them to go configure something else first.
        #
        # The BOUND still holds, because it is what keeps the call path cheap — each declared
        # dimension is another indexed lookup on every proxied call and another row per value. Past
        # the limit, refuse and say which ones are in use, since only the team knows which to drop.
        if len(declared) >= _MAX_BUDGET_DIMS:
            raise HTTPException(status_code=422, detail={
                "error": "too_many_budget_dimensions", "dim": dim, "declared": declared,
                "limit": _MAX_BUDGET_DIMS,
                "message": (f"budgets are already set up on {', '.join(declared)} — {_MAX_BUDGET_DIMS} "
                            f"is the limit, because each one is checked on every call. Remove one "
                            f"first if you want to budget on {dim!r} instead."),
            })
        caller.org.budget_dims = [*declared, dim]
        declared = caller.org.budget_dims
    if body.status is not None and body.status not in ("active", "blocked"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'blocked'")
    row = await _tag_budget(db, org_id, dim, val, create=True)
    row.auto = False  # a human set this: it is policy now, not registry bookkeeping
    sent = body.model_fields_set
    for field in ("daily_cap_micro", "monthly_cap_micro", "calls_per_day", "status", "note"):
        if field in sent:
            setattr(row, field, getattr(body, field))
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return _tag_budget_view(row)


@app.delete("/orgs/{org_id}/budgets/{dim}/{val}")
async def delete_tag_budget(
    org_id: int, dim: str, val: str,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Drop a limit. The tag keeps being recorded and invoiced — only the ceiling goes away."""
    _require_admin_of(org_id, caller)
    row = await _tag_budget(db, org_id, dim, val)
    if row is None:
        raise HTTPException(status_code=404, detail="no budget for that tag")
    await db.delete(row)
    await db.commit()
    return {"deleted": {"dim": dim, "val": val}}


app = APIRouter()
projects_router = app


@app.post("/orgs/{org_id}/projects")
async def create_project(
    org_id: int, body: ProjectIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Create a project — a sub-scope inside the team (admin+). Tools can then be filed under it and
    members scoped to it. Creating one changes nothing on its own: existing tools stay org-wide."""
    _require_admin_of(org_id, caller)
    name = (body.name or "").strip()
    slug = _slugify(name)
    if not name or not slug:
        raise HTTPException(status_code=422, detail="name is required")
    if (await db.execute(select(Project).where(
            Project.org_id == org_id, Project.slug == slug))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"a project {slug!r} already exists in this team")
    project = Project(org_id=org_id, name=name, slug=slug, created_by=caller.email)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_view(project, tool_count=0)


@app.get("/orgs/{org_id}/projects")
async def list_projects(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """The team's projects. A member scoped to some of them sees only those (the ACL hides what it
    gates, matching how `list_tools` behaves)."""
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="use this team's token")
    projects = (await db.execute(select(Project).where(Project.org_id == org_id))).scalars().all()
    access = caller.membership.project_access
    if caller.role != "owner" and access is not None:
        projects = [p for p in projects if p.id in access]
    counts = dict((await db.execute(
        select(Tool.project_id, func.count(Tool.id)).where(Tool.org_id == org_id).group_by(Tool.project_id)
    )).all())
    return [_project_view(p, tool_count=counts.get(p.id, 0)) for p in projects]


@app.delete("/orgs/{org_id}/projects/{project_id}")
async def delete_project(
    org_id: int, project_id: int,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a project. Its tools are NOT deleted — they fall back to org-wide, which is the safe
    direction (a tool that quietly vanished from every listing would be far worse than one that
    briefly becomes visible to the whole team). Members scoped to it lose that entry.

    The freed tools are what keeps a scoped member from being locked out: they were the member's only
    tools and they are now org-wide, so the member keeps exactly what they had. The scope list itself
    must NOT be widened to do that (see below)."""
    _require_admin_of(org_id, caller)
    project = await db.get(Project, project_id)
    if project is None or project.org_id != org_id:  # 404 across orgs — never confirm another org's ids
        raise HTTPException(status_code=404, detail="unknown project")
    freed = 0
    for tool in (await db.execute(select(Tool).where(Tool.project_id == project_id))).scalars().all():
        tool.project_id = None
        freed += 1
    for m in (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all():
        if m.project_access and project_id in m.project_access:
            # Store the remaining ids AS THEY ARE, empty list included. Collapsing `[]` to NULL here
            # would read as "every project" and hand a member scoped to only this project access to
            # every OTHER project's tools — a privilege escalation triggered by an unrelated delete.
            # `[]` is already the right meaning: org-wide tools only, which now include the freed ones.
            m.project_access = [p for p in m.project_access if p != project_id]
    # A rule scoped to this project can never fire again — and DenyRule.project_id is a foreign key,
    # so a surviving row would dangle (Postgres rejects that; SQLite only hides it). Same sweep-on-
    # departure idiom as _drop_member_deny_rules.
    for rule in (await db.execute(select(DenyRule).where(
            DenyRule.project_id == project_id))).scalars().all():
        await db.delete(rule)
    await db.delete(project)
    await db.commit()
    return {"deleted": project_id, "tools_made_org_wide": freed}


app = APIRouter()
policy_router = app


@app.post("/orgs/{org_id}/deny")
async def create_deny_rule(
    org_id: int, body: DenyRuleIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Block calls to a host / path / method for the whole team, or for one member or agent (admin+).

    Enforced on the proxy AND both run tiers, and it applies to every role including owner — a deny
    rule is a guardrail, not a permission tier."""
    _require_admin_of(org_id, caller)
    host = (body.host or "").strip().lower()
    if "://" in host:  # pasting a base_url is the obvious thing to try, so accept it
        host = urlsplit(host).netloc.lower()
    method = (body.method or "").strip().upper()
    path_prefix = (body.path_prefix or "").strip()
    if method and method not in PROXY_METHODS:
        raise HTTPException(status_code=422, detail=f"method must be one of {list(PROXY_METHODS)}")
    if not (host or path_prefix or method):
        # An all-empty rule matches every request — refuse it rather than silently freezing the org.
        raise HTTPException(status_code=422, detail="give at least one of host, path_prefix or method")
    if body.user_id is not None:
        target = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == body.user_id))).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="not a member of this org")
    if body.project_id is not None:
        project = await db.get(Project, body.project_id)
        if project is None or project.org_id != org_id:  # 404 across orgs, like everywhere else
            raise HTTPException(status_code=404, detail="unknown project")
    rule = DenyRule(org_id=org_id, user_id=body.user_id, project_id=body.project_id,
                    host=host, path_prefix=path_prefix,
                    method=method, note=(body.note or "").strip(), created_by=caller.email)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _deny_view(rule)


@app.get("/orgs/{org_id}/deny")
async def list_deny_rules(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    rules = (await db.execute(select(DenyRule).where(DenyRule.org_id == org_id))).scalars().all()
    return [_deny_view(r) for r in rules]


@app.get("/orgs/{org_id}/policy/cli-deny")
async def list_cli_deny(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """READ-ONLY: every CLI tool's effective argv deny patterns, so the Policy screen can show the
    whole "what is blocked" picture in one place. These live on the TOOL (treg.json `cli.deny` +
    catalog defaults, see `localrun.effective_profile`), not in the DenyRule table — editing them
    means editing the skill or the catalog, which is why this endpoint only reports (admin+)."""
    _require_admin_of(org_id, caller)
    from . import providers as prov
    out: list[dict] = []
    tools = (await db.execute(select(Tool).where(Tool.org_id == org_id))).scalars().all()
    for tool in tools:
        profile = localrun.effective_profile(tool, (prov.match_skill(tool.name) or {}).get("cli"))
        if not profile or not profile.get("deny"):
            continue
        own = set(profile.get("_own_deny") or [])
        out.append({"tool": tool.name, "enabled": bool(profile.get("enabled")),
                    "patterns": [{"pattern": p,
                                  "source": "skill" if p in own else "catalog"}
                                 for p in profile["deny"]]})
    return out


@app.delete("/orgs/{org_id}/deny/{rule_id}")
async def delete_deny_rule(
    org_id: int, rule_id: int,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_admin_of(org_id, caller)
    rule = await db.get(DenyRule, rule_id)
    if rule is None or rule.org_id != org_id:  # 404 across orgs — never confirm another org's ids
        raise HTTPException(status_code=404, detail="unknown rule")
    await db.delete(rule)
    await db.commit()
    return {"deleted": rule_id}
