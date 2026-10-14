"""HTTP routes for first-run team onboarding."""

import re
from collections.abc import Awaitable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .. import sandbox as demo_sandbox
from ..application import onboard as onboard_use_cases
from ..application import signup as signup_use_cases
from ..application.onboard import first_run
from ..config import get_settings
from ..domain.identity.access import (
    Caller,
    _require_can_register,
    require_identity,
    require_member,
)
from ..models import User
from .auth import _client_ip
from .auth_helpers import require_managed_cli
from .orgs import _require_admin_of, _owned_team_limit_error, _signup_http_error
from .signup_cookies import REFERRAL_COOKIE
from ..domain.governance.teams import OwnedTeamLimitReached


# app is the APIRouter alias so mechanically moved @app decorators stay byte-identical.
app = APIRouter()


_ONBOARD_HTTP_ERRORS = {
    "org_not_found": (404, "org not found"),
    "not_demo_email": (400, "onboarding auto-accept is for demo teammates only"),
    "invite_not_found": (404, "no pending invite for that email"),
    "not_enabled": (404, "Not Found"),
    "already_onboarded": (409, "this account already has a team"),
    "bad_email": (400, "enter an email address"),
    "preview_not_found": (404, "no such preview"),
    "bad_call": (400, "a preview makes only the calls of its tasks"),
    "preview_spent": (429, "this preview has made its calls; start another"),
    "no_house_token": (503, "preview calls need TREG_ONBOARDING_TREG_TOKEN on this server"),
    "unknown_use_case": (400, "pick one of the listed use cases"),
    "not_started": (409, "start onboarding first"),
    "starting": (503, "your team is still being set up; try again"),
}


def _onboard_http_error(exc: onboard_use_cases.OnboardError) -> HTTPException:
    status_code, detail = _ONBOARD_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


_SANDBOX_HTTP_ERRORS = {
    "rate_limited": (429, "too many demo sandboxes from here — try again later"),
    "sandbox_only_live": (400, "live-wire info is for the landing-page sandbox only"),
    "bad_signature": (400, "bad signature"),
    "bad_payload": (400, "bad payload"),
    "sandbox_only_skill": (400, "skill export is for the landing-page sandbox only"),
}


def _sandbox_http_error(exc: onboard_use_cases.SandboxError) -> HTTPException:
    if exc.kind == "webhook_unconfigured":
        return HTTPException(status_code=404)
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


async def _first_run(answer: Awaitable[dict], response: Response | None = None) -> dict:
    """A first-run route's answer: an `OnboardError` as its HTTP error, and never cached."""
    try:
        result = await answer
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    return result


@app.post("/onboarding/start")
async def onboarding_start(
    request: Request, response: Response,
    user: User = Depends(require_identity),
) -> dict:
    """Behind `onboarding_v2`: make the new user's first team and start their setup lookup.
    Idempotent per user; answers what `GET /onboarding` answers."""
    require_managed_cli(request, team_change=True)
    try:
        result = await first_run.start(
            user,
            ad_cookie=request.cookies.get("treg_ad") or "",
            utm_cookie=request.cookies.get("treg_utm") or "",
            referral_cookie=request.cookies.get(REFERRAL_COOKIE) or "",
            landing_cookie=request.cookies.get("treg_landing") or "",
            http=request.app.state.http,
        )
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc
    except signup_use_cases.SignupError as exc:
        raise _signup_http_error(exc) from exc
    except OwnedTeamLimitReached as exc:
        raise _owned_team_limit_error() from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/onboarding")
async def onboarding_view(
    response: Response,
    user: User = Depends(require_identity),
) -> dict:
    """Behind `onboarding_v2`: the setup rows so far and, once the lookup is done, the ranked first
    tasks with their filled-in inputs. The dashboard polls it."""
    return await _first_run(first_run.view(user), response)


class AnswerIn(BaseModel):
    here_for: str


@app.post("/onboarding/answer")
async def onboarding_answer(
    body: AnswerIn, response: Response,
    user: User = Depends(require_identity),
) -> dict:
    """Behind `onboarding_v2`: what the new user says their agent is for, asked when nothing public
    grounded a task. Kept on their profile; it leads their tasks. Answers what `GET /onboarding` does."""
    return await _first_run(first_run.answer(user, body.here_for), response)


class PreviewIn(BaseModel):
    email: str


class PreviewCallIn(BaseModel):
    endpoint: str
    method: str = "POST"
    body: dict = {}
    query: dict = {}


@app.post("/onboarding/preview")
async def onboarding_preview_start(
    body: PreviewIn, request: Request, response: Response,
    user: User = Depends(require_identity),
) -> dict:
    """Run the first-run flow for any email without making a team (super-admins and the people
    `onboarding_v2_emails` lists; the house team pays)."""
    return await _first_run(first_run.preview_start(user, body.email, request.app.state.http), response)


@app.get("/onboarding/preview/{preview_id}")
async def onboarding_preview_view(
    preview_id: str, response: Response,
    user: User = Depends(require_identity),
) -> dict:
    return await _first_run(first_run.preview_view(user, preview_id), response)


@app.post("/onboarding/preview/{preview_id}/answer")
async def onboarding_preview_answer(
    preview_id: str, body: AnswerIn, response: Response,
    user: User = Depends(require_identity),
) -> dict:
    return await _first_run(first_run.preview_answer(user, preview_id, body.here_for), response)


@app.post("/onboarding/preview/{preview_id}/call")
async def onboarding_preview_call(
    preview_id: str, body: PreviewCallIn, request: Request,
    user: User = Depends(require_identity),
) -> dict:
    """A preview's first call, made as a house call: only calls the task library makes."""
    return await _first_run(first_run.preview_call(user, preview_id, body.model_dump(), request.app.state.http))


onboard_entry_router = app


# app is rebound so the landing-sandbox block keeps its original attachment point.
app = APIRouter()


# ---- landing-page sandbox studio: an anonymous, throwaway team the visitor builds ----------
# Per-IP limiter for the unauthenticated mint endpoint, in the DB (treg.ratestore) so it survives a
# restart and holds across instances (backlog #3). It caps DB churn from the public landing page (abuse
# is otherwise structurally contained — sandbox calls never touch the network, each sandbox is capped + TTL'd).
SANDBOX_HIT_NS = onboard_use_cases.SANDBOX_HIT_NS
SANDBOX_RATE_MAX = onboard_use_cases.SANDBOX_RATE_MAX
SANDBOX_RATE_WINDOW_S = onboard_use_cases.SANDBOX_RATE_WINDOW_S


@app.post("/demo/sandbox")
async def demo_sandbox_mint(request: Request) -> dict:
    """Mint a login-free, short-lived sandbox TEAM for the landing-page studio: a throwaway org + a
    starter secret + a starter endpoint + a member token, returned so the browser (and the visitor's
    terminal) can register more, call them, and export a skill — all with no account. Sandbox calls
    never touch the network (see call_tool → sandbox.synthesize); rate-limited per IP; GC'd after the
    TTL. No auth — this is the anonymous front door."""
    try:
        return await onboard_use_cases.mint_sandbox(client_ip=_client_ip(request))
    except onboard_use_cases.SandboxError as exc:
        raise _sandbox_http_error(exc) from exc


@app.get("/demo/sandbox/live")
async def demo_sandbox_live(caller: Caller = Depends(require_member)) -> dict:
    """Live-wire facts for an EXISTING sandbox (the browser reuses one via localStorage, so it may
    predate the mint response carrying them): is the wire on, and who am I in the feed."""
    try:
        return onboard_use_cases.sandbox_live_facts(caller.org)
    except onboard_use_cases.SandboxError as exc:
        raise _sandbox_http_error(exc) from exc


# ---- landing-page live payments feed (the public Stripe demo — see pubfeed.py) --------------
@app.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """Stripe → treg: a signed event from the demo sandbox account. Only `charge.succeeded` feeds
    the landing ticker; everything else is acknowledged and dropped. 404 when unconfigured, so a
    deploy without the secret exposes no unauthenticated POST surface."""
    try:
        return await onboard_use_cases.accept_stripe_event(
            payload_factory=request.body,
            signature=request.headers.get("stripe-signature", ""))
    except onboard_use_cases.SandboxError as exc:
        raise _sandbox_http_error(exc) from exc


@app.get("/landing/stripe-feed", include_in_schema=False)
async def landing_stripe_feed() -> StreamingResponse:
    """SSE stream for the landing demo pane: recent charges, then live ones. Unauthenticated by
    design — it carries only server-chosen fields (amount/currency/created/id-suffix)."""
    return StreamingResponse(onboard_use_cases.stripe_feed(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # tell the reverse proxy not to buffer the stream
    })


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


@app.get("/skills/samples")
async def skill_samples() -> list[dict]:
    """The hosted sample skills the landing offers — each with its files (SKILL.md/treg.json/.secret)
    and the prompt to try. Public: the landing renders these as file packages."""
    base = get_settings().public_url.rstrip("/")
    return onboard_use_cases.sample_skills(base=base)


@app.get("/skills/{name}/install.sh", include_in_schema=False)
async def skill_install(name: str, token: str = ""):
    """`curl -fsSL {BASE}/skills/<name>/install.sh?token=<t> | sh` — writes the skill into
    ./.claude/skills/<name>/ so Claude Code loads it. The token (if given) is baked into the
    recipe's calls; without it the recipe reads the token from `treg login`."""
    if name not in demo_sandbox.SAMPLE_SKILLS:
        raise HTTPException(status_code=404, detail=f"unknown skill {name!r}")
    # The token is interpolated into a shell script the visitor runs (`curl … | sh`). Restrict it to a
    # real token charset so a crafted value can't inject a newline + commands into the generated script.
    if token and not re.fullmatch(r"[A-Za-z0-9_\-]{1,200}", token):
        raise HTTPException(status_code=422, detail="invalid token")
    base = get_settings().public_url.rstrip("/")
    script = onboard_use_cases.sandbox_install_script(
        name=name, base=base, token=token or None)
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


sandbox_router = app

# The third router preserves the later attachment point after the landing-sandbox routes.
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
