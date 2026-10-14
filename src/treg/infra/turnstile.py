"""Cloudflare Turnstile siteverify: turn a browser's widget token into one of four outcomes.

The adapter only classifies; what each outcome permits is decided by the email-login use case.
It never raises: a network failure is an outcome (`unavailable`), not an exception, so a siteverify
outage cannot fail a request. (A browser that cannot load Cloudflare's script is another matter:
it has no token at all, which enforce mode refuses; see captcha.js `!blocked`.)
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from ..config import PUBLIC_HOST_ALIASES, get_settings

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_S = 5.0
MAX_TOKEN_CHARS = 2048

PASS = "pass"                # Cloudflare vouched for this token, for this action
MISSING = "missing"          # no token at all: a script, or a widget that never ran
INVALID = "invalid"          # forged, expired, reused, or solved for another action or host
UNAVAILABLE = "unavailable"  # Cloudflare could not answer; says nothing about the caller

log = logging.getLogger("treg")


def enabled() -> bool:
    s = get_settings()
    return bool(s.turnstile_site_key and s.turnstile_secret)


def _short(value) -> str:
    """A Cloudflare-supplied field, made safe for one log line."""
    return re.sub(r"[^\w.:-]", "?", str(value or "-"))[:64]


def misconfiguration() -> list[str]:
    """What an operator most likely got wrong; logged once at startup, never raised."""
    s = get_settings()
    if bool(s.turnstile_site_key) != bool(s.turnstile_secret):
        return ["only one of TREG_TURNSTILE_SITE_KEY / TREG_TURNSTILE_SECRET is set; the check is off"]
    if not enabled():
        return []
    public = (urlsplit(s.public_url).hostname or "").lower()
    if not hostnames():
        return ["no Turnstile hostnames: set TREG_TURNSTILE_HOSTNAMES or TREG_PUBLIC_URL; every token is invalid"]
    if public and public not in hostnames():
        return [f"the public host {public} is not in TREG_TURNSTILE_HOSTNAMES; its tokens are invalid"]
    return []


def hostnames() -> frozenset[str]:
    """Where a token may have been solved: the configured list, else the public host and, when it
    is one of the known public aliases, all of them (the dashboard is served from each)."""
    s = get_settings()
    listed = {h.strip().lower() for h in s.turnstile_hostnames.split(",") if h.strip()}
    if listed:
        return frozenset(listed)
    public = (urlsplit(s.public_url).hostname or "").lower()
    return frozenset(({public} | set(PUBLIC_HOST_ALIASES)) if public in PUBLIC_HOST_ALIASES else {public} - {""})


async def siteverify(form: dict[str, str]) -> dict:
    """The one network call, separate so tests replace it instead of Cloudflare."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.post(SITEVERIFY_URL, data=form)
        response.raise_for_status()
        return response.json()


CLIENT_REASONS = ("blocked", "pending", "error")


def client_reason(token: str | None) -> str:
    """captcha.js sends `!<reason>` instead of a token when it could not get one: the script was
    blocked, the check still waits for a click, or Cloudflare's widget errored. Never a token."""
    token = (token or "").strip()
    if not token.startswith("!"):
        return ""
    return token[1:] if token[1:] in CLIENT_REASONS else "other"


async def verify(token: str | None, *, action: str, remote_ip: str = "") -> str:
    token = (token or "").strip()
    if not token or token.startswith("!"):
        return MISSING
    if len(token) > MAX_TOKEN_CHARS:
        return INVALID
    form = {"secret": get_settings().turnstile_secret, "response": token}
    if remote_ip and remote_ip != "?":
        form["remoteip"] = remote_ip
    try:
        result = await siteverify(form)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("event=turnstile_unavailable error=%s", type(exc).__name__)
        return UNAVAILABLE
    if result.get("success") is True:
        got_action, got_host = result.get("action"), (result.get("hostname") or "").lower()
        if got_action == action and got_host in hostnames():
            return PASS
        # A real solve rejected by our own rules is a configuration problem, not a bot: say which.
        log.warning("event=turnstile_mismatch action=%s hostname=%s", _short(got_action), _short(got_host))
        return INVALID
    codes = result.get("error-codes") or []
    if "internal-error" in codes:
        return UNAVAILABLE
    if {"invalid-input-secret", "missing-input-secret"} & set(codes):
        # Our secret is wrong (a rotation not yet deployed, a typo): that says nothing about the
        # caller, and treating it as `invalid` would make enforce refuse everyone.
        log.error("event=turnstile_secret_rejected codes=%s", ",".join(_short(c) for c in codes[:4]))
        return UNAVAILABLE
    log.info("event=turnstile_rejected codes=%s", ",".join(_short(c) for c in codes[:4]) or "-")
    return INVALID
