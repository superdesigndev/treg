"""HTTP routes for first-run team onboarding."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..application import onboard as onboard_use_cases
from ..application import signup as signup_use_cases
from ..application.onboard import first_run
from ..domain.identity.access import SANDBOX_CLOSED, require_identity
from ..models import User
from .auth_helpers import require_managed_cli
from .orgs import _owned_team_limit_error, _signup_http_error
from .signup_cookies import REFERRAL_COOKIE
from ..domain.governance.teams import OwnedTeamLimitReached


app = APIRouter()


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


sandbox_router = app
