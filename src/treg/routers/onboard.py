"""HTTP routes for first-run team onboarding."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import sandbox as demo_sandbox
from ..application import onboard as onboard_use_cases
from ..application import signup as signup_use_cases
from ..application.onboard import first_run
from ..domain.identity.access import (
    SANDBOX_CLOSED,
    Caller,
    _require_can_register,
    require_identity,
    require_member,
)
from ..models import User
from .auth_helpers import require_managed_cli
from .orgs import _require_admin_of, _owned_team_limit_error, _signup_http_error
from .signup_cookies import REFERRAL_COOKIE
from ..domain.governance.teams import OwnedTeamLimitReached


app = APIRouter()


_ONBOARD_HTTP_ERRORS = {
    "org_not_found": (404, "org not found"),
    "not_demo_email": (400, "onboarding auto-accept is for demo teammates only"),
    "invite_not_found": (404, "no pending invite for that email"),
    "sandbox_user": (403, (
        "the demo sandbox can't create a real team — sign in with GitHub, Google, or email to make one"
    )),
}


def _onboard_http_error(exc: onboard_use_cases.OnboardError) -> HTTPException:
    status_code, detail = _ONBOARD_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


_SANDBOX_HTTP_ERRORS = {
    "rate_limited": (429, "too many demo sandboxes from here — try again later"),
    "sandbox_only_live": (400, "live-wire info is for the landing-page sandbox only"),
    "sandbox_only_skill": (400, "skill export is for the landing-page sandbox only"),
}


def _sandbox_http_error(exc: onboard_use_cases.SandboxError) -> HTTPException:
    status_code, detail = _SANDBOX_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


class OnboardIn(BaseModel):
    team_name: str = "Acme Design"


@app.post("/onboard/demo")
async def onboard_demo(
    body: OnboardIn | None = None,
    user: User = Depends(require_identity),
) -> dict:
    """Seed a sandbox team owned by the caller — fake teammates (one per role) + a working `echo`
    tool + sample activity — so a brand-new user can feel the product immediately. Idempotent
    (reuses an existing demo team); marks the caller onboarded. Same seed for dashboard + CLI."""
    try:
        return await onboard_use_cases.provision_demo(
            user_id=user.id, team_name=(body.team_name if body else "Acme Design"))
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc
    except OwnedTeamLimitReached as exc:
        raise _owned_team_limit_error() from exc


@app.post("/onboard/skip")
async def onboard_skip(
    user: User = Depends(require_identity),
) -> dict:
    """Dismiss onboarding without seeding — so it's never auto-offered again."""
    return await onboard_use_cases.skip(user_id=user.id)


@app.post("/onboard/reset")
async def onboard_reset(
    user: User = Depends(require_identity),
) -> dict:
    """Remove the caller's demo team(s) + demo teammates from their real teams — a clean exit."""
    return await onboard_use_cases.reset(user_id=user.id)


@app.post("/onboard/team")
async def onboard_team(
    request: Request, response: Response,
    user: User = Depends(require_identity),
) -> dict:
    """A new account's first team, named for it, so the dashboard never asks for a name.
    Idempotent per user: `created` is false when the account already has its first run."""
    require_managed_cli(request, team_change=True)
    try:
        result = await first_run.first_team(
            user,
            ad_cookie=request.cookies.get("treg_ad") or "",
            utm_cookie=request.cookies.get("treg_utm") or "",
            referral_cookie=request.cookies.get(REFERRAL_COOKIE) or "",
        )
    except signup_use_cases.SignupError as exc:
        raise _signup_http_error(exc) from exc
    except OwnedTeamLimitReached as exc:
        raise _owned_team_limit_error() from exc
    response.headers["Cache-Control"] = "no-store"
    return result


onboard_entry_router = app


app = APIRouter()


@app.post("/demo/sandbox", status_code=410)
async def demo_sandbox_mint() -> dict:
    """The landing sandbox is closed: no new sandbox is minted."""
    raise HTTPException(status_code=410, detail=SANDBOX_CLOSED)


@app.get("/demo/sandbox/live")
async def demo_sandbox_live(caller: Caller = Depends(require_member)) -> dict:
    """Live-wire facts for an EXISTING sandbox (the browser reuses one via localStorage, so it may
    predate the mint response carrying them): is the wire on, and who am I in the feed."""
    try:
        return onboard_use_cases.sandbox_live_facts(caller.org)
    except onboard_use_cases.SandboxError as exc:
        raise _sandbox_http_error(exc) from exc


@app.get("/demo/sandbox/skill")
async def demo_sandbox_skill(
    caller: Caller = Depends(require_member),
) -> dict:
    """Export whatever the visitor built in their sandbox as a shareable **skill** (treg.json manifest
    + SKILL.md + install commands). Sandbox-only — the payoff that shows what skills are."""
    try:
        return await onboard_use_cases.export_sandbox_skill(
            org_id=caller.org_id, sandbox=demo_sandbox.is_sandbox(caller.org))
    except onboard_use_cases.SandboxError as exc:
        raise _sandbox_http_error(exc) from exc


sandbox_router = app

app = APIRouter()


class TeammateIn(BaseModel):
    email: str


@app.post("/onboard/seed-tool")
async def onboard_seed_tool(
    caller: Caller = Depends(require_member),
) -> dict:
    """Pre-seed the working `echo` tool into the caller's active team so the no-key call in the
    dashboard onboarding just works (the user builds the team + invites by hand; the tool is on us)."""
    _require_can_register(caller)
    try:
        return await onboard_use_cases.seed_tool(
            org_id=caller.org_id, owner_email=caller.email)
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc


@app.post("/onboard/accept-teammate")
async def onboard_accept_teammate(
    body: TeammateIn, caller: Caller = Depends(require_member),
) -> dict:
    """Auto-accept the fake teammate the user just invited during onboarding, so it lands in the
    roster instantly (they feel the invite, then see the loop close). Admin+ only, demo email only."""
    _require_admin_of(caller.org_id, caller)
    try:
        return await onboard_use_cases.accept_teammate(
            org_id=caller.org_id, email=body.email)
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc


onboard_teammate_router = app
