"""Landing-page sandbox studio - a login-free, throwaway team a visitor BUILDS in-browser.

A visitor with no account gets a real, short-lived (user, org) + member token. Holding that token
they use the SAME product endpoints the dashboard does - `POST /secrets`, `POST /tools`, `/call/…`,
`POST /skills` - to register their own secret + up to a few endpoints and call them, then copy CLI
commands that keep working from their terminal for the TTL. Sign-up brings it into a real account.

Two things make this safe even though the token can register any endpoint:
  1. **Calls never touch the network.** `synthesize()` runs the REAL injectors to compute exactly
     what treg *would* send upstream, then returns a labelled dummy response. No SSRF, no open relay,
     no arbitrary-host fetch - but the credential injection shown is 100% real (same `injectors.inject`
     the proxy uses). The `/call` handler routes sandbox orgs here instead of `relay()`.
  2. **Caps + TTL + GC.** A sandbox holds at most `MAX_TOOLS`/`MAX_SECRETS`; the whole footprint is
     reaped after `SANDBOX_TTL_MIN` (see `gc()`).

Sandbox orgs are identified by `is_sandbox()` (a `demo` org whose slug starts `sbx-`), which keeps
them distinct from the onboarding demo teams (also `demo`, but team-named). Self-contained
(models + crypto + injectors) → no import cycle with api.py.
"""
from __future__ import annotations

import json
import re

from . import crypto
from .infra.upstream import injectors
from .models import Org, Secret, Tool
from .sandbox_identity import visitor_name

# ---- the ONE live wire ----------------------------------------------------------------------
# The seeded stripe tool above is special: when TREG_DEMO_STRIPE_KEY is configured, a sandbox call
# to a tool matching this EXACT fingerprint relays to the real Stripe test API — with the key
# injected from env, so no sandbox org ever holds it. Edit anything about the tool (base_url,
# bindings, a lookalike) and it stops matching, silently falling back to synthesize(): tampering
# can't exfiltrate a key that isn't there. The base is pinned to the charges resource, so the only
# reachable surface is list/create test charges (the key is also Stripe-restricted to Charges).
LIVE_HOST = "api.stripe.com"
LIVE_BASE = "https://api.stripe.com/v1/charges"

def is_live_tool(tool: Tool) -> bool:
    """Does this sandbox tool match the seeded live-wire fingerprint exactly?"""
    return tool.host == LIVE_HOST and (tool.base_url or "").rstrip("/") == LIVE_BASE


# Brand-shaped dummy payloads so "what the API received" feels like the real endpoint (keyed by host).
SAMPLE_BODIES = {
    "api.stripe.com": {"object": "list", "url": "/v1/charges", "has_more": False,
                       "data": [{"id": "ch_3P9xE2eZvKY", "object": "charge", "amount": 4200,
                                 "currency": "usd", "status": "succeeded", "paid": True}]},
    "app.posthog.com": {"results": [{"event": "$pageview", "count": 1284},
                                    {"event": "signup", "count": 37}], "next": None},
}

_SANDBOX_SLUG_RE = re.compile(r"^sbx-[0-9a-f]{12}$")  # the exact mint format — see mint(): sbx-{token_hex(6)}


def is_sandbox(org: Org | None) -> bool:
    """True for a landing-page sandbox org (distinct from onboarding demo teams). Match the EXACT mint
    slug format, so a real team a user names 'sbx …' (slug 'sbx-…') isn't misread as a sandbox."""
    return bool(org) and bool(org.demo) and bool(_SANDBOX_SLUG_RE.match(org.slug or ""))


def synthesize(method: str, upstream_url: str, tool: Tool, secrets: dict[int, Secret],
               query: list[tuple[str, str]] | None = None, body: str = "") -> dict:
    """Compute what treg WOULD send upstream (via the real injectors) and return a labelled dummy
    response - never touching the network. This is the sandbox's stand-in for `relay()`."""
    headers: dict[str, str] = {}
    params: list[tuple[str, str]] = list(query or [])
    for b in tool.bindings:
        sec = secrets.get(b.get("secret_id"))
        if sec is None:
            continue
        injectors.inject(headers, params, b, crypto.decrypt(sec.value))
    injected_h = dict(headers)
    injected_q = [(k, v) for (k, v) in params if (k, v) not in (query or [])]
    data = SAMPLE_BODIES.get(tool.host,
                             {"ok": True, "message": "Authenticated by treg - your key was injected in the proxy."})
    return {
        "sandbox": True,
        "note": "Dummy sandbox response - treg did NOT call the real upstream. Sign up to reach live APIs.",
        "request": {"method": method, "url": upstream_url},
        "injected": {  # exactly what the proxy added server-side - the credential you never sent
            "headers": injected_h,
            "query": dict(injected_q),
        },
        "upstream_would_receive": {  # a believable echo of what your endpoint would have seen
            "method": method,
            "url": upstream_url,
            "headers": {"host": tool.host, **injected_h},
            "query": dict(params),
            "body": (json.loads(body) if body.strip().startswith(("{", "[")) else body) if body else None,
        },
        "data": data,
    }




from .application.onboard.sandbox import (
    DEFAULTS,
    MAX_SECRETS,
    MAX_TOOLS,
    SAMPLE_SKILLS,
    SANDBOX_DOMAIN,
    SANDBOX_TTL_MIN,
    _ORG_MODELS,
    _skill_md,
    _skill_secret,
    _skill_treg_json,
    export_skill,
    gc,
    install_script,
    is_sandbox_user,
    mint,
    skill_files,
)
