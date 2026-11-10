"""Team SSO purchase, setup, sign-in and signed directory event routes."""

import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import select

from .. import client_ip, crypto, ratestore
from ..application import enterprise_billing, enterprise_sso
from ..config import get_settings
from ..domain.identity import session as identity_session
from ..domain.identity.access import Caller, _user_from_session, require_member
from ..infra import workos
from ..infra.db import session_maker
from ..models import EnterpriseSSO, Org
from .auth_helpers import _is_https, _same_origin
from .auth import _arena_return_target
from .billing import _return_base


router = APIRouter()
STATE_COOKIE = "treg_sso_state"


def _signin_failure(reason: str, slug: str = "") -> RedirectResponse:
    query = {"sso_error": reason}
    if slug:
        query["team"] = slug
    response = RedirectResponse("/?" + urlencode(query), status_code=303)
    response.delete_cookie(STATE_COOKIE)
    response.headers["Cache-Control"] = "no-store"
    return response


class CheckoutIn(BaseModel):
    plan: str
    price_id: str


class EnforceIn(BaseModel):
    enabled: bool


class DiscoverIn(BaseModel):
    email: str


class ITContactIn(BaseModel):
    email: str


def _owner(caller: Caller, org_id: int) -> None:
    if caller.org_id != org_id or caller.role != "owner":
        raise HTTPException(403, "team owner required")


@router.get("/orgs/{org_id}/sso")
async def sso_status(org_id: int, caller: Caller = Depends(require_member)) -> dict:
    if caller.org_id != org_id:
        raise HTTPException(403, "team membership required")
    return await enterprise_billing.status(org_id, include_plans=caller.role == "owner")


@router.get("/orgs/{org_id}/sso/blocked")
async def sso_blocked_members(org_id: int, caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    return {"members": await enterprise_sso.blocked_members(org_id)}


@router.post("/orgs/{org_id}/sso/blocked/{user_id}/restore")
async def sso_restore_member(org_id: int, user_id: int, request: Request,
                             caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        await enterprise_sso.unblock_member(org_id, user_id, caller.user.id)
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"restored": user_id}


@router.post("/orgs/{org_id}/sso/checkout")
async def sso_checkout(org_id: int, body: CheckoutIn, request: Request,
                       caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        return await enterprise_billing.start_checkout(org_id, body.plan, price_id=body.price_id,
            base=_return_base(request), email=caller.email)
    except enterprise_billing.EnterpriseBillingError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/orgs/{org_id}/sso/setup/{intent}")
async def sso_setup(org_id: int, intent: str, request: Request,
                    caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    settings = get_settings()
    if not settings.workos_api_key:
        raise HTTPException(503, "WorkOS is not configured")
    try:
        org, config = await enterprise_sso.configured_team(org_id)
        if intent not in {"sso", "dsync"} or (intent == "dsync" and config.plan != "sso_directory"):
            raise HTTPException(422, "setup option is unavailable")
        workos_org_id = config.workos_org_id
        async with httpx.AsyncClient() as client:
            if not workos_org_id:
                workos_org_id = await workos.create_organization(client,
                    key=settings.workos_api_key, name=org.name, org_id=org_id)
                await enterprise_sso.bind_workos_org(org_id, workos_org_id)
            link = await workos.portal_link(client, key=settings.workos_api_key,
                organization=workos_org_id, intent=intent,
                return_url=settings.public_url.rstrip("/") + "/app#sso")
        await enterprise_sso.record_action(org_id, "sso.setup_link_generated", actor=caller.user.id)
        return {"url": link}
    except (enterprise_sso.EnterpriseSSOError, workos.WorkOSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/orgs/{org_id}/sso/invite-it")
async def sso_invite_it(org_id: int, body: ITContactIn, request: Request,
                        caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    email = body.email.strip().lower()
    if len(email) > 254 or email.count("@") != 1 or not email.split("@", 1)[0] or "." not in email.rsplit("@", 1)[1]:
        raise HTTPException(422, "enter the IT admin's email")
    settings = get_settings()
    if not settings.workos_api_key:
        raise HTTPException(503, "WorkOS is not configured")
    try:
        org, config = await enterprise_sso.configured_team(org_id)
        workos_org_id = config.workos_org_id
        async with httpx.AsyncClient() as client:
            if not workos_org_id:
                workos_org_id = await workos.create_organization(client,
                    key=settings.workos_api_key, name=org.name, org_id=org_id)
                await enterprise_sso.bind_workos_org(org_id, workos_org_id)
            await workos.invite_it_contact(client, key=settings.workos_api_key,
                organization=workos_org_id, email=email,
                directory_sync=config.plan == "sso_directory")
        await enterprise_sso.record_action(org_id, "sso.it_contact_invited", actor=caller.user.id)
        return {"invited": True}
    except (enterprise_sso.EnterpriseSSOError, workos.WorkOSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/auth/sso/idp/start")
async def sso_idp_start(request: Request, connection_id: str = ""):
    """An Okta app tile supplies a connection hint, then starts our normal checked flow."""
    if not connection_id or len(connection_id) > 100:
        raise HTTPException(404, "company sign-in not found")
    async with session_maker() as db:
        config = (await db.execute(select(EnterpriseSSO).where(
            EnterpriseSSO.connection_id == connection_id,
            EnterpriseSSO.subscription_status.in_(enterprise_sso.PAID_STATUSES),
        ))).scalar_one_or_none()
        org = await db.get(Org, config.org_id) if config else None
        if org is None or org.suspended:
            raise HTTPException(404, "company sign-in not found")
        slug = org.slug
    return await sso_start(slug, request)


@router.get("/auth/sso/{org_slug}/start")
async def sso_start(org_slug: str, request: Request, return_to: str = ""):
    settings = get_settings()
    if not settings.workos_api_key or not settings.workos_client_id:
        raise HTTPException(503, "SSO is not configured")
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.slug == org_slug))).scalar_one_or_none()
        if org is None:
            raise HTTPException(404, "team not found")
        org_id = org.id
    try:
        _, config = await enterprise_sso.configured_team(org_id)
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not config.workos_org_id:
        raise HTTPException(409, "SSO setup is incomplete")
    nonce = secrets.token_urlsafe(32)
    cookie = crypto.encrypt(json.dumps({"org_id": org_id, "slug": org_slug, "nonce": nonce,
                                        "return_to": _arena_return_target(return_to),
                                        "expires": int(time.time()) + 600}))
    callback = settings.public_url.rstrip("/") + "/auth/sso/callback"
    url = workos.authorization_url(client_id=settings.workos_client_id,
        organization=config.workos_org_id, redirect_uri=callback, state=nonce)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(STATE_COOKIE, cookie, httponly=True, secure=_is_https(request),
                        samesite="lax", max_age=600)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/sso/discover")
async def sso_discover(body: DiscoverIn, request: Request) -> dict:
    """Email routes a visitor to a connection; only the WorkOS callback can grant access."""
    email = body.email.strip().lower()
    if len(email) > 254 or email.count("@") != 1 or not email.split("@", 1)[0]:
        raise HTTPException(422, "enter a valid work email")
    domain = email.rsplit("@", 1)[1].strip(".")
    if not domain or "." not in domain or len(domain) > 253:
        raise HTTPException(422, "enter a valid work email")
    async with session_maker() as db:
        await ratestore.sweep(db, "sso_discover")
        allowed = await ratestore.rate_check(db, "sso_discover",
            [(f"ip:{client_ip.of(request)}", 30)], 900)
        await db.commit()
    if not allowed:
        raise HTTPException(429, "too many sign-in lookups; try again later")
    settings = get_settings()
    if not settings.workos_api_key:
        raise HTTPException(503, "company sign-in is unavailable")
    try:
        async with httpx.AsyncClient() as client:
            organizations = await workos.verified_organizations_for_domain(client,
                key=settings.workos_api_key, domain=domain)
    except workos.WorkOSError as exc:
        raise HTTPException(503, "company sign-in is temporarily unavailable") from exc
    organization_ids = {item.get("id") for item in organizations}
    if not organization_ids:
        return {"teams": []}
    async with session_maker() as db:
        matches = (await db.execute(select(Org, EnterpriseSSO).join(
            EnterpriseSSO, EnterpriseSSO.org_id == Org.id).where(
                EnterpriseSSO.workos_org_id.in_(organization_ids),
                EnterpriseSSO.subscription_status.in_(enterprise_sso.PAID_STATUSES),
                Org.suspended.is_(False),
        ))).all()
    return {"teams": sorted(({"name": org.name, "slug": org.slug}
        for org, _ in matches), key=lambda team: (team["name"].lower(), team["slug"]))}


@router.get("/auth/sso/callback")
async def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    cookie = request.cookies.get(STATE_COOKIE, "")
    try:
        saved = json.loads(crypto.decrypt(cookie))
        if not state or not secrets.compare_digest(state, saved["nonce"]):
            raise ValueError("state")
        if int(saved["expires"]) < time.time():
            raise ValueError("expired")
        org_id = int(saved["org_id"])
    except (InvalidToken, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _signin_failure("expired")
    if not code:
        await enterprise_sso.record_action(org_id, "sso.signin_failed")
        return _signin_failure(
            "domain" if error == "profile_not_allowed_outside_organization" else "failed",
            saved.get("slug", ""),
        )
    settings = get_settings()
    try:
        async with httpx.AsyncClient() as client:
            profile = await workos.exchange_code(client, key=settings.workos_api_key,
                                                 client_id=settings.workos_client_id, code=code)
            organization = await workos.get_organization(client, key=settings.workos_api_key,
                                                         organization=profile.get("organization_id", ""))
        original = identity_session.read_session_claims(request.cookies.get(identity_session.COOKIE, ""))
        proven_user_id = None
        if original is not None and original.get("sso_org_id") is None:
            async with session_maker() as db:
                user = await _user_from_session(request.cookies.get(identity_session.COOKIE, ""), db)
                proven_user_id = user.id if user is not None else None
        result = await enterprise_sso.finish_signin(org_id, profile, organization=organization,
                                                    proven_user_id=proven_user_id)
    except enterprise_sso.EnterpriseSSOError as exc:
        await enterprise_sso.record_action(org_id, "sso.signin_failed")
        reason = "link" if "sign in to your existing treg account" in str(exc) else "failed"
        return _signin_failure(reason, saved.get("slug", ""))
    except workos.WorkOSError:
        await enterprise_sso.record_action(org_id, "sso.signin_failed")
        return _signin_failure("failed", saved.get("slug", ""))
    response = RedirectResponse(_arena_return_target(saved.get("return_to", "")) or "/app?sso=success", status_code=303)
    response.delete_cookie(STATE_COOKIE)
    response.set_cookie(identity_session.COOKIE,
        identity_session.make_session(result.user_id, token_version=result.token_version,
                                      sso_org_id=result.org_id),
        httponly=True, secure=_is_https(request), samesite="lax",
        max_age=identity_session.TTL_SECONDS)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/orgs/{org_id}/sso/enforce")
async def sso_enforce(org_id: int, body: EnforceIn, request: Request,
                      caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        return {"enforced": await enterprise_sso.set_enforced(org_id, body.enabled, caller.user.id)}
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/orgs/{org_id}/sso/cancel")
async def sso_cancel(org_id: int, request: Request,
                     caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        return await enterprise_billing.cancel_at_period_end(org_id)
    except enterprise_billing.EnterpriseBillingError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/auth/sso/workos/webhook", include_in_schema=False)
async def sso_workos_webhook(request: Request) -> dict:
    secret = get_settings().workos_webhook_secret
    if not secret:
        raise HTTPException(404)
    try:
        event = workos.verify_event(await request.body(),
                                    request.headers.get("workos-signature", ""), secret)
        return {"received": True, "processed": await enterprise_sso.apply_directory_event(event)}
    except workos.WorkOSError as exc:
        raise HTTPException(400, "invalid WorkOS webhook") from exc
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(422, str(exc)) from exc
