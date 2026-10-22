"""OpenMail on treg's key: one pod per org, every relayed call on that pod's scoped key.

treg's account-wide OpenMail key (`TREG_PLATFORM_KEY_OPENMAIL`) does exactly two things, both
here: create the org's pod (`POST /v1/pods`, `clientId` = the org) and mint that pod's scoped key
(`POST /v1/pods/{id}/api-keys`). It never rides a relayed call. Mail calls carry the pod key, so
OpenMail refuses a cross-team inbox id before treg has to know about it — the tenancy boundary is
upstream, which is why `openmail.yaml` needs no `managed_resource` rows.

A pod key cannot set `podId` itself; `body_allowlist` on every row keeps the caller from passing
one. Provisioning runs once per org on its first platform call and is idempotent: a 409 from
`POST /v1/pods` (the pod exists from an earlier, uncommitted attempt) is resolved by listing pods
and matching the `clientId`.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto
from ...config import get_settings
from ...models import PlatformSubaccount, Secret
from .types import CallFailure

PROVIDER = "openmail"
# What a sub-account binding looks like before `attach_subaccount` fills in its pseudo secret id.
# Same header shape as a pasted OpenMail key (`oauth_providers.OPENMAIL`), so the relay injects it
# through the ordinary `env` injector; the value just comes from `PlatformSubaccount` instead of
# `Secret`. Negative ids keep the two tables from ever colliding in `secrets`.
BINDING = {"platform_subaccount": PROVIDER, "injector": "env", "location": "header",
           "name": "Authorization", "format": "Bearer {secret}"}


def client_id(org) -> str:
    return f"treg-org-{org.id}"


def _unavailable(detail: str) -> CallFailure:
    return CallFailure(
        "platform_subaccount_unavailable", status_code=503,
        detail={"error": "platform_subaccount_unavailable", "provider": PROVIDER,
                "message": f"treg could not set up this team's OpenMail pod ({detail}); "
                           "nothing was charged, retry later or use your own key"},
    )


async def ensure_subaccount(db: AsyncSession, org, client: httpx.AsyncClient) -> PlatformSubaccount:
    """The org's pod row (`org` is the caller's `OrgSnapshot`), provisioning it on first use. Commits the row before returning so a
    pod key is never minted twice for one org across concurrent first calls."""
    row = (await db.execute(select(PlatformSubaccount).where(
        PlatformSubaccount.org_id == org.id, PlatformSubaccount.provider == PROVIDER))).scalars().first()
    if row is not None:
        return row
    settings = get_settings()
    root = settings.platform_key_openmail
    if not root:
        raise _unavailable("no platform key")
    base = "https://api.openmail.sh"
    headers = {"Authorization": f"Bearer {root}", "Content-Type": "application/json"}
    cid = client_id(org)
    try:
        created = await client.post(f"{base}/v1/pods", headers=headers, timeout=15,
                                    json={"clientId": cid, "name": f"treg {org.slug}"})
        if created.status_code == 409:
            listed = await client.get(f"{base}/v1/pods", headers=headers, timeout=15)
            listed.raise_for_status()
            pods = listed.json().get("data", [])
            match = next((p for p in pods if p.get("clientId") == cid), None)
            if match is None:
                raise _unavailable("pod exists but was not found")
            pod_id = str(match["id"])
        else:
            if created.status_code >= 400:
                raise _unavailable(f"pod creation returned {created.status_code}")
            pod_id = str(created.json()["id"])
        minted = await client.post(f"{base}/v1/pods/{pod_id}/api-keys", headers=headers, timeout=15,
                                   json={"name": "treg platform"})
        if minted.status_code >= 400:
            raise _unavailable(f"key minting returned {minted.status_code}")
        key = minted.json().get("token")  # the raw token, shown once by OpenMail
        if not key:
            raise _unavailable("key minting returned no token")
    except httpx.RequestError as exc:
        raise _unavailable(exc.__class__.__name__) from exc
    row = PlatformSubaccount(org_id=org.id, provider=PROVIDER, upstream_id=pod_id,
                             secret=crypto.encrypt(key))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def attach_subaccount(tool, org, db: AsyncSession, client: httpx.AsyncClient,
                            secrets: dict[int, Secret]) -> None:
    """Resolve every `platform_subaccount` binding on the virtual tool: provision if needed, then
    expose the pod key to the relay as a pseudo `Secret` under a negative id. The relay decrypts it
    like any org secret; the row itself never leaves this module."""
    for binding in tool.bindings:
        if binding.get("platform_subaccount") != PROVIDER:
            continue
        row = await ensure_subaccount(db, org, client)
        sid = -int(row.id)
        secrets[sid] = Secret(id=sid, org_id=org.id, name=f"{PROVIDER} pod {row.upstream_id}",
                              kind="platform_subaccount", value=row.secret, provider=PROVIDER)
        binding["secret_id"] = sid
