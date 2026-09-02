"""Authentication HTTP routes and presentation helpers."""

from __future__ import annotations

import hashlib
import hmac
import re

from fastapi import APIRouter, Cookie, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import auth as auth_use_cases
from ..application import signup
from ..application.auth import (
    CLI_APPROVE_MAX_TRIES,
    CLI_TOKEN_TTL,
    EMAIL_CODE_TTL,
    HANDSHAKE_TTL,
    MAX_OTP_ATTEMPTS,
    OTP_NS,
    OTP_START_MAX_PER_EMAIL,
    OTP_START_MAX_PER_IP,
    OTP_START_NS,
    OTP_START_WINDOW_S,
)
from ..config import PUBLIC_HOST_ALIASES, get_settings
from ..domain.identity import mcp_oauth
from ..domain.identity import session as sess
from ..domain.identity.access import require_identity
from ..domain.identity.mcp_oauth import REFRESH_TTL_S
from ..models import User
from .auth_helpers import _is_https, _remember_oauth_return, _same_origin
from .signup_cookies import REFERRAL_COOKIE
from .web import _esc_html

# The app alias preserves the moved handlers' original @app.post decorator text byte-for-byte.
app = APIRouter()
email_router = app


# ---- human login via email one-time code (the third identity door) ------------------------
# OTP code + its brute-force counter, and the /auth/email/start throttle, live in the DB (treg.ratestore
# over the Ephemeral table) — NOT per-process dicts — so a restart can't reset them and they stay correct
# across instances (backlog #3). The 'otp' namespace holds {code_hash, attempts} keyed by email; the
# 'otp_start' namespace holds the per-email + per-IP sliding windows (email-bomb + brute-force guard).


class EmailStartIn(BaseModel):
    email: str


class EmailVerifyIn(BaseModel):
    email: str
    code: str


_EMAIL_HTTP_ERRORS = {
    "demo_address": (400, "that's a demo address — pick a real email"),
    "machine_identity": (403, "this address cannot be used to sign in"),
    "rate_limited": (429, "too many code requests — please wait a few minutes"),
    "invalid_code": (401, "invalid code"),
    "suspended": (403, "account suspended"),
}


def _email_http_error(exc: auth_use_cases.EmailAuthError) -> HTTPException:
    status_code, detail = _EMAIL_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


@app.post("/auth/email/start")
async def auth_email_start(
    request: Request, body: EmailStartIn,
) -> dict:
    """Prove ownership of an email: mint a 6-digit code. With no mail sender yet, dev mode returns
    + logs it (so dummy emails are testable); prod will email it instead. Throttled per-email AND per-IP
    (sliding window) so this open endpoint can't be used to email-bomb an inbox or reset the OTP
    brute-force counter at will. All this state is in the DB (survives restart, correct multi-instance)."""
    try:
        return await auth_use_cases.start_email_login(body.email, _client_ip(request))
    except auth_use_cases.EmailAuthError as exc:
        raise _email_http_error(exc) from exc


@app.post("/auth/email/verify")
async def auth_email_verify(
    request: Request, body: EmailVerifyIn,
) -> JSONResponse:
    """Check the code → find-or-create the user → mint an identity token AND set a browser session
    cookie. The CLI reads the token from the body; the dashboard just reloads into session mode
    (same path as GitHub login) — one endpoint serves both clients."""
    try:
        verified = await auth_use_cases.verify_email_login(body.email, body.code)
    except auth_use_cases.EmailAuthError as exc:
        raise _email_http_error(exc) from exc
    # Same first-team guarantee as the OAuth doors (this one is a POST, so a POST-blocking network
    # never reaches it — but a user who signed up here and signs in via OAuth later must not differ).
    await _ensure_first_team(request, verified.email)
    resp = JSONResponse({"token": verified.token, "email": verified.email})
    resp.set_cookie(sess.COOKIE, verified.session_cookie, httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


async def _find_or_create_user(db: AsyncSession, email: str) -> User:
    """Find a user by email, else register them — the user ONLY, **no auto personal org**. The shared
    core of every identity door (GitHub / Google / email OTP). A brand-new user therefore lands with
    zero teams and is asked to NAME + CREATE their first team (the dashboard's mandatory welcome, or
    `treg org create`) — we never spawn a throwaway personal org they didn't ask for. Their identity
    token is user-scoped, so it works before they have any org (org chosen per-request via X-Treg-Org).
    Caller commits."""
    try:
        return await signup.find_or_create_user(db, email)
    except signup.MachineIdentityError as exc:
        raise HTTPException(status_code=403, detail="this address cannot be used to sign in") from exc


def _client_ip(request: Request) -> str:
    """Best-effort client IP — first hop of X-Forwarded-For behind the reverse proxy (Render), else the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


# The pairing state machine belongs to the application use case. These aliases keep the staged
# api.py compatibility exports and the social-login handshake on the exact same mutable objects.
_cli_states = auth_use_cases._cli_states
_cli_results = auth_use_cases._cli_results
_cli_pending = auth_use_cases._cli_pending
_PAIR_ALPHABET = auth_use_cases._PAIR_ALPHABET
_prune_handshakes = auth_use_cases._prune_handshakes
_norm_pair_code = auth_use_cases._norm_pair_code
_orgs_brief = auth_use_cases._orgs_brief


_AUTH_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>tools-registry</title>'
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">'
    "<style>"
    ':root{--bg:#151412;--panel:#1c1b19;--ink:#f2efe8;--muted:rgba(242,239,232,.55);'
    '--line:rgba(255,255,255,.1);--accent:#19D0E8;'
    '--mono:"DM Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}'
    "html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:var(--mono)}"
    "body{background:radial-gradient(90% 50% at 50% -10%,rgba(255,255,255,.04),transparent 60%),var(--bg)}"
    ".wrap{min-height:100%;display:flex;align-items:center;justify-content:center;padding:24px}"
    ".card{background:linear-gradient(180deg,#201f1d,#171614);border:1px solid var(--line);border-radius:20px;"
    "padding:34px 40px;max-width:440px;text-align:center;"
    "box-shadow:rgba(255,255,255,.08) 0 1px 0 inset, 0 30px 70px rgba(0,0,0,.5)}"
    ".logo{color:var(--accent);font-size:15px;letter-spacing:.5px;margin-bottom:18px}"
    ".mark{font-size:34px;line-height:1;margin-bottom:14px}"
    'h1{font-family:"Geist Pixel",var(--mono);font-size:22px;margin:0 0 8px;font-weight:400;letter-spacing:0}'
    "p{color:var(--muted);font-size:13.5px;line-height:1.55;margin:0}"
    ".pbtn{display:inline-block;background:linear-gradient(180deg,#fdfcf7,#eae7de);color:#1c1b19;border:0;"
    "border-radius:999px;padding:12px 24px;font:500 14px var(--mono);cursor:pointer;"
    "box-shadow:rgba(178,168,165,.2) -1.3px -1.3px 2.5px 0, rgba(0,0,0,.4) 2px 2px 1.5px 0}"
    "</style></head>"
)


def _login_callback_base(request: Request) -> str:
    """The base URL a GitHub/Google login round-trip is anchored to. Normally `public_url` — but
    the provider compares the exchange's `redirect_uri` byte-for-byte against the one the
    authorization request named, so a flow living on a legacy host (a login in flight across the
    cutover deploy, with its state cookie and provider registration both on the old name) must
    keep building the OLD host's callback. Any recognized alias Host therefore wins — in BOTH
    directions, so a login minted on treg.to also survives a TREG_PUBLIC_URL rollback."""
    host = request.headers.get("host", "").split(":")[0].rstrip(".").lower()
    if host in PUBLIC_HOST_ALIASES:
        return f"https://{host}"
    return get_settings().public_url.rstrip("/")


# The app alias preserves the moved handlers' original @app.get decorators byte-for-byte.
app = APIRouter()
social_router = app


_SOCIAL_HTTP_ERRORS = {
    "github_not_configured": (503, "GitHub login not configured"),
    "google_not_configured": (503, "Google login not configured"),
    "machine_identity": (403, "this address cannot be used to sign in"),
}

_SOCIAL_PAGE_ERRORS = {
    "bad_state": ("Login failed", "Bad state. Please start the login again.", False, 400),
    "github_no_access_token": ("Login failed", "No access token from GitHub.", False, 400),
    "github_no_verified_email": ("Login failed", "No verified email on your GitHub account.", False, 400),
    "google_no_access_token": ("Login failed", "No access token from Google.", False, 400),
    "google_no_email": ("Login failed", "No email on your Google account.", False, 400),
    "google_unverified_email": ("Login failed", "Your Google email isn't verified.", False, 400),
    "callback_failed": ("Login failed", "Something went wrong. Please try again.", False, 502),
    "suspended": ("Account suspended", "This account has been suspended.", False, 403),
}


def _social_http_error(exc: auth_use_cases.SocialLoginError) -> HTTPException:
    status_code, detail = _SOCIAL_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


# ---- human login via GitHub OAuth (dashboard sessions) ------------------------------------
@app.get("/auth/github")
async def auth_github(request: Request, cli: str = ""):
    try:
        started = auth_use_cases.start_github_login(cli, lambda: _login_callback_base(request))
    except auth_use_cases.SocialLoginError as exc:
        raise _social_http_error(exc) from exc
    resp = RedirectResponse(started.url, status_code=302)
    resp.set_cookie("treg_oauth_state", started.state, httponly=True, max_age=600,
                    samesite="lax", secure=_is_https(request))
    return resp


def _auth_page(headline: str, sub: str = "", *, ok: bool = True, status: int = 200) -> HTMLResponse:
    """A brand-styled full-page response for the browser-facing auth flow (GitHub callback)."""
    sub_html = f"<p>{sub}</p>" if sub else ""
    html = (
        f'{_AUTH_HEAD}<body><div class="wrap"><div class="card">'
        f'<div class="logo">▚ tools-registry</div><div class="mark">{"✅" if ok else "⚠️"}</div>'
        f"<h1>{headline}</h1>{sub_html}</div></div></body></html>"
    )
    return HTMLResponse(html, status_code=status)


async def _ensure_first_team(request: Request, email: str) -> None:
    """A browser sign-in with no team gets one made server-side, on this GET. The welcome modal's
    own POST /orgs never arrives from behind some corporate secure-web-gateways (GETs pass, POSTs
    are silently swallowed), which stranded signups on a spinner — so the team is created here and
    the modal only renames it. Never fails the login (the use case swallows its own errors)."""
    await signup.ensure_first_team(
        email=email,
        ad_cookie=request.cookies.get("treg_ad") or "",
        utm_cookie=request.cookies.get("treg_utm") or "",
        referral_cookie=request.cookies.get(REFERRAL_COOKIE) or "",
    )


def _finish_oauth_login(request: Request, user: User, st: tuple | None) -> RedirectResponse:
    """After a GitHub/Google callback proves an identity: set the browser session cookie, then either
    land on the dashboard (a plain browser login) or bounce to /login?cli=<id> so a `treg login`
    handshake goes through the SAME team picker as the other doors (instead of completing blind — which
    would leave the CLI guessing the org). The picker's POST /auth/cli/approve reads this same cookie."""
    login_id = st[0] if st is not None else None
    dest = f"/login?cli={login_id}" if login_id else "/app"
    resp = RedirectResponse(dest, status_code=302)
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    resp.delete_cookie("treg_oauth_state")
    return resp


def _social_login_failure(exc: auth_use_cases.SocialLoginError) -> HTMLResponse:
    if exc.kind == "machine_identity":
        raise _social_http_error(exc) from exc
    headline, sub, ok, status = _SOCIAL_PAGE_ERRORS[exc.kind]
    return _auth_page(headline, sub, ok=ok, status=status)


@app.get("/auth/github/callback")
async def auth_github_callback(
    request: Request, code: str = "", state: str = "",
    treg_oauth_state: str = Cookie(default=""),
):
    try:
        proof = await auth_use_cases.complete_github_login(
            lambda: request.app.state.http, code, state, treg_oauth_state,
            lambda: _login_callback_base(request),
        )
    except auth_use_cases.SocialLoginError as exc:
        return _social_login_failure(exc)
    await _ensure_first_team(request, proof.user.email)
    return _finish_oauth_login(request, proof.user, proof.cli_state)


@app.get("/auth/google")
async def auth_google(request: Request, cli: str = ""):
    """Human login via Google OAuth — a parallel door to GitHub, same session/CLI-handshake plumbing."""
    try:
        started = auth_use_cases.start_google_login(cli, lambda: _login_callback_base(request))
    except auth_use_cases.SocialLoginError as exc:
        raise _social_http_error(exc) from exc
    resp = RedirectResponse(started.url, status_code=302)
    resp.set_cookie("treg_oauth_state", started.state, httponly=True, max_age=600,
                    samesite="lax", secure=_is_https(request))
    return resp


@app.get("/auth/google/callback")
async def auth_google_callback(
    request: Request, code: str = "", state: str = "",
    treg_oauth_state: str = Cookie(default=""),
):
    try:
        proof = await auth_use_cases.complete_google_login(
            lambda: request.app.state.http, code, state, treg_oauth_state,
            lambda: _login_callback_base(request),
        )
    except auth_use_cases.SocialLoginError as exc:
        return _social_login_failure(exc)
    await _ensure_first_team(request, proof.user.email)
    return _finish_oauth_login(request, proof.user, proof.cli_state)


# The app alias preserves the moved handlers' original @app decorators byte-for-byte.
app = APIRouter()
cli_router = app


@app.post("/auth/cli/start")
async def auth_cli_start() -> dict:
    """`treg login` calls this FIRST. The SERVER mints both the login_id and a short pairing code and
    remembers them (pending approval). The code is shown only in that terminal; the browser must echo it
    back at approve time, where it's validated server-side, before any token is issued. So a login the
    user didn't start (a phished /login?cli=<id> link) can't be completed, and the poll endpoint carries
    no code to brute-force. Unauthenticated — on its own it grants nothing."""
    return await auth_use_cases.start_cli_login()


@app.get("/auth/cli/poll")
async def auth_cli_poll(login_id: str = "") -> dict:
    """The CLI polls this after opening the browser; returns the identity token once, then forgets it.
    A token only lands here after auth_cli_approve validated the terminal pairing code, so a login the
    user didn't approve never yields one — there is nothing here to brute-force (no code parameter)."""
    return await auth_use_cases.poll_cli_login(login_id)


# `treg login` mints the login_id with token_urlsafe(18) (24 chars); anything outside this shape is
# not one of ours. It's echoed into the /login page's JS, so the whitelist is also the XSS guard.
_LOGIN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


class CliApproveIn(BaseModel):
    login_id: str
    code: str | None = None  # the pairing code the user copied from their terminal (phishing guard)
    org: str | None = None  # the team slug the user picked in the /login org picker (optional)


@app.get("/auth/cli/orgs")
async def auth_cli_orgs(treg_session: str = Cookie(default="")) -> dict:
    """The /login page fetches this (session-cookie authed) to render the team picker before completing
    a `treg login` handshake. Returns the signed-in user's teams; empty list if no session."""
    return await auth_use_cases.cli_orgs(treg_session)


_CLI_HTTP_ERRORS = {
    "no_session": (401, "no session"),
    "expired": (400, "this login has expired — run `treg login` again"),
    "too_many_wrong_codes": (400, "too many wrong codes — run `treg login` again"),
    "wrong_code": (400, "that code doesn't match the one in your terminal"),
    "not_member": (403, "not a member of that team"),
}


def _cli_http_error(exc: auth_use_cases.CliPairingError) -> HTTPException:
    status_code, detail = _CLI_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


@app.post("/auth/cli/approve")
async def auth_cli_approve(
    request: Request, body: CliApproveIn,
    treg_session: str = Cookie(default=""),
) -> dict:
    """Complete a `treg login` handshake from an EXISTING browser session (the "Continue as" button
    on /login, and the email door after /auth/email/verify sets the cookie). Deliberately a POST with
    a same-origin check — auto-completing on a GET would let a phisher mail out /login?cli=<their-id>
    and poll the victim's identity token straight out of /auth/cli/poll.

    `org` (optional) is the team slug the user picked in the /login org picker; it's validated to be
    one of the user's memberships and passed back to the CLI so it lands on the RIGHT team instead of
    guessing (`_pick_active_org`)."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin approve rejected")
    if not _LOGIN_ID_RE.fullmatch(body.login_id or ""):
        raise HTTPException(status_code=400, detail="bad login_id")
    try:
        return await auth_use_cases.approve_cli_login(
            treg_session, body.login_id, body.code, body.org,
        )
    except auth_use_cases.CliPairingError as exc:
        raise _cli_http_error(exc) from exc


@app.get("/login", include_in_schema=False)
async def login_page(cli: str = "", treg_session: str = Cookie(default="")):
    """The universal sign-in page `treg login` opens: reuses an existing dashboard session with one
    click ("Continue as …"), else offers every configured door — GitHub, Google, email one-time code.
    The email door is always present, so login works even with no OAuth app configured."""
    if not cli:
        return RedirectResponse("/app", status_code=302)  # a bare visit belongs on the dashboard
    if not _LOGIN_ID_RE.fullmatch(cli):
        return _auth_page("Login failed", "Bad login link. Run <code>treg login</code> again.", ok=False, status=400)
    s = get_settings()
    session_email = await auth_use_cases.cli_session_email(treg_session)
    return HTMLResponse(_login_page_html(
        cli, session_email=session_email,
        github=bool(s.github_client_id), google=bool(s.google_client_id)))


def _login_page_html(login_id: str, *, session_email: str | None, github: bool, google: bool) -> str:
    """Server-rendered /login card. login_id is whitelist-validated by the caller; the session email
    is HTML-escaped (it's the only other interpolated value)."""
    from html import escape

    # A pairing-code block sits above everything: whichever door the user takes, approve() won't complete
    # the CLI handshake until the code shown in their own terminal is echoed back (phishing guard — a
    # login they didn't start has no matching code). A `treg login` link carries the code in the URL
    # fragment, so the JS swaps this input for a read-only display the user just visually confirms;
    # the typed input remains the fallback for links without one. #orgpick is ALWAYS present (filled by loadOrgs when a
    # session exists at load, and after the email door signs in). #doors holds the sign-in options; the
    # divider only shows when a session pre-exists.
    parts: list[str] = [
        '<div id="pcbox"><div class="pklabel">Enter the code shown in your terminal:</div>'
        '<input id="paircode" autocomplete="off" autocapitalize="characters" spellcheck="false" '
        'inputmode="latin" placeholder="e.g. 7F3K" maxlength="9"></div>',
        '<div id="orgpick"></div>',
    ]
    # With a live session the doors are noise — the user is one click from done. Collapse them behind
    # the divider (an accordion); a click expands. No session → no divider, doors always visible.
    if session_email:
        parts.append('<div class="div acc" id="other-acct" onclick="toggleDoors()" role="button" tabindex="0" '
                     'onkeydown="if(event.key===\'Enter\')toggleDoors()">'
                     'use a different account <span id="acc-caret">▸</span></div>')
    doors: list[str] = []
    if github:
        doors.append(f'<a class="btn" href="/auth/github?cli={login_id}">Sign in with GitHub</a>')
    if google:
        doors.append(f'<a class="btn" href="/auth/google?cli={login_id}">Sign in with Google</a>')
    doors.append(
        '<div id="email-door">'
        '<div id="email-row"><input id="em" type="email" placeholder="you@company.com" autocomplete="email">'
        '<button class="btn" onclick="sendCode()">Email me a code</button></div>'
        '<div id="code-row" style="display:none"><input id="code" inputmode="numeric" placeholder="6-digit code">'
        '<button class="btn primary" onclick="verifyCode()">Verify</button></div>'
        '<div class="hint" id="hint"></div></div>')
    doors_style = ' style="display:none"' if session_email else ''
    parts.append(f'<div id="doors" class="stack"{doors_style}>{"".join(doors)}</div>')
    has_session = "true" if session_email else "false"
    return (
        f"{_AUTH_HEAD.replace('</style>', _LOGIN_CSS + '</style>')}"
        f'<body><div class="wrap"><div class="card" id="card">'
        f'<div class="logo">▚ tools-registry</div><h1>Sign in</h1>'
        f'<p>to connect the <b>treg</b> CLI to your account</p>'
        f'<div class="stack">{"".join(parts)}</div><div class="err" id="err"></div>'
        f"</div></div>"
        f"<script>const HAS_SESSION={has_session};{_LOGIN_JS.replace('__LOGIN_ID__', login_id)}</script></body></html>"
    )


_LOGIN_CSS = (
    ".btn{display:block;width:100%;box-sizing:border-box;padding:11px 14px;border-radius:9px;"
    "border:1px solid var(--line);background:#332d23;color:var(--ink);font-family:var(--mono);"
    "font-size:13.5px;cursor:pointer;text-decoration:none;text-align:center}"
    ".btn:hover{border-color:var(--accent)}"
    ".btn.primary{background:var(--accent);border-color:var(--accent);color:#211d16;font-weight:700}"
    ".stack{display:flex;flex-direction:column;gap:10px;margin-top:18px}"
    ".stack>div{display:flex;flex-direction:column;gap:10px}"
    ".div{display:flex;flex-direction:row!important;align-items:center;gap:10px;color:var(--muted);font-size:12px;margin:6px 0 0}"
    ".div:before,.div:after{content:'';flex:1;border-top:1px solid var(--line)}"
    ".div.acc{cursor:pointer;user-select:none}.div.acc:hover{color:var(--ink)}"
    "#email-row,#code-row{display:flex;flex-direction:column;gap:10px}"
    "input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid var(--line);"
    "background:#1c1913;color:var(--ink);font-family:var(--mono);font-size:13.5px}"
    ".err{color:#d78f6c;font-size:12.5px;margin-top:10px;min-height:1em}"
    ".hint{color:var(--muted);font-size:12px}"
    ".muted{color:var(--muted)}"
    ".team{display:flex;justify-content:space-between;align-items:center;gap:10px;text-align:left}"
    ".team .tn{display:flex;flex-direction:column;gap:2px;min-width:0}"
    ".team .tnm{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".team .tm{font-size:11px;color:var(--muted)}"
    ".team.primary .tm{color:#211d16;opacity:.8}"
    ".pklabel{font-size:12px;color:var(--muted);margin:2px 0 2px}"
    "#paircode-show{font-size:22px;font-weight:700;letter-spacing:8px;text-align:center;color:var(--accent);"
    "padding:10px 12px 10px 20px;border:1px dashed var(--line);border-radius:9px;background:#1c1913}"
)

# The page's whole brain: every door funnels into approve(), which completes the CLI handshake.
# done() builds DOM via textContent (the email came over JSON — never trust it into innerHTML).
_LOGIN_JS = """
const LID='__LOGIN_ID__';
// `treg login` puts the pairing code in the URL FRAGMENT (#code=…) — it never reaches the server on
// the GET. When present, show it read-only for a visual match against the terminal instead of making
// the user type it; approve() still sends it for full server-side validation. No fragment (an old CLI,
// or a link someone stripped it from) → the typed-input fallback below stays.
const PAIR=(()=>{const m=/[#&]code=([A-Za-z0-9-]{1,16})/.exec(location.hash||'');return m?m[1].toUpperCase():''})();
if(PAIR){const box=document.getElementById('pcbox');if(box){box.innerHTML='';
 const l=document.createElement('div');l.className='pklabel';l.textContent='Check this code matches your terminal:';box.appendChild(l);
 const c=document.createElement('div');c.id='paircode-show';c.textContent=PAIR;box.appendChild(c);}}
const pairCode=()=>PAIR||((document.getElementById('paircode')||{}).value||'');
// Signed-in users see the doors collapsed behind the "use a different account" divider.
function toggleDoors(){const d=document.getElementById('doors');if(!d)return;
 const open=d.style.display==='none';d.style.display=open?'':'none';
 const c=document.getElementById('acc-caret');if(c)c.textContent=open?'\\u25be':'\\u25b8'}
const err=m=>{document.getElementById('err').textContent=m||''};
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 let d={};try{d=await r.json()}catch(e){}
 if(!r.ok)throw new Error(d.detail||('error '+r.status));return d}
function done(email){const c=document.getElementById('card');c.innerHTML='';
 const mk=(t,cls,txt)=>{const e=document.createElement(t);if(cls)e.className=cls;if(txt)e.textContent=txt;c.appendChild(e);return e};
 mk('div','logo','\\u259a tools-registry');mk('div','mark','\\u2705');
 mk('h1',null,email?('Logged in as '+email):'Logged in');
 mk('p',null,'Return to your terminal. The CLI is finishing up.');
 // Don't strand the tab: the approve() above set a session cookie, so /app works. Count down to
 // Getting started, but let a click beat the clock — and a second click on "stay" cancel it.
 const row=mk('p','hint','');let left=10;
 const go=document.createElement('a');go.href='/app#start';go.textContent='Open Getting started now \\u2192';
 go.style.color='var(--accent)';row.appendChild(go);
 const cnt=document.createElement('span');row.appendChild(cnt);
 const stay=document.createElement('a');stay.href='#';stay.textContent='stay here';stay.className='muted';
 stay.style.marginLeft='8px';stay.style.textDecoration='underline';row.appendChild(stay);
 const tick=()=>{cnt.textContent=' \\u00b7 auto in '+left+'s \\u00b7 '};tick();
 const timer=setInterval(()=>{left--;if(left<=0){clearInterval(timer);location.href='/app#start'}else tick()},1000);
 stay.onclick=e=>{e.preventDefault();clearInterval(timer);row.textContent='';
  const a=document.createElement('a');a.href='/app#start';a.textContent='Open Getting started \\u2192';
  a.style.color='var(--accent)';row.appendChild(a)}}
async function approve(org){err('');
 const pc=pairCode();
 if(!pc.trim()){err('Enter the code shown in your terminal to continue.');const el=document.getElementById('paircode');if(el)el.focus();return}
 try{const b={login_id:LID,code:pc};if(org)b.org=org;const d=await post('/auth/cli/approve',b);done(d.email)}catch(e){err(e.message)}}
let CREATED_ORG=null;  // remember a just-created team so a retry (e.g. after a wrong code) reuses it, never makes a 2nd
async function createTeam(){err('');
 const pc=pairCode();
 if(!pc.trim()){err('Enter the code shown in your terminal to continue.');const el=document.getElementById('paircode');if(el)el.focus();return}
 const inp=document.getElementById('newteam');const name=(inp&&inp.value||'').trim();if(!name)return err('give your team a name');
 try{if(!CREATED_ORG){const o=await post('/orgs',{name:name});CREATED_ORG=o.org;}await approve(CREATED_ORG);}catch(e){err(e.message)}}
// Render the team picker into #orgpick once a session exists (fetched, so it also runs after the
// email door signs in). One team → a single "Continue as" button; many → a labelled list.
async function loadOrgs(){const box=document.getElementById('orgpick');if(!box)return;
 let d;try{d=await(await fetch('/auth/cli/orgs',{credentials:'include'})).json()}catch(e){return}
 const orgs=d.orgs||[];box.innerHTML='';
 if(!orgs.length){  // brand-new user: no team yet → make them NAME one (never finish the CLI login team-less)
  const l=document.createElement('div');l.className='pklabel';l.textContent='Name your team to finish signing in'+(d.email?(' ('+d.email+')'):'')+':';box.appendChild(l);
  const inp=document.createElement('input');inp.id='newteam';inp.placeholder='Team name, e.g. Superdesign';inp.autocomplete='off';box.appendChild(inp);
  const b=document.createElement('button');b.className='btn primary';b.textContent='Create team \\u2192';b.onclick=createTeam;box.appendChild(b);
  inp.addEventListener('keyup',e=>{if(e.key==='Enter')createTeam()});inp.focus();return}
 if(orgs.length>1){const l=document.createElement('div');l.className='pklabel';l.textContent='Continue as '+d.email+' — pick a team:';box.appendChild(l)}
 orgs.forEach((o,i)=>{const b=document.createElement('button');b.className='btn team'+((i===0&&orgs.length>1)?' primary':'');b.onclick=()=>approve(o.slug);
  const tn=document.createElement('div');tn.className='tn';
  const nm=document.createElement('div');nm.className='tnm';nm.textContent=o.name+(o.personal?' (personal)':'');
  const mt=document.createElement('div');mt.className='tm';mt.textContent=o.role+' · '+o.tool_count+' tool'+(o.tool_count===1?'':'s');
  tn.appendChild(nm);tn.appendChild(mt);b.appendChild(tn);
  if(orgs.length===1){const c=document.createElement('span');c.textContent='→';b.appendChild(c)}
  box.appendChild(b)});
 if(orgs.length===1){box.firstChild.classList.add('primary')}}
async function sendCode(){err('');const em=document.getElementById('em').value.trim();if(!em)return err('enter your email');
 try{const d=await post('/auth/email/start',{email:em});
  document.getElementById('code-row').style.display='';
  document.getElementById('hint').textContent=d.dev_code?('dev code: '+d.dev_code):('code sent to '+d.email);
 }catch(e){err(e.message)}}
async function verifyCode(){err('');const em=document.getElementById('em').value.trim(),co=document.getElementById('code').value.trim();
 if(!co)return err('enter the code');
 try{await post('/auth/email/verify',{email:em,code:co});
  // The email door just set a session cookie — hide the doors and show the team picker.
  const d=document.getElementById('doors');if(d)d.style.display='none';
  const o=document.getElementById('other-acct');if(o)o.style.display='none';
  await loadOrgs();
 }catch(e){err(e.message)}}
if(HAS_SESSION)loadOrgs();
"""

# The app alias preserves the moved handlers' original @app decorators byte-for-byte.
app = APIRouter()
session_router = app


def _intercom_user_hash(email: str) -> str:
    """Intercom identity verification: HMAC-SHA256 of the identifier the dashboard boots the
    Messenger with (the email), keyed by the workspace secret — so a third party who knows an email
    can't impersonate that user in support chat. Empty when unconfigured (self-hosted: no widget)."""
    secret = get_settings().intercom_secret
    if not secret:
        return ""
    return hmac.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()


@app.get("/auth/me")
async def auth_me(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
) -> dict:
    """Who is the caller? Drives the dashboard's identity display in BOTH session mode (cookie) and
    token mode (X-Treg-Token) — the token door otherwise had no way to learn its own email, which
    broke `isPersonal` and join-by-code."""
    try:
        identity = await auth_use_cases.current_identity(x_treg_token, treg_session)
    except auth_use_cases.IdentityLookupError as exc:
        raise HTTPException(status_code=401, detail="no session") from exc
    out = {"email": identity.email, "is_superadmin": identity.is_superadmin, "onboarded": identity.onboarded,
           "github": identity.github}
    if (ich := _intercom_user_hash(identity.email)):
        out["intercom_user_hash"] = ich
    if identity.org_id is not None:
        out |= {"org_id": identity.org_id, "org": identity.org, "role": identity.role}
    return out


@app.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    # A cross-site auto-submitted form could force-logout the victim (the cookie delete is a "simple"
    # request). Bind it to same-origin: reject a request whose Origin isn't treg's own.
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin logout rejected")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sess.COOKIE)
    return resp


# The app alias preserves the moved handlers' original @app decorators byte-for-byte.
app = APIRouter()
invite_router = app


_live_invite_by_email_token = auth_use_cases._live_invite_by_email_token


@app.get("/auth/invite-signin")
async def auth_invite_signin(
    request: Request, code: str = "", t: str = "",
    treg_session: str = Cookie(default=""),
):
    """Landing for an invite email link. Two secrets, two very different trust levels:

    `t` (email_token) exists ONLY in the emailed link — possession proves inbox access, the same bar
    as the emailed OTP — so it may sign the invitee in. But not on this GET: corporate mail scanners
    (Outlook SafeLinks etc.) prefetch GET links and would consume a one-time credential before the
    human ever clicks. So the GET only renders a confirm page whose button POSTs the token back;
    the POST below mints the session.

    `code` (legacy + out-of-band) is also returned to the admin who created the invite, so it can
    NEVER be an authentication factor — holding it lets you JOIN (POST /invites/accept), not log in.
    Links carrying ?code= (emails sent before the split, or relayed by an admin) keep their old
    behavior: validate and bounce to the SPA login with the email prefilled; the invitee proves the
    email through a real door (OTP / GitHub / Google) and the invite auto-appears via /invites/mine.
    An invalid/expired secret of either kind just lands on the site."""
    from urllib.parse import quote
    base = get_settings().public_url.rstrip("/")
    try:
        landing = await auth_use_cases.invite_signin_landing(t, code, treg_session)
    except auth_use_cases.InviteSigninError as exc:
        if exc.kind != "expired":
            raise
        return RedirectResponse("/?invite_expired=1", status_code=303)
    if isinstance(landing, auth_use_cases.InviteEmailConfirmation):
        # Already signed in as someone ELSE? Warn — continuing replaces that browser session.
        switch_note = ""
        if landing.switch_email is not None:
            switch_note = (f"<p>You're currently signed in as <b>{_esc_html(landing.switch_email)}</b> — "
                           f"continuing switches this browser to <b>{_esc_html(landing.email)}</b>.</p>")
        return HTMLResponse(
            f'{_AUTH_HEAD}<body><div class="wrap"><div class="card">'
            f'<div class="logo">▚ tools-registry</div><div class="mark">👋</div>'
            f'<h1>Join {_esc_html(landing.org_name)}</h1>'
            f'<p><b>{_esc_html(landing.invited_by)}</b> invited '
            f'<b>{_esc_html(landing.email)}</b> as {_esc_html(landing.role)}.</p>{switch_note}'
            f'<form method="post" action="/auth/invite-signin" style="margin-top:18px">'
            f'<input type="hidden" name="t" value="{_esc_html(t.strip())}">'
            f'<button type="submit" class="pbtn">'
            f'Continue as {_esc_html(landing.email)} →</button></form>'
            f"</div></div></body></html>"
        )
    # Code path: same redirect whether or not the email already has an account — the code is a
    # convenience that prefills the sign-in email, never an authentication factor. A suspended
    # account is caught at the real login door, the only place the code path can mint a session.
    return RedirectResponse(f"/?invite={quote(landing.email)}", status_code=303)


@app.post("/auth/invite-signin")
async def auth_invite_signin_confirm(request: Request):
    """The confirm page's POST: the emailed one-time token signs the invitee in. Mirrors the OTP
    door (auth_email_verify) — find-or-create the user, refuse the suspended, set the session
    cookie — because the trust source is identical: only the inbox saw this secret. The token is
    consumed here (one-time) so a link floating in a forwarded thread can't be replayed; the invite
    itself stays PENDING — acceptance happens in the dashboard, where a multi-team invitee can
    accept several at once. Body is parsed by hand (urlencoded form) to avoid the python-multipart
    dependency FastAPI's Form() would pull in."""
    from urllib.parse import parse_qs
    try:
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — any junk body = no token
        form = {}
    t = (form.get("t", [""])[0] or "").strip()
    try:
        proof = await auth_use_cases.confirm_invite_signin(t)
    except auth_use_cases.InviteSigninError as exc:
        if exc.kind == "expired":
            return RedirectResponse("/?invite_expired=1", status_code=303)
        if exc.kind == "machine_identity":
            raise HTTPException(status_code=403, detail="this address cannot be used to sign in") from exc
        if exc.kind == "suspended":
            return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
        raise
    resp = RedirectResponse(proof.destination, status_code=303)
    resp.set_cookie(sess.COOKIE, proof.session_cookie, httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


# The app alias preserves the moved handlers' original @app decorators byte-for-byte.
app = APIRouter()
oauth_server_router = app


class OAuthClientRegistration(BaseModel):
    """RFC 7591 registration request. Extra fields are ignored rather than refused — clients send
    plenty we do not use, and rejecting an unknown key would break them for no benefit."""

    client_name: str = ""
    redirect_uris: list[str] = []
    client_uri: str = ""
    logo_uri: str = ""
    scope: str = ""


def _oauth_json_error(exc: auth_use_cases.OAuthServerError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.error, "error_description": exc.description},
    )


@app.post("/oauth/register", include_in_schema=False)
async def oauth_register(body: OAuthClientRegistration) -> JSONResponse:
    """Dynamic client registration (RFC 7591) — how Claude Code and most MCP clients arrive.

    Open by design: the spec has clients register unauthenticated, and a registration grants nothing
    on its own. Every token still requires a human to sign in and approve at the consent screen, so
    the worst a spurious registration achieves is a row in a table.

    What is NOT open is the redirect URI. It is fixed here and matched exactly at authorize time,
    because that is where authorization codes get delivered.
    """
    try:
        registered = await auth_use_cases.register_oauth_client(
            client_name=body.client_name,
            redirect_uris=body.redirect_uris,
            client_uri=body.client_uri,
            logo_uri=body.logo_uri,
            scope=body.scope,
        )
    except auth_use_cases.OAuthServerError as exc:
        return _oauth_json_error(exc)
    return JSONResponse(status_code=201, content=registered)


_resolve_oauth_client = auth_use_cases._resolve_oauth_client
AUTH_CODE_TTL_S = auth_use_cases.AUTH_CODE_TTL_S
_CONSENT_CSS = """
.consent{max-width:460px;text-align:left}
.consent h1{font-size:20px;margin:0 0 4px}
.consent .who{color:var(--ink55,#8a8a8a);font-size:13.5px;margin:0 0 18px}
.consent .grants{list-style:none;padding:0;margin:0 0 18px}
.consent .grants li{padding:7px 0 7px 22px;position:relative;font-size:13.5px;line-height:1.45}
.consent .grants li:before{content:"›";position:absolute;left:6px;color:#e0703f}
.consent label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--ink55,#8a8a8a);margin:0 0 6px}
.consent select{width:100%;padding:9px 10px;border-radius:8px;font:inherit;font-size:14px;
  background:#1a1a1a;color:inherit;border:1px solid #333;margin-bottom:16px}
.consent .row{display:flex;gap:10px}
.consent button{flex:1;padding:10px 14px;border-radius:8px;font:inherit;font-size:14px;cursor:pointer;
  border:1px solid #333;background:#1a1a1a;color:inherit}
.consent button.primary{background:#e0703f;border-color:#e0703f;color:#161310;font-weight:600}
.consent .fine{color:var(--ink55,#8a8a8a);font-size:12px;margin:14px 0 0;line-height:1.5}
"""


def _consent_page(*, client_name: str, client_uri: str, user_email: str, teams: list,
                  hidden: dict, unverified: bool) -> HTMLResponse:
    """The one place a human sees what they are granting — so it says it in words, not scopes.

    Deliberately plain about the two things that cost money or leak data: this client will be able to
    spend the team's balance, and to use the keys the team registered. A consent screen that lists
    `treg:call` and calls it informed is a formality, not a decision.

    The team picker is here rather than anywhere else because this is the only moment a human is
    present to answer it. `balance` used to have to refuse and ask when someone belonged to several
    teams; that question belongs at the grant, once.
    """
    import html as _h

    def _label(t: dict) -> str:
        bal = t.get("balance_usd")
        if bal is None:
            money = ""
        elif bal <= 0:
            # Named, not hidden: an empty team is still a legitimate choice when the work uses the
            # team's OWN keys, which are never metered. Saying "no balance" is the useful warning;
            # removing the option would be wrong.
            money = "  ·  no balance — catalog calls will be refused"
        else:
            money = f"  ·  ${bal:.2f}"
        return f'{t["slug"]} — {t["role"]}{money}'

    opts = "".join(
        f'<option value="{t["org_id"]}">{_h.escape(_label(t))}</option>' for t in teams)
    fields = "".join(
        f'<input type="hidden" name="{_h.escape(k)}" value="{_h.escape(str(v))}"/>'
        for k, v in hidden.items())
    who = _h.escape(client_name or "An application")
    where = (f' <span class="who">({_h.escape(client_uri)})</span>' if client_uri else "")
    warn = ("" if not unverified else
            '<p class="fine"><b>This application registered itself.</b> treg has not reviewed it — '
            'only continue if you recognise it and started this yourself.</p>')
    return HTMLResponse(
        f"{_AUTH_HEAD.replace('</style>', _CONSENT_CSS + '</style>')}"
        f'<body><div class="wrap"><div class="card consent">'
        f'<div class="logo">▚ tools-registry</div>'
        f"<h1>{who} wants to use treg</h1>{where}"
        f'<p class="who">Signed in as {_h.escape(user_email)}</p>'
        f'<ul class="grants">'
        f"<li>Search the catalog and read prices</li>"
        f"<li>Call tools on your team's behalf — <b>this spends the team's balance</b></li>"
        f"<li>Use the API keys and connections your team has registered, without seeing them</li>"
        f"<li>Read the team's balance</li>"
        f"</ul>"
        f'<form method="post" action="/oauth/authorize">{fields}'
        f'<label for="org_id">Which team?</label>'
        f'<select id="org_id" name="org_id" required>{opts}</select>'
        f'<div class="row">'
        f'<button type="submit" name="decision" value="deny">Cancel</button>'
        f'<button type="submit" name="decision" value="allow" class="primary">Allow</button>'
        f"</div></form>{warn}"
        f'<p class="fine">You can revoke this at any time from your treg dashboard.</p>'
        f"</div></div></body></html>")


_wrong_resource = auth_use_cases._wrong_resource
_effective_mcp_resource = auth_use_cases._effective_mcp_resource
_same_mcp_resource = auth_use_cases._same_mcp_resource


def _oauth_error(redirect_uri: str, state: str, error: str, desc: str = ""):
    """OAuth errors go BACK TO THE CLIENT via the redirect, once we trust the redirect.

    Before the client and redirect_uri are validated we must NOT redirect — bouncing an error to an
    unvalidated URI is an open redirect, and it would leak `state` to whoever asked for it. Those
    cases raise a plain 400 instead, which is why this helper is only ever called after validation.
    """
    from urllib.parse import urlencode

    q = {"error": error}
    if desc:
        q["error_description"] = desc
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


_authorize_request = auth_use_cases._authorize_request


def _oauth_server_failure(
    exc: auth_use_cases.OAuthServerError, redirect_uri: str, state: str,
):
    if exc.redirect:
        return _oauth_error(redirect_uri, state, exc.error, exc.description)
    return _oauth_json_error(exc)


@app.get("/oauth/authorize", include_in_schema=False)
async def oauth_authorize(
    request: Request,
    client_id: str = Query(default=""), redirect_uri: str = Query(default=""),
    response_type: str = Query(default="code"), scope: str = Query(default=""),
    state: str = Query(default=""), code_challenge: str = Query(default=""),
    code_challenge_method: str = Query(default=""), resource: str = Query(default=""),
    treg_session: str = Cookie(default=""),
):
    """What is this client asking for, and on behalf of which team?

    Step 3 answers that as JSON; step 4 puts a consent page on top of the same checks. It issues
    NOTHING — approval is a POST, because a GET that granted access could be triggered by any page
    that can make the browser navigate.
    """
    try:
        view = await auth_use_cases.prepare_oauth_authorization(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,
            scope=scope,
            session_cookie=treg_session,
        )
    except auth_use_cases.OAuthServerError as exc:
        return _oauth_server_failure(exc, redirect_uri, state)
    if view is None:
        # Sign in first, then come back to THIS request. Parked in a cookie rather than a `?next=`
        # query the sign-in page would have to understand — the first version invented that
        # convention and nothing implemented it, so the user signed in and landed on the dashboard
        # with the authorization silently dropped.
        # The query is only a UI cue. The validated request stays in the HttpOnly return cookie.
        resp = RedirectResponse("/?signin=oauth", status_code=302)
        _remember_oauth_return(resp, request)
        return resp

    hidden = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": response_type,
              "scope": scope, "state": state, "code_challenge": code_challenge,
              "code_challenge_method": code_challenge_method, "resource": resource}
    if "application/json" in (request.headers.get("accept") or ""):
        return {
            "client": {"client_id": view.client_id, "name": view.client_name,
                       "uri": view.client_uri, "kind": view.client_kind},
            "redirect_uri": redirect_uri, "scope": scope, "resource": resource,
            "user": view.user_email,
            # The team picker. A person may belong to several, and which one this client may spend
            # from is a decision for the human here — not something resolved per call later.
            "teams": view.teams,
            "approve_with": "POST /oauth/authorize with the same parameters plus org_id",
        }
    return _consent_page(client_name=view.client_name, client_uri=view.client_uri,
                         user_email=view.user_email, teams=view.teams, hidden=hidden,
                         unverified=(view.client_kind == "dcr"))


@app.post("/oauth/authorize", include_in_schema=False)
async def oauth_authorize_approve(
    request: Request,
    decision: str = Form(default="allow"),
    client_id: str = Form(default=""), redirect_uri: str = Form(default=""),
    response_type: str = Form(default="code"), scope: str = Form(default=""),
    state: str = Form(default=""), code_challenge: str = Form(default=""),
    code_challenge_method: str = Form(default=""), resource: str = Form(default=""),
    org_id: int = Form(default=0),
    treg_session: str = Cookie(default=""),
):
    """The human decided. On approval, mint a one-time code bound to everything that made this
    request; on anything else, tell the client no."""
    # The consent form is the security boundary, so the submission must have come from OUR page.
    # Without this, a page anywhere could auto-submit a form and grant itself a team's balance —
    # the user is signed in, so their cookie would ride along. Same guard `auth_logout` uses.
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail=(
            "cross-origin authorization rejected — this form must be submitted from treg's own "
            f"consent page (saw Origin: {request.headers.get('origin') or 'none'}, "
            f"Sec-Fetch-Site: {request.headers.get('sec-fetch-site') or 'none'})"))

    try:
        code = await auth_use_cases.approve_oauth_authorization(
            decision=decision,
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,
            org_id=org_id,
            session_cookie=treg_session,
        )
    except auth_use_cases.OAuthServerError as exc:
        return _oauth_server_failure(exc, redirect_uri, state)

    from urllib.parse import urlencode

    q = {"code": code}
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


_refresh_grant = auth_use_cases._refresh_grant


@app.post("/oauth/revoke", include_in_schema=False)
async def oauth_revoke(token: str = Form(default="")) -> JSONResponse:
    """RFC 7009. Revoking a refresh token ends its whole family — a user who disconnects an app
    means all of it, not the one string they happened to send.

    Always answers 200, as the RFC requires: an unknown token is already revoked as far as the caller
    is concerned, and saying otherwise would turn this into an oracle for guessing valid tokens.
    """
    await auth_use_cases.revoke_oauth_token(token)
    return JSONResponse({"ok": True})


@app.post("/oauth/token", include_in_schema=False)
async def oauth_token(
    grant_type: str = Form(default=""), code: str = Form(default=""),
    redirect_uri: str = Form(default=""), client_id: str = Form(default=""),
    code_verifier: str = Form(default=""), resource: str = Form(default=""),
    refresh_token: str = Form(default=""),
):
    """Exchange a code for an access token.

    Errors here are JSON, not redirects: this is a back-channel call from the client itself, and
    there is no browser to send anywhere.
    """
    try:
        token = await auth_use_cases.exchange_oauth_token(
            grant_type=grant_type,
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=code_verifier,
            resource=resource,
            refresh_token=refresh_token,
        )
    except auth_use_cases.OAuthServerError as exc:
        return _oauth_json_error(exc)
    return JSONResponse(token)


@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def oauth_protected_resource():
    """Tells an MCP client which authorization server guards /mcp/ and what it may ask for.

    Two paths for one document: the spec has clients look it up either at the host root or under the
    resource's own path, and which one a given client tries is not something we get to choose.
    """
    return JSONResponse(mcp_oauth.protected_resource_metadata(),
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/.well-known/oauth-protected-resource/mcp/v2", include_in_schema=False)
async def oauth_protected_resource_v2():
    """Protected-resource metadata for the catalog-only connector."""
    if not get_settings().claude_connector_enabled:
        raise HTTPException(status_code=404, detail="Claude catalog connector is not enabled")
    return JSONResponse(mcp_oauth.protected_resource_metadata("v2"),
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def oauth_authorization_server():
    """How to get a token: the authorize and token endpoints, S256, and that we accept both dynamic
    registration and a client-id metadata document."""
    return JSONResponse(mcp_oauth.authorization_server_metadata(),
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/.well-known/openai-apps-challenge", include_in_schema=False)
async def openai_apps_challenge():
    """Domain-verification token for the OpenAI plugin directory.

    The portal issues a token and fetches it here to confirm we control the host serving the MCP
    endpoint. It must return THAT token as plain text and nothing else — the documentation is
    explicit that JSON, a list, or several tokens all fail. 404 when unset, which is correct for
    every deployment that is not ours: an empty file would read as a verification that never
    completes.
    """
    token = (get_settings().openai_apps_challenge or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="not configured")
    return PlainTextResponse(token, headers={"Cache-Control": "no-store"})


# The app alias preserves the moved handlers' original @app decorators byte-for-byte.
app = APIRouter()
grants_router = app


class GrantTeamIn(BaseModel):
    team: str = ""          # the slug (or numeric id) of a team the signed-in user belongs to


@app.get("/oauth/grants", include_in_schema=False)
async def oauth_grants(user: User = Depends(require_identity)) -> list[dict]:
    """The MCP connections this account has granted, and which team each one spends from.

    The team on a grant was chosen once, on a consent screen, and after that it was invisible from
    every side: the client reports a slug, the CLI lists the teams of whichever identity is logged
    in THERE, and the two need not be the same account at all. Somebody spent from a team they could
    not see listed anywhere and had no way to recognise as wrong.
    """
    return await auth_use_cases.list_oauth_grants(user.id)


@app.post("/oauth/grants/{family_id}/team", include_in_schema=False)
async def oauth_grant_set_team(family_id: str, body: GrantTeamIn,
                               user: User = Depends(require_identity)) -> dict:
    """Re-point a live grant at another of the user's teams — without re-doing the OAuth dance.

    The team is stored on the refresh family rather than only inside the issued access token, so
    moving it is a row update and the next refresh picks it up. Re-consenting works too, but it
    means disconnecting a working connector in whatever client holds it, and "the only way to fix
    which balance this spends is to tear the connection down" is not an answer for the person who
    just found out they were spending from the wrong one.

    Only the GRANT'S OWN user may move it, and only to a team THEY are a member of — a grant must
    never become a way to reach a team the consent screen would not have offered.
    """
    try:
        return await auth_use_cases.move_oauth_grant(
            user_id=user.id, family_id=family_id, team=body.team,
        )
    except auth_use_cases.OAuthGrantError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc


# The app alias preserves the moved handlers' original @app decorators byte-for-byte.
app = APIRouter()
token_router = app


@app.get("/auth/cli-token")
async def auth_cli_token(
    user: User = Depends(require_identity),
    x_treg_org: str = Header(default=""),
) -> dict:
    """Mint a fresh CLI/bearer token for the authenticated caller (session cookie OR token). Identity
    tokens are stateless (`sess.make`), so handing one out rotates/invalidates nothing — it just lets
    the dashboard embed a working token in copy-paste snippets + a 'copy token' button, so a human
    doesn't have to hunt for it in `~/.treg/config.json`.

    When the caller names a team (the dashboard sends `X-Treg-Org` for the active org, and only after
    confirming membership), the org slug is BAKED into the token. That is what makes the dashboard's
    "your API key" work as a bare bearer where no `X-Treg-Org` header can travel — pasted into an MCP
    server's Authorization it resolves to that team, no header, no per-org agent token to manage. A
    caller in one team who sends no header still gets a plain token (MCP auto-selects the sole team)."""
    return await auth_use_cases.issue_cli_token(
        user_id=user.id,
        email=user.email,
        token_version=user.token_version,
        org_ref=x_treg_org,
    )


@app.post("/auth/revoke-tokens")
async def auth_revoke_tokens(
    request: Request,
    user: User = Depends(require_identity),
) -> JSONResponse:
    """Kill switch for a leaked token: invalidate every signed identity token (from `treg login`) AND
    every browser session this user holds, in one step. Bumping user.token_version makes all previously
    minted tokens (which carry the old tv) mismatch and be rejected. Unlike suspending the account this
    keeps the user active; unlike rotating TREG_SESSION_SECRET it affects ONLY this user. We then re-issue
    a fresh session cookie + token for the caller, so the device that pressed the button stays signed in
    while every other device is signed out. (Org membership tokens from accept-invite are a separate token
    type and are unaffected — those are revoked by removing the membership.)"""
    revoked = await auth_use_cases.revoke_identity_tokens(user.id)
    resp = JSONResponse({"token": revoked.token, "email": revoked.email, "revoked": True})
    resp.set_cookie(sess.COOKIE, revoked.session_cookie, httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp
