"""A machine identity has to be able to read the wallet it is spending.

Reported by Jason: `treg balance` died with "no active org" for an agent token. Two causes stacked —
the CLI could not resolve the org id, and the route was admin-only — so fixing one alone leaves the
symptom. Both are pinned here.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _agent_token(c: AsyncClient) -> tuple[str, int]:
    org_id = (await c.get("/orgs")).json()[0]["org_id"]
    r = await c.post(f"/orgs/{org_id}/agents", json={"name": "probe", "role": "member"})
    assert r.status_code == 200, r.text
    return r.json()["token"], org_id


async def test_a_machine_identity_can_learn_its_own_org(clients: AsyncClient):
    """It cannot call GET /orgs — that is deliberate, `create_org` hangs off the same dependency and
    an agent could otherwise mint an org it owns. But it still has to reach /orgs/{id}/… routes, so
    /auth/me tells it the one org its token could ever mean."""
    token, org_id = await _agent_token(clients)
    h = {"X-Treg-Token": token}

    assert (await clients.get("/orgs", headers=h)).status_code == 403      # still refused, on purpose
    me = await clients.get("/auth/me", headers=h)
    assert me.status_code == 200
    assert me.json()["org_id"] == org_id                                   # …but it can learn this


async def test_a_machine_identity_reads_the_wallet_but_not_the_purchase_history(clients: AsyncClient):
    """Every agent is told to run `treg balance` after a call, and a 402 already hands the caller
    `balance_micro` - refusing the same number here while shipping it in an error was incoherent.
    The wallet is everyone's; what was bought, when, and what is left of each block is not."""
    token, org_id = await _agent_token(clients)
    r = await clients.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Token": token})
    assert r.status_code == 200, r.text
    assert "balance_micro" in r.json()
    assert r.json()["blocks"] == [] and r.json()["entries"]["items"] == []


async def test_a_missing_org_is_indistinguishable_from_one_you_cannot_see(clients: AsyncClient):
    """Jason also suspected an org-id enumeration oracle here. There is none, and this keeps it that
    way: a non-existent org and someone else's org must answer identically, or the difference counts
    ids for an attacker."""
    _, org_id = await _agent_token(clients)
    r = await clients.post("/users", json={"email": "stranger2@elsewhere.dev"})
    other = {"X-Treg-Token": r.json()["token"]}
    missing = await clients.get("/orgs/999999/balance", headers=other)
    existing = await clients.get(f"/orgs/{org_id}/balance", headers=other)
    assert missing.status_code == existing.status_code == 403
    assert missing.json() == existing.json()


# ---- what a BAD token is told, and what stays public --------------------------------------------
def test_an_invalid_token_is_named_as_such_not_as_a_missing_org(monkeypatch, capsys):
    """`_active_org_id` returns None both when the token is bad and when there is genuinely no org.
    21 commands turned that into a bare "no active org", sending the reader to fix org config when
    the real problem was authentication."""
    from treg import cli

    class _Resp:
        status_code = 401
        def json(self): return {"detail": "invalid token"}

    class _C:
        def get(self, path, **kw): return _Resp()

    with pytest.raises(SystemExit) as exc:
        cli._active_org_id({"base_url": "http://x", "token": "bad"}, _C())
    assert "invalid/expired" in str(exc.value) and "treg login" in str(exc.value)


def test_the_public_catalog_still_renders_when_signed_out(monkeypatch):
    """`catalog get` only ENRICHES its table with the team's pin, so a signed-out reader must still
    get the page. The auth exit is a SystemExit, which `except Exception` does NOT catch — a
    try/except at the call site would not have saved this, which is why the caller passes
    strict=False instead."""
    from treg import cli

    class _Resp:
        status_code = 401
        def json(self): return {"detail": "invalid token"}

    class _C:
        def get(self, path, **kw): return _Resp()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(cli, "_client", lambda cfg: _C())
    # must return None quietly, NOT exit the process
    assert cli._pinned_provider({"base_url": "http://x"}, "tiktok.user.profile") is None
    assert cli._active_org_id({"base_url": "http://x"}, _C(), strict=False) is None
