"""Dub Partners API: the only module that talks to Dub (docs/context/architecture/affiliates.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

API_URL = "https://api.dub.co"
TIMEOUT_S = 10.0

# Dub appends the click to the destination URL as `?dub_id=<id>`. The id is opaque; this allowlist
# is the shape check before it is stored in a cookie or sent back to Dub.
CLICK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A partner code is the key of the partner's Dub link, typed after `--ref`. Dub link keys are
# case-insensitive, and treg's friend-referral codes use this same shape, so one value serves both.
PARTNER_CODE_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,31}")
# Dub requires `customer.country` on a lead it creates without a click: an ISO 3166-1 alpha-2 code.
# Cloudflare marks unknown and Tor traffic with these two.
COUNTRY_RE = re.compile(r"[A-Z]{2}")
_NOT_A_COUNTRY = frozenset({"XX", "T1"})
# Refusals of the request itself (bad body, unknown click), which resending cannot fix. 401/403 are
# left retryable: a wrong or revoked key is fixed in configuration, and the rows must then drain.
_PERMANENT = frozenset({400, 404, 409, 410, 422})


@dataclass(frozen=True)
class Outcome:
    ok: bool
    retry: bool
    status: int
    detail: str = ""


def valid_click_id(value: str) -> str:
    value = (value or "").strip().strip('"')
    return value if CLICK_ID_RE.fullmatch(value) else ""


def valid_partner_code(value: str) -> str:
    value = (value or "").strip().strip('"').lower()
    return value if PARTNER_CODE_RE.fullmatch(value) else ""


def valid_country(value: str) -> str:
    value = (value or "").strip().upper()
    return value if COUNTRY_RE.fullmatch(value) and value not in _NOT_A_COUNTRY else ""


def _outcome(status: int, text: str) -> Outcome:
    if 200 <= status < 300:
        return Outcome(ok=True, retry=False, status=status)
    return Outcome(ok=False, retry=status not in _PERMANENT, status=status, detail=text[:300])


async def track_lead(client: httpx.AsyncClient, *, api_key: str, click_id: str,
                     customer_external_id: str, event_name: str,
                     customer_email: str = "") -> Outcome:
    """`POST /track/lead`: tie a customer to the click they came from.

    Dub deduplicates on (customerExternalId, eventName), so a retry after an ambiguous outcome is
    safe. `customerEmail` is what Dub's risk monitoring compares with the partner's own email.
    """
    body: dict = {"clickId": click_id, "eventName": event_name,
                  "customerExternalId": customer_external_id}
    if customer_email:
        body["customerEmail"] = customer_email
    try:
        resp = await client.post(f"{API_URL}/track/lead", json=body, timeout=TIMEOUT_S,
                                 headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        return Outcome(ok=False, retry=True, status=0, detail=f"{type(exc).__name__}: {exc}"[:300])
    return _outcome(resp.status_code, resp.text)


async def lead_from_code(client: httpx.AsyncClient, *, api_key: str, link_domain: str, code: str,
                         customer_external_id: str, country: str,
                         customer_email: str = "") -> Outcome:
    """Tie a customer to the partner whose link key is `code`, with no click (a CLI or agent signup).

    `GET /links/info` names the partner behind the key; `POST /commissions` with `type: lead` then
    creates the customer under that partner, so later Stripe sales for the customer's external id
    pay the partner. Dub requires `customer.country` on this call.
    """
    if not link_domain:
        return Outcome(ok=False, retry=True, status=0, detail="dub_link_domain is not configured")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        info = await client.get(f"{API_URL}/links/info", params={"domain": link_domain, "key": code},
                                timeout=TIMEOUT_S, headers=headers)
    except httpx.HTTPError as exc:
        return Outcome(ok=False, retry=True, status=0, detail=f"{type(exc).__name__}: {exc}"[:300])
    if info.status_code == 404:
        return Outcome(ok=False, retry=False, status=404, detail=f"no Dub link {link_domain}/{code}")
    if not 200 <= info.status_code < 300:
        return _outcome(info.status_code, info.text)
    try:
        partner_id = (info.json() or {}).get("partnerId") or ""
    except ValueError:
        partner_id = ""
    if not partner_id:
        return Outcome(ok=False, retry=False, status=422, detail=f"Dub link {code} belongs to no partner")
    customer: dict = {"externalId": customer_external_id, "country": country}
    if customer_email:
        customer["email"] = customer_email
    try:
        resp = await client.post(f"{API_URL}/commissions", timeout=TIMEOUT_S, headers=headers,
                                 json={"type": "lead", "partnerId": partner_id, "customer": customer})
    except httpx.HTTPError as exc:
        return Outcome(ok=False, retry=True, status=0, detail=f"{type(exc).__name__}: {exc}"[:300])
    return _outcome(resp.status_code, resp.text)
