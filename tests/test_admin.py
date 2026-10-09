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
    assert body["balance_micro"] == 50_000_000  # unverified seed starts with no signup credit

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


async def test_manual_grant_uses_configured_database_without_cloud_credentials(c, monkeypatch):
    """The standalone tool still grants once after removing hosted connection helpers."""
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    path = Path(__file__).parents[1] / "scripts/manual_grant.py"
    spec = importlib.util.spec_from_file_location("manual_grant_test", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    _, org, *_ = await _seed(c)
    args = SimpleNamespace(email="a@x.dev", org_id=org["org_id"], amount_usd="1.25",
                           ref="maintenance-test", reason="test", by="test", confirm=False)
    assert await script.run(args) == 0
    args.confirm = True
    assert await script.run(args) == 0
    assert await script.run(args) == 1
    async with session_maker() as db:
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id == org["org_id"], LedgerEntry.kind == "grant",
        ))).scalars().all()
        credits = [entry for entry in entries if entry.meta.get("ref") == "maintenance-test"]
        assert len(credits) == 1
        assert credits[0].amount_micro == 1_250_000


async def test_stats_count_calls_from_the_day_table_not_the_call_table(c):
    """It loaded every call row to count them, which timed out at production size and answered
    502. Windowed call numbers now come from `endpointdaystat`."""
    from datetime import datetime, timedelta, timezone
    from treg.infra.db import session_maker
    from treg.models import EndpointDayStat

    await _seed(c)
    today = datetime.now(timezone.utc).date()
    async with session_maker() as db:
        for days_ago, n, ok in ((1, 100, 90), (10, 50, 40), (45, 1000, 0)):  # the last is outside 30 d
            db.add(EndpointDayStat(endpoint_id="e.x", day=(today - timedelta(days=days_ago)).isoformat(),
                                   n=n, ok=ok))
        await db.commit()
    calls = (await c.get("/admin/stats", headers=_a())).json()["calls"]
    assert calls["last_7d"] == 100 and calls["last_30d"] == 150
    assert calls["success_rate"] == round(130 / 150, 3)


async def test_admin_calls_reads_new_rows_after_a_cursor_by_provider(c):
    """A live poller reads only what is new: `since_id` returns rows after it oldest first, with
    the provider, charge and timing; `provider` alone is refused so the read stays a key range."""
    from treg.models import CallRecord
    async with session_maker() as s:
        for i, (prov, ok) in enumerate((("crawl4ai", 200), ("firecrawl", 200), ("crawl4ai", 502), ("crawl4ai", 200))):
            s.add(CallRecord(org_id=1, user_email="u@example.com", tool_name=f"{prov}.web.scrape", method="POST",
                             path="/call/x", status_code=ok, endpoint_id=f"{prov}.web.scrape", provider=prov,
                             credential_tier="platform", cost_charged_micro=250 * (i + 1), duration_ms=100 + i,
                             upstream_ms=90 + i, call_ref=f"ref{i}:r0",
                             error_response='[502] {"ok": false, "reason": "no-answer:dns-failed"}' if ok == 502 else None))
        await s.commit()
    first = (await c.get("/admin/calls?limit=1", headers=_a())).json()
    assert len(first) == 1 and first[0]["provider"] == "crawl4ai" and first[0]["charged_micro"] == 1000
    cursor = first[0]["id"] - 4
    rows = (await c.get(f"/admin/calls?since_id={cursor}&provider=crawl4ai", headers=_a())).json()
    assert [r["status"] for r in rows] == [200, 502, 200]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert rows[0]["duration_ms"] == 100 and rows[0]["upstream_ms"] == 90 and rows[0]["tier"] == "platform"
    assert [r["error_reason"] for r in rows] == [None, "no-answer:dns-failed", None]
    assert (await c.get("/admin/calls?provider=crawl4ai", headers=_a())).status_code == 422


async def test_admin_share_counts_requests_per_job_and_answers_per_provider(c):
    """A routed call counts once as a request, and its successful attempt credits the provider that
    answered; a direct call credits its own provider; a failed attempt, or a 200 its adapter judged
    empty, credits nobody."""
    from treg.models import CallRecord
    async with session_maker() as s:
        def add(ep, prov, status, ref):
            s.add(CallRecord(org_id=1, user_email="u@example.com", tool_name=ep, method="POST", path="/call/x",
                             status_code=status, endpoint_id=ep, provider=prov, call_ref=ref))
        add("treg.web.extract", "treg", 200, "p1")
        add("crawl4ai.web.scrape", "crawl4ai", 502, "p1:r0")
        add("tinyfish.web.fetch", "tinyfish", 200, "p1:r1")
        add("treg.web.extract", "treg", 200, "p2")
        add("crawl4ai.web.scrape", "crawl4ai", 200, "p2:r0")
        add("crawl4ai.web.scrape", "crawl4ai", 200, "d1")
        add("treg.web.extract", "treg", 200, "p3")
        s.add(CallRecord(org_id=1, user_email="u@example.com", tool_name="tinyfish.web.fetch", method="POST",
                         path="/call/x", status_code=200, endpoint_id="tinyfish.web.fetch", provider="tinyfish",
                         call_ref="p3:r0", hit=False))   # a 200 its adapter judged empty: not an answer
        add("crawl4ai.web.scrape", "crawl4ai", 200, "p3:r1")
        await s.commit()
    body = (await c.get("/admin/share?minutes=60", headers=_a())).json()
    job = next(j for j in body["jobs"] if j["capability"] == "web.extract")
    assert job["requests"] == 4 and job["answered"] == 4
    assert job["by_provider"] == {"crawl4ai": 3, "tinyfish": 1}
