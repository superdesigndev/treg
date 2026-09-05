"""The API — the only brain. CLI + skill are thin clients over this (charter).

Surface: open user registration (creates a personal org) + per-membership token auth; full CRUD
on secrets and tools; the /skills composer (register a whole skill = bundle + its secrets + its
tool(s) atomically) and /bundles reads; the /call proxy with a fire-and-forget audit record; and
/calls. A tool carries a LIST of bindings (multi-credential), with flat single-binding sugar on POST.

Multi-tenancy: a token = a (user, org) Membership. Every list/create/mutation and the proxy are
scoped to the caller's org; `owner` (creator email) drives the member-vs-admin role gate.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import select

from . import archive, audit, crypto, localrun, ratestore, runner, sandbox as demo_sandbox
from .application import asynctasks as async_task_app
from .caller_metadata import _client_of
from .config import get_settings
from .domain.catalog import store as catalog_store
from .infra.db import get_session, session_maker
from .domain.governance.teams import _unique_slug
from .domain.identity.access import (
    Caller,
    _membership_by_token,
    _require_can_register,
    _role_at_least,
    _user_from_identity_token,
    _user_from_session,
    require_member,
)
from .models import (CallRecord, CapabilityPin, LedgerEntry, Membership, Org, RunRecord, Secret,
                     Tool, ToolRequest, User)
from .routers import admin as admin_routes
from .routers import auth as auth_routes
from .routers import billing as billing_routes
from .routers import call as call_routes
from .routers import catalog as catalog_routes
from .routers import connections as connection_routes
from .routers import onboard as onboard_routes
from .routers import orgs as org_routes
from .routers import referrals as referral_routes
from .routers import resources as resources_routes
from .routers import web as web_routes
from .routers.auth import _client_ip
from .routers.auth_helpers import _same_origin
from .routers.call import _enforce_daily_cap, _require_tool_use_http
from .routers.orgs import _day_start_utc, _enforce_deny, _require_admin_of
from .routers.resources import _allowed_server_bins
from .routers.web import LOCAL_USER_EMAIL, _WEB_DIR
from .timeutil import utcnow_naive as _utcnow_naive

# ---- compatibility re-exports --------------------------------------------------------------
# _mount_static reads these directories off `api_module`. Everything else once re-exported here
# now lives in, and is imported from, its real home.
from .routers.web import _LOGO_DIR, _MEDIA_DIR, _TOUR_DIR, _VENDOR_DIR  # noqa: F401


LOCAL_ORG_NAME = "personal"


async def _bootstrap_single_user() -> None:
    """Frictionless local mode: make the machine's owner exist, so `curl … | sh` lands on a dashboard
    that is already signed in — no account, no email, no password.

    Idempotent, and the token is STABLE across restarts (rotating it every boot would break the CLI
    config the installer just wrote). It is re-minted only when the token file is missing, i.e. the
    user deleted it and needs a new one. Gated by `single_user_ok`, which refuses anything that isn't
    a local sqlite box — see config.Settings.
    """
    s = get_settings()
    if not s.single_user_ok:
        return
    path = Path(s.single_user_token_file).expanduser()
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none()
        if user is None:
            user = User(email=LOCAL_USER_EMAIL, onboarded=True)
            db.add(user)
            await db.flush()
        # Adopt an org ONLY through a membership this identity already has. Looking one up by the
        # slug `personal` and joining it as owner would, on a database that is not fresh, hand the
        # password-less local identity ownership of a team that belongs to someone else — and an
        # owner is exempt from every ACL. A new team therefore takes a FREE slug (`personal-2`, …)
        # rather than colliding with whatever already holds `personal`.
        membership = (await db.execute(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)
        )).scalars().first()
        token = ""
        if membership is None:
            org = Org(name=LOCAL_ORG_NAME.title(), slug=await _unique_slug(LOCAL_ORG_NAME, db))
            db.add(org)
            await db.flush()
            token = crypto.new_token()  # first boot
            membership = Membership(user_id=user.id, org_id=org.id, role="owner",
                                    token_hash=crypto.hash_token(token))
            db.add(membership)
        else:
            org = await db.get(Org, membership.org_id)
            if not path.exists():
                token = crypto.new_token()  # the token file was removed — mint a replacement
                membership.token_hash = crypto.hash_token(token)
        team = org.slug if org is not None else LOCAL_ORG_NAME
        await db.commit()
    if token:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        path.chmod(0o600)  # the installer reads it; nobody else should
    shown = token or "(unchanged — see " + str(path) + ")"
    print(f"\n  treg is ready — no account needed."
          f"\n  Dashboard  {s.public_url}/app"
          f"\n  Team       {team}"
          f"\n  Token      {shown}\n", flush=True)


# The few routes still defined below await their post-stage-4 destinations; everything else is
# spliced in from routers/. `bootstrap.create_app` consumes this router after import and owns
# every concrete assembly decision around it.
router = APIRouter()
app = router  # temporary decorator target; replaced by the compatibility FastAPI app at EOF


async def _id_out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
    # A huge all-digit path param (e.g. /secrets/999…) overflows SQLite's 64-bit INTEGER at bind
    # time; that's a non-existent id, not a server fault — surface a 404 instead of a 500.
    return JSONResponse({"detail": "identifier out of range"}, status_code=404)


_app_version_cache: tuple[float, str] | None = None  # (index.html mtime, content hash)


@lru_cache(maxsize=1)
def _treg_version() -> str:
    """The released package version, read from installed metadata.

    Read directly rather than through `cli.cli_version`, which does the same thing: importing
    `treg.cli` here costs ~200ms on first call and pulls the entire CLI into the server process for
    one string. Cached because package metadata cannot change while the process runs.
    """
    try:
        from importlib.metadata import version

        return version("tools-registry")
    except Exception:  # noqa: BLE001 — an editable/source run has no installed metadata
        return "dev"


def _app_version() -> str:
    """A stamp that changes with every deploy of the dashboard bundle: a hash of index.html,
    re-derived when the file's mtime moves (so dev --reload picks up edits too). Long-lived tabs
    compare this against the value they booted with and offer a refresh when it drifts."""
    global _app_version_cache
    index = _WEB_DIR / "index.html"
    try:
        mtime = index.stat().st_mtime
    except OSError:
        return "dev"
    if _app_version_cache is None or _app_version_cache[0] != mtime:
        digest = hashlib.sha256(index.read_bytes()).hexdigest()[:12]
        _app_version_cache = (mtime, digest)
    return _app_version_cache[1]


@app.get("/meta")
async def meta() -> dict:
    """Open: what the dashboard needs to render correct, shareable snippets — the public proxy URL
    (so copy/paste snippets use the real domain, not whatever origin the browser happens to be on)
    — plus the bundle version, so an open tab can detect a new deploy and offer a refresh.

    `treg_version` and `app_version` answer DIFFERENT questions and both are worth having.
    `app_version` is a hash of index.html: it changes whenever the dashboard bundle does, which is
    what an open tab compares to offer a refresh. `treg_version` is the released package version,
    which is what a release check needs — after publishing 0.9.0 there was no way to confirm from the
    live path which version was actually serving, only the commit id.
    """
    s = get_settings()
    return {"public_url": s.public_url.rstrip("/"), "github": bool(s.github_client_id),
            "google": bool(s.google_client_id), "app_version": _app_version(),
            "treg_version": _treg_version(),
            # public ingestion key — only present when this deployment opts in (self-hosters send nothing)
            "posthog_key": s.posthog_key, "posthog_host": s.posthog_host.rstrip("/") if s.posthog_key else "",
            # public workspace id — only present when this deployment opts in (self-hosters load no widget)
            "intercom_app_id": s.intercom_app_id}


@app.get("/providers.json", include_in_schema=False)
async def providers_catalog() -> dict:
    """Open: the provider catalog `treg upload` uses to detect env keys → tools. Served so the CLI can
    refresh it centrally (add a provider here → every CLI picks it up) with its bundled copy as fallback.
    See [env-import](../docs/context/interface/env-import.md)."""
    from . import providers as prov
    return {"version": prov.CATALOG_VERSION, "providers": prov.CATALOG}


# Moved routers are spliced in at their pre-refactor positions: FastAPI resolves overlapping
# paths by registration order, and the route snapshot holds this file to the original order.
router.routes.extend(catalog_routes.public_router.routes)
router.routes.extend(web_routes.catalog_pages_router.routes)

# ---- "the catalog doesn't have X" — tool requests -------------------------------------------
TOOLREQ_HIT_NS = "toolreq"
TOOLREQ_RATE_MAX = 10          # filings per IP per window
TOOLREQ_RATE_WINDOW_S = 3600   # 1 hour
TOOLREQ_SOURCES = {"web", "cli", "mcp", "claude-connector", "api"}


class ToolRequestIn(BaseModel):
    capability: str          # what they wanted — "Ahrefs backlinks", "flight prices", a provider name
    query: str = ""          # the catalog search that came up empty (agents auto-fill this)
    note: str = ""
    contact: str = ""        # optional reach-back; free text, unverified
    source: str = "web"      # web | cli | mcp | claude-connector | api


@app.post("/tool-requests", include_in_schema=False)
async def create_tool_request(
    body: ToolRequestIn,
    request: Request,
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Open: file a "the catalog doesn't have X" report. No auth on purpose — the filer is most
    often an agent that just got zero search results and holds no token, and a signup wall here
    costs exactly the demand signal the catalog team wants. Per-IP rate limiting (ratestore) is
    the abuse valve, same shape as POST /demo/sandbox; field caps bound the row.

    Identity is attribution, never authorization: when the caller happens to be signed in (token
    or same-origin session), the row records who asked so they can be told when it lands — a
    forged cross-origin cookie POST gets stored as anonymous, not rejected, hence the
    `_same_origin` gate on the cookie path only."""
    await ratestore.sweep(db, TOOLREQ_HIT_NS)
    if not await ratestore.rate_check(db, TOOLREQ_HIT_NS,
                                      [(_client_ip(request), TOOLREQ_RATE_MAX)], TOOLREQ_RATE_WINDOW_S):
        await db.commit()  # persist the sweep even on reject
        raise HTTPException(status_code=429, detail="too many tool requests from here — try again later")
    capability = body.capability.strip()
    if not capability:
        raise HTTPException(status_code=422, detail="say what tool/capability you need")
    if len(capability) > 200:
        raise HTTPException(status_code=422, detail="capability is a headline — keep it under 200 chars")
    org_id, user_email = None, ""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        if user is not None and not user.suspended:
            org_id, user_email = (m.org_id if m else None), user.email
    elif treg_session and _same_origin(request):
        user = await _user_from_session(treg_session, db)
        if user is not None:
            user_email = user.email
    row = ToolRequest(
        org_id=org_id,
        user_email=user_email,
        capability=capability,
        query=body.query.strip()[:300],
        note=body.note.strip()[:2000],
        contact=body.contact.strip()[:200],
        source=body.source if body.source in TOOLREQ_SOURCES else "api",
    )
    db.add(row)
    await db.commit()
    return {"id": row.id, "status": "received",
            "note": "logged — requests steer which provider gets keyed next"}


router.routes.extend(auth_routes.social_router.routes)
router.routes.extend(auth_routes.cli_router.routes)         # CLI pairing
router.routes.extend(auth_routes.session_router.routes)
router.routes.extend(auth_routes.email_router.routes)       # email OTP
router.routes.extend(auth_routes.invite_router.routes)
router.routes.extend(web_routes.site_router.routes)
router.routes.extend(auth_routes.oauth_server_router.routes)
router.routes.extend(web_routes.public_docs_router.routes)
router.routes.extend(auth_routes.token_router.routes)       # caller auth: token = a Membership


def _require_local_run(caller: Caller) -> None:
    """Gate the LOCAL run tier on the member's `local_run_enabled` (owner exempt). Off → server only."""
    if caller.role != "owner" and not caller.membership.local_run_enabled:
        raise HTTPException(status_code=403, detail=(
            "local execution is disabled for you — run on the server instead (`treg run --server`), "
            "or ask an admin to enable local runs for your account"))


router.routes.extend(org_routes.signup_router.routes)
router.routes.extend(auth_routes.grants_router.routes)
router.routes.extend(org_routes.org_entry_router.routes)
router.routes.extend(org_routes.invite_entry_router.routes)
router.routes.extend(onboard_routes.onboard_entry_router.routes)
router.routes.extend(onboard_routes.sandbox_router.routes)
router.routes.extend(onboard_routes.onboard_teammate_router.routes)
router.routes.extend(org_routes.invite_management_router.routes)
router.routes.extend(org_routes.member_list_router.routes)
router.routes.extend(org_routes.org_usage_router.routes)
router.routes.extend(billing_routes.balance_router.routes)
router.routes.extend(org_routes.tag_controls_router.routes)
router.routes.extend(billing_routes.billing_router.routes)
router.routes.extend(referral_routes.router.routes)
router.routes.extend(billing_routes.webhook_router.routes)
router.routes.extend(org_routes.member_management_router.routes)
router.routes.extend(org_routes.machine_identity_router.routes)


# ---- projects: an optional sub-scope inside an org ------------------------------------------
router.routes.extend(org_routes.projects_router.routes)


# ---- capability pins: the team's provider choice, per capability ---------------------------
class CapabilityPinIn(BaseModel):
    capability: str
    provider: str


@app.get("/orgs/{org_id}/pins")
async def list_capability_pins(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """The team's provider choices, per capability. Readable by any member — an agent has to know
    what it is allowed to call, and finding out by being refused is a wasted round-trip."""
    if caller.org_id != org_id:      # the token IS the membership (same rule as every org route)
        raise HTTPException(status_code=403, detail="not a member of this org")
    rows = (await db.execute(select(CapabilityPin).where(CapabilityPin.org_id == org_id)
                             .order_by(CapabilityPin.capability))).scalars().all()
    return [{"id": r.id, "capability": r.capability, "provider": r.provider,
             "created_by": r.created_by, "created_at": r.created_at} for r in rows]


@app.post("/orgs/{org_id}/pins")
async def set_capability_pin(
    org_id: int, body: CapabilityPinIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Pin a capability to one provider for the whole team (admin+). Re-pinning replaces.

    Both halves are validated against the catalog: an unknown capability, or a provider that does
    not actually serve it, would be a rule that silently blocks every call to a job the team
    genuinely uses — a typo must fail here, loudly, not at 3am in an agent's log."""
    _require_admin_of(org_id, caller)
    cat = catalog_store.load()
    serving = cat.for_capability(body.capability)
    if not serving:
        raise HTTPException(status_code=422, detail=f"unknown capability {body.capability!r}")
    providers = sorted({e["provider"] for e in serving})
    if body.provider not in providers:
        raise HTTPException(status_code=422, detail=(
            f"{body.provider!r} does not serve {body.capability!r} — "
            f"these do: {', '.join(providers)}"))
    # Read the caller's email NOW, as a plain string. A rollback below expires every ORM instance
    # behind `caller`, and touching one afterwards lazy-loads outside the async context —
    # MissingGreenlet, which is how this first failed under concurrency.
    who = caller.email
    row = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == org_id,
        CapabilityPin.capability == body.capability))).scalars().first()
    if row is None:
        row = CapabilityPin(org_id=org_id, capability=body.capability, provider=body.provider,
                            created_by=who)
        db.add(row)
    else:
        row.provider, row.created_by = body.provider, who
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to another admin (or another web worker). The UNIQUE index is what actually
        # prevents the duplicate; this makes losing look like the sequential path — re-apply onto
        # the winner's row rather than handing back a 500 for a pin that plainly succeeded.
        await db.rollback()
        row = (await db.execute(select(CapabilityPin).where(
            CapabilityPin.org_id == org_id,
            CapabilityPin.capability == body.capability))).scalars().first()
        if row is None:
            raise
        row.provider, row.created_by = body.provider, who
        await db.commit()
    return {"capability": body.capability, "provider": body.provider,
            "alternatives": [p for p in providers if p != body.provider]}


@app.delete("/orgs/{org_id}/pins")
async def clear_capability_pin(
    org_id: int, capability: str = Query(..., min_length=1, max_length=200),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a pin (admin+) — the capability goes back to the caller's choice.

    The capability is a QUERY parameter, not a path segment, and that is a safety decision rather
    than a style one. As `/orgs/{id}/pins/{capability}` it was one keystroke from a catastrophe:
    every normalizing HTTP client (httpx included) rewrites `/orgs/1/pins/..` to `/orgs/1` BEFORE
    sending it — which is DELETE /orgs/{id}, the delete-the-team route. `treg org unpin ..` really
    did destroy an org in testing. Server-side validation cannot defend against it, because the
    rewrite happens in the client; taking the value out of the path removes the class entirely."""
    _require_admin_of(org_id, caller)
    rows = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == org_id, CapabilityPin.capability == capability))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"no pin for {capability!r}")
    for row in rows:      # all of them, in case a duplicate predates the unique index
        await db.delete(row)
    await db.commit()
    return {"capability": capability, "pinned": False}


router.routes.extend(org_routes.policy_router.routes)
router.routes.extend(resources_routes.crud_router.routes)


# ---- local runs (`treg run`): grant + outcome report (docs/CLI-RUN-PLAN.md) -----------------
# Redact obvious credentials a user might type INLINE (`treg run x -- --token sk_live_…`) before the
# argv is persisted to the audit log — known key prefixes, any high-entropy token, JWTs, AND the value
# that follows a credential-looking flag (so a SHORT password like `--password hunter2` is masked too).
_ARGV_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    # JWT (base64url with dots). `\b` and the POSSESSIVE `++` are load-bearing, not tidying: without
    # the anchor, input like "eyJeyJeyJ…" offers a fresh start position every three characters and
    # each one scans forward for a `.`, which is quadratic. Anchoring leaves one start; `++` removes
    # backtracking within an attempt (it cannot change what matches here — the class excludes `.`,
    # so the run always ends at the first one). Same shape guards the argv rule below.
    r"|\beyJ[A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]{8,}"
    r"|\b[A-Za-z0-9_\-]{24,}\b")  # any 24+ high-entropy run — deliberately over-masks (git SHAs, UUIDs)
                                  # since in an audit log a false mask is harmless but a real key isn't
_CRED_FLAG = r"--?(?:token|password|passwd|pass|pwd|api[-_]?key|secret|auth|bearer|credential)s?"
_CRED_FLAG_EQ_RE = re.compile(rf"({_CRED_FLAG})=\S+", re.I)
_CRED_FLAG_BARE_RE = re.compile(rf"^{_CRED_FLAG}$", re.I)


def _redact_argv_list(argv: list[str]) -> list[str]:
    """Per-element redaction that also masks the element FOLLOWING a bare credential flag."""
    out: list[str] = []
    mask_next = False
    for a in argv:
        if mask_next:
            out.append("***"); mask_next = False; continue
        if _CRED_FLAG_BARE_RE.match(a):          # `--password` `hunter2` → mask the value that follows
            out.append(a); mask_next = True; continue
        a = _CRED_FLAG_EQ_RE.sub(r"\1=***", a)   # `--password=hunter2`
        out.append(_ARGV_SECRET_RE.sub("***", a))
    return out


def _redact_argv(argv: list[str]) -> str:
    return " ".join(_redact_argv_list(argv))[:500]


class GrantIn(BaseModel):
    argv: list[str] = []  # the CLI args the member is about to run (deny-checked + audited)


class RunReportIn(BaseModel):
    audit_id: int      # the grant's audit row — proves this report follows a real grant
    exit_code: int
    verdict: str       # ok | credential_invalid | unknown_error (client matched stderr locally)


async def _grant_audit(db: AsyncSession, caller: Caller, tool_name: str, method: str, path: str,
                       status: int, client: str = "") -> int:
    """A SYNCHRONOUS audit row (unlike record_call): the grant returns its audit id so the
    run-report can prove it follows a real grant. One insert; this is not the hot proxy path."""
    rec = CallRecord(org_id=caller.org_id, user_email=caller.email, tool_name=tool_name,
                     method=method, path=path[:500], status_code=status, kind="local_run",
                     client=client)
    db.add(rec)
    await db.commit()
    return rec.id


@app.post("/tools/{name}/grant")
async def grant_local_run(
    name: str,
    body: GrantIn,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint the process material for ONE local run of this tool's CLI: the audited, owner-opt-in
    exception to "values are never returned". OAuth secrets release only the expiring leaf; the
    deny check happens here, where the secret lives. Unlike /call (which injects server-side and
    leaks nothing), a grant HANDS the credential value to the caller's machine — so it needs member+
    (a viewer may call but not extract). Loosening this to a per-tool run ACL is a future policy knob."""
    _require_can_register(caller)
    from . import providers as prov
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_use_http(caller, tool)  # per-member tool + project ACL (call + both run tiers)
    _require_local_run(caller)               # local tier may be disabled for this member (server-only)
    await _enforce_deny(caller, tool.base_url, "", db, tool.project_id)  # host-level policy (see run_tool_server)
    await _enforce_daily_cap(caller, db)  # a local run counts toward the per-user daily cap
    catalog_cli = (prov.match_skill(tool.name) or {}).get("cli")
    profile = localrun.effective_profile(tool, catalog_cli)
    if profile is None:
        raise HTTPException(status_code=409, detail=(
            f"treg doesn't know how to inject credentials into {tool.name!r}. Add a \"cli\" block to the "
            'skill\'s treg.json — template: {"cli": {"bin": "' + tool.name + '", '
            '"inject": [{"secret": "<local secret name>", "via": "env", "name": "<ENV_VAR>"}]}}'))
    if profile.get("unsupported"):
        raise HTTPException(status_code=409, detail=f"{tool.name}: {profile.get('reason', 'this CLI cannot be injected')}")
    if not profile.get("enabled"):
        raise HTTPException(status_code=403, detail=(
            f"local runs are disabled for {tool.name!r} — an owner/admin can enable them: "
            f"treg tool update {tool.name} --local-run on"))
    denied = localrun.check_deny(profile, body.argv)
    if denied:
        pattern, source = denied
        await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403, _client_of(request))
        raise HTTPException(status_code=403, detail=(
            f"denied by {source}: pattern {pattern!r}. The skill's creator controls this list "
            "(cli.deny in treg.json)."))
    # Runner-proof gate (Bug 1). Handing a member a secret they do NOT own — a shared-key tool they may
    # RUN but not SEE — is allowed only for the isolated treg-run runner, which proves itself with a
    # value the member can't read (`X-Treg-Run-Proof`). A direct member call has no proof, so the raw
    # value never reaches the member's eyes. Owned secrets (or an admin) skip this — you can read a key
    # you already hold.
    inject_sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
    needs_proof = False
    if not _role_at_least(caller.role, "admin"):
        for sid in (s for s in inject_sids if s is not None):
            sec = await db.get(Secret, sid)
            if sec is not None and sec.owner != caller.email:
                needs_proof = True
                break
    if needs_proof:
        proof = get_settings().run_proof
        supplied = request.headers.get("X-Treg-Run-Proof", "")
        if not (proof and hmac.compare_digest(supplied, proof)):
            await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403, _client_of(request))
            raise HTTPException(status_code=403, detail=(
                "this tool uses another member's key — running it needs the isolated treg-run runner "
                "(an admin sets it up once: `sudo treg setup-local-run --run-proof …`). A direct grant "
                "can't expose someone else's key value to you."))
    try:
        rendered = await localrun.render_grant(tool, profile, db, request.app.state.http)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — a failed oauth refresh must read clearly, like /call
        raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
    audit_id = await _grant_audit(db, caller, tool.name, "GRANT", _redact_argv(body.argv), 200, _client_of(request))
    warnings = list(profile.get("warnings") or [])
    ttl = rendered["ttl_seconds"]
    if ttl is not None and ttl <= 0:
        warnings.append("the injected token appears already expired — the run will likely fail; "
                        "an owner may need to reconnect it (treg oauth connect)")
    elif ttl is not None:
        warnings.append(f"the injected token expires in ~{max(1, ttl // 60)} min — "
                        "long-running commands may outlive it")
    return {
        "bin": profile.get("bin", tool.name),
        "inject": rendered["items"],  # delivery-tagged items — the client applies each (env/argv/broker)
        "ttl_seconds": rendered["ttl_seconds"],
        "install": profile.get("install"),
        "noninteractive": profile.get("noninteractive") or [],
        "warnings": warnings,
        "errors": profile.get("errors") or [],
        # Scrub the injected value from the CLI's output when the member doesn't OWN the key (a shared
        # key run through the isolated runner) — so a CLI feature (`gh auth token`, an env dump) can't be
        # used to print it back. Owned/admin runs skip it (you may see your own key) and keep a raw TTY.
        "redact_output": needs_proof,
        "audit_id": audit_id,
    }


@app.post("/tools/{name}/run-report")
async def report_local_run(
    name: str,
    body: RunReportIn,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The client's post-run verdict (it matches stderr against the grant's error patterns LOCALLY
    and sends only this enum — raw output never leaves the machine). credential_invalid flips the
    granted secrets to invalid via the same health fields the runner uses."""
    _require_can_register(caller)  # marking a credential invalid is a register-tier action, not a read
    if body.verdict not in localrun.VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {localrun.VERDICTS}")
    grant_rec = await db.get(CallRecord, body.audit_id)
    # Bind the report to the SAME user who received the grant — otherwise a member could invalidate
    # another user's secrets (a DoS) by guessing a sequential audit_id.
    if (grant_rec is None or grant_rec.org_id != caller.org_id or grant_rec.method != "GRANT"
            or grant_rec.tool_name != name or grant_rec.user_email != caller.email):
        raise HTTPException(status_code=404, detail="no matching grant for that audit_id")
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    marked: list[str] = []
    if body.verdict == "credential_invalid":
        from . import providers as prov
        profile = localrun.effective_profile(tool, (prov.match_skill(tool.name) or {}).get("cli")) or {}
        # Mark only the credentials this run actually INJECTED (the ones the CLI used) — not every HTTP
        # binding — and never a `param` (it's config, not a credential; mirrors health.run_all's guard).
        sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
        now = _utcnow_naive()
        for sid in [s for s in sids if s is not None]:
            secret = await db.get(Secret, sid)
            if secret is not None and secret.org_id == caller.org_id and secret.kind != "param":
                secret.health_status = "invalid"
                secret.health_detail = f"local run of {tool.name} reported an auth failure (exit {body.exit_code})"
                secret.health_checked_at = now
                marked.append(secret.name)
    await _grant_audit(db, caller, tool.name, "REPORT", f"exit={body.exit_code} verdict={body.verdict}", 200)
    return {"ok": True, "marked_invalid": marked}


# ---- skills: register a whole skill atomically; analyze / import an uploaded folder --------
router.routes.extend(resources_routes.skill_router.routes)


# ---- audit read ---------------------------------------------------------------------------
@app.get("/calls")
async def list_calls(
    limit: int = 50, days: int | None = None, before_id: int | None = None,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """This team's recent calls. `days` windows it and `before_id` pages backwards — a builder
    reconciling a month cannot do it through a newest-first limit alone.

    Analytics, NOT an invoice: these rows are written fire-and-forget and the queue sheds them under
    load. Money comes from `/orgs/{id}/usage/by-tag`, which reads the ledger.
    """
    limit = max(1, min(limit, 500))
    # The failure-evidence columns are not in the response below and are not fetched either: they are
    # the two wide columns on this table, this endpoint returns up to 500 rows, and a column nobody
    # reads should not cross the wire. Deferring also means adding them to the payload later has to be
    # a deliberate edit in two places, not an accident in one.
    # Keep owned free polling in diagnostic audit, but out of Activity before applying the limit.
    q = (select(CallRecord)
         .options(defer(CallRecord.error_request), defer(CallRecord.error_response))
         .where(CallRecord.org_id == caller.org_id, CallRecord.kind != "async_poll"))
    if days is not None:
        q = q.where(CallRecord.created_at >= _day_start_utc() - timedelta(days=max(1, min(days, 365)) - 1))
    if before_id is not None:
        q = q.where(CallRecord.id < before_id)
    rows = (await db.execute(q.order_by(CallRecord.id.desc()).limit(limit))).scalars().all()
    # A metered async submission audited its RESERVE as the charge. The task record is the account
    # of what happened afterwards (settled, refunded, timed out) and what the caller bought.
    tasks = await async_task_app.views_for(
        caller.org_id, [c.call_ref for c in rows if c.call_ref and c.credential_tier == "platform"])
    return [
        {
            "id": c.id,
            "user_email": c.user_email,
            "tool_name": c.tool_name,
            "method": c.method,
            "path": c.path,
            "status_code": c.status_code,
            "kind": c.kind,
            "client": c.client,
            # Marketplace telemetry — all null for a plain tool call (see models.CallRecord). Kept in
            # the same row a caller already reads, so "what did this cost me" needs no second endpoint.
            "endpoint_id": c.endpoint_id,
            "provider": c.provider,
            "credential_tier": c.credential_tier,
            # The archive answered instead of the vendor; the money columns are still identical to
            # a live call on purpose (docs/context/architecture/archive.md).
            "cached": c.cached,
            # The archive holds this call's answer: `GET /calls/{id}/result` can show it. False
            # for own-key/own-tool calls (never stored), failures, and calls made while recording
            # was off.
            "has_result": c.archive_key_hash is not None,
            "cost_estimated_micro": c.cost_estimated_micro,
            "cost_observed_micro": c.cost_observed_micro,
            "cost_charged_micro": _async_charged(c, tasks.get(c.call_ref)),
            "duration_ms": c.duration_ms,
            "response_bytes": c.response_bytes,
            "params_hash": c.params_hash,
            # non-null = treg said no before anything went upstream (see models.CallRecord) — the
            # one field that tells "the provider failed" apart from "we refused" in `treg audit`.
            "refused_by": c.refused_by,
            # The caller's own tags (X-Treg-Meta), for a builder reconciling this row against their
            # records. Money is NOT invoiced from here — see the ledger-backed usage endpoint.
            "call_ref": c.call_ref,
            "budget_dim": c.budget_dim,
            "budget_val": c.budget_val,
            "tags": c.tags,
            "created_at": c.created_at.isoformat(),
            # Present only on a metered async submission: settlement state, and the artifact once the
            # task succeeded (a time-limited URL, or the CLI command that retrieves it).
            "async_task": tasks.get(c.call_ref),
        }
        for c in rows
    ]


def _async_charged(c: CallRecord, task: dict | None) -> int | None:
    """What hit the balance, once the task record is known: nothing yet while pending, the settled
    figure at a terminal state. Without a task record the audit row's own column stands."""
    if task is None:
        return c.cost_charged_micro
    return None if task["status"] == "pending" else task["settled_micro"]


@app.get("/calls/{call_id:int}/result")
async def get_call_result(
    call_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """What one of this team's calls asked and what came back — the archive's copy.

    Only a METERED PLATFORM 2xx call has one: those answers are recorded by the archive (see
    docs/context/architecture/archive.md), and the audit row keeps the identities of the exact
    bytes the caller received. Own-key and own-tool calls are relayed without being stored, a
    failed call never is, and the redacted failure evidence stays admin-only — `stored` is false
    with a `note` saying which. The request shape is the vendor-facing one BEFORE credential
    injection, so no secret can appear in it.
    """
    row = (await db.execute(
        select(CallRecord)
        .options(defer(CallRecord.error_request), defer(CallRecord.error_response))
        .where(CallRecord.org_id == caller.org_id, CallRecord.id == call_id))).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="no call with that id")
    out = {"id": row.id, "call_ref": row.call_ref, "endpoint_id": row.endpoint_id,
           "provider": row.provider, "credential_tier": row.credential_tier,
           "method": row.method, "path": row.path, "status_code": row.status_code,
           "cached": row.cached, "created_at": row.created_at.isoformat() if row.created_at else None,
           "stored": False, "note": None, "request": None, "response": None}
    if not row.archive_key_hash or not row.archive_content_hash:
        if row.credential_tier != "platform":
            out["note"] = ("not stored: calls on your own key or your own tools are relayed "
                           "without being kept")
        elif not (200 <= row.status_code < 300):
            out["note"] = "not stored: the call failed, so there is no answer on file"
        else:
            out["note"] = "not stored: recording was off when this call was made"
        return out
    found = await archive.resolve_result(db, row.archive_key_hash, row.archive_content_hash)
    if found is None:
        out["note"] = "expired: this answer is no longer on file"
        return out
    out |= found
    if not found["stored"]:
        out["note"] = ("hash-only: the provider's licence or the size cap did not allow keeping "
                       "the bytes")
    return out


@app.get("/calls/{call_ref}")
async def get_call(
    call_ref: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """One call by the `X-Treg-Call-Id` it returned — the join key for a builder's own records.

    Also reports the LEDGER's view of the same reference, because that is the durable one: the audit
    row is fire-and-forget and may have been shed, while the money entries were written synchronously.
    A 404 here therefore means "no audit row", not "this call never happened" — check `ledger` in the
    body before concluding anything about money.
    """
    row = (await db.execute(select(CallRecord).where(
        CallRecord.org_id == caller.org_id, CallRecord.call_ref == call_ref))).scalars().first()
    entries = (await db.execute(select(LedgerEntry).where(
        LedgerEntry.org_id == caller.org_id, LedgerEntry.call_id == call_ref)
        .order_by(LedgerEntry.created_at))).scalars().all()
    if row is None and not entries:
        raise HTTPException(status_code=404, detail="no call with that id")
    task = (await async_task_app.views_for(caller.org_id, [call_ref])).get(call_ref)
    view = None
    if row is not None:
        view = {"id": row.id, "call_ref": row.call_ref, "user_email": row.user_email,
                "tool_name": row.tool_name, "method": row.method, "path": row.path,
                "status_code": row.status_code, "kind": row.kind, "client": row.client,
                "endpoint_id": row.endpoint_id, "provider": row.provider,
                "credential_tier": row.credential_tier,
                "cost_estimated_micro": row.cost_estimated_micro,
                "cost_observed_micro": row.cost_observed_micro,
                "cost_charged_micro": _async_charged(row, task),
                "duration_ms": row.duration_ms, "response_bytes": row.response_bytes,
                "refused_by": row.refused_by, "budget_dim": row.budget_dim,
                "budget_val": row.budget_val, "tags": row.tags,
                "created_at": row.created_at.isoformat() if row.created_at else None}
    return {
        "call": view,
        "async_task": task,
        "ledger": [{"kind": e.kind, "amount_micro": e.amount_micro, "endpoint_id": e.endpoint_id,
                    "created_at": e.created_at.isoformat() if e.created_at else None}
                   for e in entries],
        "charged_micro": sum(-e.amount_micro for e in entries if e.kind == "settle"),
    }


@app.get("/runs")
async def list_runs(
    limit: int = 50, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Audit log for CLI executions (`treg run`, both tiers), scoped to the caller's org — each row
    tagged `where`: "server" (RunRecord) or "local" (a `local_run` GRANT on the member's machine).
    Local successes carry no exit code (only failures report back), so `exit_code` is null for them.
    Ids are prefixed (s/l) so the two sources never collide as list keys."""
    limit = max(1, min(limit, 500))
    server = (await db.execute(
        select(RunRecord).where(RunRecord.org_id == caller.org_id)
        .order_by(RunRecord.id.desc()).limit(limit)
    )).scalars().all()
    # A local run is audited as its GRANT (kind="local_run"); the redacted argv lives in `path`.
    local = (await db.execute(
        select(CallRecord).where(
            CallRecord.org_id == caller.org_id, CallRecord.kind == "local_run",
            CallRecord.method == "GRANT")
        .order_by(CallRecord.id.desc()).limit(limit)
    )).scalars().all()
    rows = [
        {"id": f"s{r.id}", "user_email": r.user_email, "tool": r.bundle_name,  # bundle_name = tool (historical)
         "argv": r.argv, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
         "where": "server", "client": r.client, "created_at": r.created_at.isoformat()}
        for r in server
    ] + [
        {"id": f"l{c.id}", "user_email": c.user_email, "tool": c.tool_name,
         "argv": (c.path or "").split(), "exit_code": None, "duration_ms": None,
         "where": "local", "client": c.client, "created_at": c.created_at.isoformat()}
        for c in local
    ]
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:limit]


# ---- OAuth connect flow (Phase C): mint the first token via browser consent --------------
router.routes.extend(connection_routes.oauth_router.routes)
router.routes.extend(connection_routes.resources_router.routes)
router.routes.extend(connection_routes.token_router.routes)
router.routes.extend(connection_routes.management_router.routes)
router.routes.extend(connection_routes.status_router.routes)


# ---- credential health (Phase B): validate all creds + alert owners ----------------------
router.routes.extend(connection_routes.health_router.routes)


# ---- super-admin: cross-tenant read + control (env token OR is_superadmin user) -----------
router.routes.extend(admin_routes.reads_router.routes)
router.routes.extend(admin_routes.mutations_router.routes)
router.routes.extend(admin_routes.reports_router.routes)


# ---- the proxy: call a tool without holding its credential; tier-4 metering ----------------
router.routes.extend(call_routes.router.routes)


# ---- server-side CLI execution (Tier 0 `treg run`) ---------------------------------------
class RunIn(BaseModel):
    tool: str             # the tool name in the caller's org (its `cli` profile drives execution)
    args: list[str] = []  # argv passed to the CLI (secrets are injected via env, never here)
    timeout_s: int | None = None


@app.post("/run")
async def run_tool_server(
    body: RunIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Run a tool's CLI **on the treg server**, with its `cli.inject` secrets injected into the
    child process — the caller never holds the key. Both run tiers read the same `Tool.cli`
    profile; any tool WITH a profile is server-runnable (no per-tool opt-in — unlike the local
    tier, the key never reaches the member, and the bin allow-list still gates what executes).
    See docs/CLI-RUN-PLAN.md.

    member+ (executing argv server-side is a register-tier capability, not a read); the sandbox is
    excluded (it never touches the real world). A non-zero CLI exit is a normal 200 result with
    `exit_code` set; only a failure to *start* (not enabled / CLI absent) is a 4xx."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="CLI run is disabled in the sandbox")
    tool = (
        await db.execute(select(Tool).where(Tool.name == body.tool, Tool.org_id == caller.org_id))
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no tool {body.tool!r} in this org")
    _require_tool_use_http(caller, tool)  # per-member tool + project ACL
    # A run executes a CLI, so there is no request path to match — evaluate the tool's own upstream
    # host, which is what a host-level rule ("nobody may reach api.stripe.com") is really saying.
    await _enforce_deny(caller, tool.base_url, "", db, tool.project_id)
    await _enforce_daily_cap(caller, db)  # a server run counts toward the per-user daily cap
    try:
        exec_bin = runner.resolve_exec_bin(tool)  # the SAME resolution run_tool execs — never diverges
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if exec_bin not in _allowed_server_bins():
        raise HTTPException(status_code=422, detail=(
            f"{exec_bin!r} is not approved for server runs — only catalog-known CLIs may run on the "
            "server (an admin can allow more via TREG_RUN_ALLOWED_BINS). Use `treg run --local` instead."))
    timeout = max(1, min(body.timeout_s or runner.DEFAULT_TIMEOUT_S, 600))
    try:
        async with runner.run_slot(caller.email):  # cap concurrent server runs (global + per-user)
            result = await runner.run_tool(tool, list(body.args), db, timeout_s=timeout)
    except runner.RunBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record_run(
        org_id=caller.org_id, user_email=caller.email, bundle_name=tool.name,
        argv=_redact_argv_list(list(body.args)),  # redact any credential typed inline before it's stored
        exit_code=result.exit_code, duration_ms=result.duration_ms, client=_client_of(request),
    )
    return {
        "tool": tool.name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }


# Deployment and imports keep using `treg.api:app`; the concrete assembly now lives in bootstrap.
from .bootstrap import create_app  # noqa: E402

app = create_app()
# Moved handlers retain api.py's original final global binding for calls such as app.openapi().
web_routes.app = app
