"""Super-admin (cross-tenant) — auth via the env token OR an is_superadmin user; read dashboards;
Phase-2 mutations (grant, suspend, delete). Suspension is enforced at the org-scoped gate."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg.api import app
from treg.config import get_settings
from treg.infra.db import reset_db, session_maker
from treg.models import CreditBlock, LedgerEntry

ADMIN = "ENV-ADMIN-SECRET"


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


def _a() -> dict:
    return {"X-Treg-Token": ADMIN}


@pytest.fixture
async def c(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()
    get_settings.cache_clear()


async def _seed(c: AsyncClient):
    """Two orgs owned by two users; Org One has a tool. Returns (user1, org1, user2, org2) responses."""
    u1 = (await c.post("/users", json={"email": "a@x.dev"})).json()
    o1 = (await c.post("/orgs", headers=_h(u1["token"]), json={"name": "Org One"})).json()
    sid = (await c.post("/secrets", headers=_h(o1["token"]), json={"name": "k", "value": "V"})).json()["id"]
    await c.post("/tools", headers=_h(o1["token"]), json={"name": "echo", "base_url": "http://upstream", "secret_id": sid})
    u2 = (await c.post("/users", json={"email": "b@x.dev"})).json()
    o2 = (await c.post("/orgs", headers=_h(u2["token"]), json={"name": "Org Two"})).json()
    return u1, o1, u2, o2


async def _uid(c, email):
    return next(u["id"] for u in (await c.get("/admin/users", headers=_a())).json() if u["email"] == email)


async def test_env_token_authorizes_and_sees_across_orgs(c):
    await _seed(c)
    r = await c.get("/admin/stats", headers=_a())
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["orgs"] >= 4  # 2 personal + 2 team orgs
    assert "env" in body["tools_by_injector"]  # the echo tool's binding


async def test_non_admin_denied(c):
    u1, *_ = await _seed(c)
    assert (await c.get("/admin/stats", headers=_h(u1["token"]))).status_code == 403
    assert (await c.get("/admin/stats")).status_code == 401


async def test_admin_lists_all_orgs_users_tools(c):
    await _seed(c)
    slugs = {o["slug"] for o in (await c.get("/admin/orgs", headers=_a())).json()}
    assert {"org-one", "org-two"} <= slugs
    assert len({u["email"] for u in (await c.get("/admin/users", headers=_a())).json()}) >= 2
    assert any(t["name"] == "echo" for t in (await c.get("/admin/tools", headers=_a())).json())


async def test_grant_and_revoke_superadmin(c):
    u1, *_ = await _seed(c)
    uid = await _uid(c, "a@x.dev")
    assert (await c.get("/admin/orgs", headers=_h(u1["token"]))).status_code == 403          # before
    assert (await c.post(f"/admin/users/{uid}/superadmin", headers=_a(), json={"value": True})).status_code == 200
    assert (await c.get("/admin/orgs", headers=_h(u1["token"]))).status_code == 200          # now a superadmin user
    await c.post(f"/admin/users/{uid}/superadmin", headers=_a(), json={"value": False})
    assert (await c.get("/admin/orgs", headers=_h(u1["token"]))).status_code == 403          # revoked


async def test_suspend_org_locks_members_out(c):
    _, o1, *_ = await _seed(c)
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 200
    await c.post(f"/admin/orgs/{o1['org_id']}/suspend", headers=_a(), json={"value": True})
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 403
    await c.post(f"/admin/orgs/{o1['org_id']}/suspend", headers=_a(), json={"value": False})
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 200


async def test_suspend_user_locks_out(c):
    _, o1, *_ = await _seed(c)
    uid = await _uid(c, "a@x.dev")
    await c.post(f"/admin/users/{uid}/suspend", headers=_a(), json={"value": True})
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 403
    await c.post(f"/admin/users/{uid}/suspend", headers=_a(), json={"value": False})
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 200


async def test_force_delete_org(c):
    _, o1, *_ = await _seed(c)
    assert (await c.delete(f"/admin/orgs/{o1['org_id']}", headers=_a())).status_code == 200
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 401  # membership gone


async def test_delete_user_cascades_empty_orgs(c):
    u1, o1, *_ = await _seed(c)
    uid = await _uid(c, "a@x.dev")
    r = (await c.delete(f"/admin/users/{uid}", headers=_a())).json()
    assert o1["org_id"] in r["deleted_empty_orgs"]  # a@x.dev solely owned Org One
    assert (await c.get("/tools", headers=_h(o1["token"]))).status_code == 401


# ---- admin credit org: the HTTP equivalent of scripts/manual_grant.py ----

async def test_admin_credit_org_happy_path(c):
    """A superadmin can credit an org, and the money goes through money.grant()."""
    u1, o1, *_ = await _seed(c)
    org_id = o1["org_id"]

    r = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
        "amount_usd": "50",
        "ref": "hs-1234",
        "reason": "goodwill comp for issue",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_id"] == org_id
    assert body["amount_micro"] == 50_000_000
    assert body["amount_usd"] == 50.0
    assert body["ref"] == "hs-1234"
    assert body["block_id"]
    promo = get_settings().promo_grant_micro
    assert body["balance_micro"] == promo + 50_000_000

    async with session_maker() as db:
        block = (await db.execute(
            select(CreditBlock).where(CreditBlock.id == body["block_id"])
        )).scalars().first()
        assert block is not None
        assert block.kind == "promotional"
        assert block.amount_micro == 50_000_000
        assert block.remaining_micro == 50_000_000

        entries = (await db.execute(
            select(LedgerEntry).where(LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant")
        )).scalars().all()
        admin_grant = [e for e in entries if (e.meta or {}).get("ref") == "hs-1234"]
        assert len(admin_grant) == 1
        assert admin_grant[0].amount_micro == 50_000_000
        assert admin_grant[0].meta["reason"] == "goodwill comp for issue"
        assert admin_grant[0].meta["source"] == "admin_credit_org"
        assert admin_grant[0].meta["principal"] == "env-admin"


async def test_admin_credit_org_duplicate_ref_is_409(c):
    """A repeated ref for the same org refuses (409) — no double-crediting."""
    _, o1, *_ = await _seed(c)
    org_id = o1["org_id"]

    first = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
        "amount_usd": "25",
        "ref": "dup-ticket-99",
        "reason": "first grant",
    })
    assert first.status_code == 200, first.text

    second = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
        "amount_usd": "25",
        "ref": "dup-ticket-99",
        "reason": "second attempt with same ref",
    })
    assert second.status_code == 409
    assert "dup-ticket-99" in second.json()["detail"]

    async with session_maker() as db:
        entries = (await db.execute(
            select(LedgerEntry).where(LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant")
        )).scalars().all()
        refs = [(e.meta or {}).get("ref") for e in entries]
        assert refs.count("dup-ticket-99") == 1


async def test_admin_credit_org_non_superadmin_is_403(c):
    """A regular user cannot use the credit endpoint."""
    u1, o1, *_ = await _seed(c)
    r = await c.post(f"/admin/orgs/{o1['org_id']}/credit", headers=_h(u1["token"]), json={
        "amount_usd": "10",
        "ref": "test",
        "reason": "test",
    })
    assert r.status_code == 403


async def test_admin_credit_org_unknown_org_is_404(c):
    """Crediting a non-existent org returns 404."""
    r = await c.post("/admin/orgs/999999/credit", headers=_a(), json={
        "amount_usd": "10",
        "ref": "test",
        "reason": "test",
    })
    assert r.status_code == 404


async def test_admin_credit_org_invalid_amount_is_400(c):
    """Various invalid amounts are rejected."""
    _, o1, *_ = await _seed(c)
    org_id = o1["org_id"]

    for bad in ["-10", "0", "abc", "10.0000001"]:
        r = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
            "amount_usd": bad,
            "ref": f"test-{bad}",
            "reason": "test",
        })
        assert r.status_code == 400, f"expected 400 for {bad!r}, got {r.status_code}: {r.text}"


async def test_admin_credit_org_missing_ref_or_reason_is_400(c):
    """ref and reason are both required."""
    _, o1, *_ = await _seed(c)
    org_id = o1["org_id"]

    r = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
        "amount_usd": "10",
        "ref": "",
        "reason": "has reason",
    })
    assert r.status_code == 400
    assert "ref" in r.json()["detail"]

    r = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
        "amount_usd": "10",
        "ref": "has-ref",
        "reason": "",
    })
    assert r.status_code == 400
    assert "reason" in r.json()["detail"]


async def test_admin_credit_org_uses_promotional_kind(c):
    """The credit is always promotional (burns before purchased, non-refundable)."""
    _, o1, *_ = await _seed(c)
    org_id = o1["org_id"]

    r = await c.post(f"/admin/orgs/{org_id}/credit", headers=_a(), json={
        "amount_usd": "100",
        "ref": "promo-check",
        "reason": "checking kind",
    })
    assert r.status_code == 200
    block_id = r.json()["block_id"]

    async with session_maker() as db:
        block = await db.get(CreditBlock, block_id)
        assert block.kind == "promotional"
