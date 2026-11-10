"""Narrow WorkOS transport for enterprise sign-in; no treg authorization decisions here."""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlencode, urlsplit

import httpx

BASE_URL = "https://api.workos.com"


class WorkOSError(Exception):
    pass


def authorization_url(*, client_id: str, organization: str, redirect_uri: str, state: str) -> str:
    return BASE_URL + "/sso/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "organization": organization,
        "redirect_uri": redirect_uri, "state": state,
    })


async def _post(client: httpx.AsyncClient, path: str, key: str, payload: dict) -> dict:
    try:
        response = await client.post(
            BASE_URL + path, json=payload,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise WorkOSError("invalid WorkOS response")
        return data
    except (httpx.HTTPError, ValueError) as exc:
        raise WorkOSError("WorkOS request failed") from exc


async def create_organization(client: httpx.AsyncClient, *, key: str, name: str, org_id: int) -> str:
    data = await _post(client, "/organizations", key, {"name": name, "external_id": str(org_id)})
    value = data.get("id")
    if not isinstance(value, str) or not value.startswith("org_"):
        raise WorkOSError("WorkOS organization missing")
    return value


async def get_organization(client: httpx.AsyncClient, *, key: str, organization: str) -> dict:
    if not isinstance(organization, str) or not re.fullmatch(r"org_[A-Za-z0-9_]{1,96}", organization):
        raise WorkOSError("invalid WorkOS organization")
    try:
        response = await client.get(BASE_URL + f"/organizations/{organization}",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WorkOSError("WorkOS organization lookup failed") from exc
    if not isinstance(data, dict) or data.get("id") != organization:
        raise WorkOSError("WorkOS organization mismatch")
    return data


async def _list(client: httpx.AsyncClient, path: str, *, key: str, params: dict,
                max_pages: int = 5) -> list[dict]:
    rows: list[dict] = []
    after: str | None = None
    for _ in range(max_pages):
        query = {**params, "limit": 100}
        if after:
            query["after"] = after
        try:
            response = await client.get(BASE_URL + path, params=query,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkOSError("WorkOS request failed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise WorkOSError("invalid WorkOS response")
        rows.extend(item for item in payload["data"] if isinstance(item, dict))
        next_after = (payload.get("list_metadata") or {}).get("after")
        if not next_after:
            return rows
        if next_after == after:
            raise WorkOSError("WorkOS pagination stalled")
        after = next_after
    raise WorkOSError("WorkOS sign-in lookup exceeds page limit")


async def verified_organizations_for_domain(client: httpx.AsyncClient, *, key: str, domain: str) -> list[dict]:
    """Return routing hints for verified WorkOS domains; sign-in still proves membership."""
    organizations = await _list(client, "/organizations", key=key, params={"domains[]": domain})
    found: list[dict] = []
    for org in organizations:
        domains = org.get("domains") or []
        verified = any(isinstance(item, dict) and item.get("state") == "verified"
            and isinstance(item.get("domain"), str) and item["domain"].lower() == domain
            for item in domains)
        if verified and isinstance(org.get("id"), str):
            found.append(org)
    return found


async def directories_for_organization(client: httpx.AsyncClient, *, key: str,
                                       organization: str) -> list[dict]:
    """Discover connected directories even when activation webhooks were lost."""
    return await _list(client, "/directories", key=key,
                       params={"organization_id": organization})


async def connections_for_organization(client: httpx.AsyncClient, *, key: str,
                                       organization: str) -> list[dict]:
    return await _list(client, "/connections", key=key,
                       params={"organization_id": organization})


async def _delete(client: httpx.AsyncClient, path: str, *, key: str) -> None:
    try:
        response = await client.delete(BASE_URL + path,
            headers={"Authorization": f"Bearer {key}"}, timeout=15)
        if response.status_code != 404:  # A previous attempt may have succeeded before the worker died.
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WorkOSError("WorkOS resource deletion failed") from exc


async def delete_directory(client: httpx.AsyncClient, *, key: str, directory: str) -> None:
    if not re.fullmatch(r"directory_[A-Za-z0-9_]{1,96}", directory):
        raise WorkOSError("invalid WorkOS directory")
    await _delete(client, f"/directories/{directory}", key=key)


async def delete_connection(client: httpx.AsyncClient, *, key: str, connection: str) -> None:
    if not re.fullmatch(r"conn_[A-Za-z0-9_]{1,96}", connection):
        raise WorkOSError("invalid WorkOS connection")
    await _delete(client, f"/connections/{connection}", key=key)


async def directory_users(client: httpx.AsyncClient, *, key: str, directory: str) -> list[dict]:
    """Fetch a complete roster before a caller makes any revocation decision."""
    users: list[dict] = []
    after: str | None = None
    for _ in range(1000):
        params = {"directory": directory, "limit": 100}
        if after:
            params["after"] = after
        try:
            response = await client.get(BASE_URL + "/directory_users", params=params,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkOSError("WorkOS directory request failed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise WorkOSError("invalid WorkOS directory response")
        users.extend(payload["data"])
        next_after = (payload.get("list_metadata") or {}).get("after")
        if not next_after:
            return users
        if next_after == after:
            raise WorkOSError("WorkOS directory pagination stalled")
        after = next_after
    raise WorkOSError("WorkOS directory exceeds reconciliation bound")


async def portal_link(client: httpx.AsyncClient, *, key: str, organization: str, intent: str,
                      return_url: str | None = None) -> str:
    if intent not in {"sso", "dsync"}:
        raise ValueError("invalid portal intent")
    payload = {"organization": organization, "intent": intent}
    if return_url:
        payload["return_url"] = return_url
    data = await _post(client, "/portal/generate_link", key, payload)
    value = data.get("link")
    parsed = urlsplit(value) if isinstance(value, str) else None
    if parsed is None or parsed.scheme != "https" or parsed.netloc != "setup.workos.com" or parsed.path not in ("", "/", "/init"):
        raise WorkOSError("WorkOS portal link missing")
    return value


async def invite_it_contact(client: httpx.AsyncClient, *, key: str, organization: str,
                            email: str, directory_sync: bool) -> None:
    """WorkOS emails its long-lived setup invitation; treg never emails a five-minute portal URL."""
    path = f"/organizations/{organization}/it_contacts"
    listing = await _list(client, path, key=key, params={}, max_pages=10)
    contacts = [item for item in listing if isinstance(item, dict)
                and isinstance(item.get("email"), str) and item["email"].lower() == email]
    if contacts:
        contact_id = contacts[0].get("id")
    else:
        contact = await _post(client, path, key, {"email": email})
        contact_id = contact.get("id")
    if not isinstance(contact_id, str) or not contact_id.startswith("it_contact_"):
        raise WorkOSError("WorkOS IT contact missing")
    intents = ["sso"] + (["directory_sync"] if directory_sync else [])
    try:
        response = await client.post(BASE_URL + f"{path}/{contact_id}/invite",
            json={"intents": intents},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WorkOSError("WorkOS invitation failed") from exc


async def exchange_code(client: httpx.AsyncClient, *, key: str, client_id: str, code: str) -> dict:
    data = await _post(client, "/sso/token", key, {
        "client_id": client_id, "client_secret": key, "code": code,
        "grant_type": "authorization_code",
    })
    profile = data.get("profile")
    if not isinstance(profile, dict):
        raise WorkOSError("WorkOS profile missing")
    return profile


def verify_event(body: bytes, signature: str, secret: str) -> dict:
    """WorkOS-Signature is t=milliseconds,v1=hex(HMAC(secret, t + '.' + body))."""
    if not secret or len(body) > 1024 * 1024:
        raise WorkOSError("invalid WorkOS webhook")
    try:
        fields = dict(item.strip().split("=", 1) for item in signature.split(","))
        timestamp = fields["t"]
        issued = int(timestamp)
        if abs(time.time() * 1000 - issued) > 5 * 60 * 1000:
            raise ValueError("stale")
        expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(fields["v1"], expected):
            raise ValueError("signature")
        event = json.loads(body)
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise ValueError("payload")
        return event
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise WorkOSError("invalid WorkOS webhook") from exc
