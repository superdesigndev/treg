"""Landing-page sandbox STUDIO — the anonymous, throwaway team a visitor builds in-browser and
keeps using from their terminal for a TTL.

Covers: minting a team without auth; the visitor building their own secret + endpoints with the
sandbox token (real product endpoints); synthetic calls that inject for real but never hit the
network (the safety boundary); per-sandbox caps; skill export; org-scoped resolution; rate limit; GC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import make_upstream

from treg import sandbox
from treg.routers.onboard import SANDBOX_RATE_MAX
from treg.api import app
from treg.infra.db import reset_db, session_maker
from treg.models import Secret, Tool, User


@pytest.fixture
async def anon():
    """A fresh, UNAUTHENTICATED client + clean rate-limit state (the mint endpoint is the anon door).
    reset_db() also clears the DB-backed sandbox throttle (the `ephemeral` table)."""
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    await app.state.http.aclose()


def _h(tok: str) -> dict:
    return {"X-Treg-Token": tok}


STRIPE = sandbox.DEFAULTS[0]  # {"secret":"STRIPE_KEY","value":…,"tool":"stripe","base":…}


async def test_mint_creates_a_starter_team(anon):
    r = await anon.post("/demo/sandbox")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["token"] and s["org_slug"].startswith("sbx-")
    # one live endpoint seeded (stripe); both keys in the vault (posthog is vault-only, for the add-row)
    tools = (await anon.get("/tools", headers=_h(s["token"]))).json()
    assert {t["name"] for t in tools} == {d["tool"] for d in sandbox.DEFAULTS if d.get("tool")}  # stripe
    secrets = (await anon.get("/secrets", headers=_h(s["token"]))).json()
    assert {x["name"] for x in secrets} == {d["secret"] for d in sandbox.DEFAULTS}  # STRIPE_KEY + POSTHOG_KEY


async def test_call_synthesizes_and_injects_without_network(anon):
    """The safety boundary: the call injects for REAL (visible) but never leaves treg."""
    tok = (await anon.post("/demo/sandbox")).json()["token"]
    r = await anon.get(f"/call/{STRIPE['base']}/{STRIPE['example']['path']}", headers=_h(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sandbox"] is True
    assert body["injected"]["headers"]["Authorization"] == f"Bearer {STRIPE['value']}"
    assert body["data"]["object"] == "list"  # brand-shaped dummy body


async def test_visitor_builds_own_secret_and_endpoint(anon):
    tok = (await anon.post("/demo/sandbox")).json()["token"]
    # add my own secret + endpoint via the SAME product endpoints the dashboard uses
    assert (await anon.post("/secrets", json={"name": "MYKEY", "value": "s3cr3t", "kind": "env"},
                            headers=_h(tok))).status_code == 200
    sid = [s for s in (await anon.get("/secrets", headers=_h(tok))).json() if s["name"] == "MYKEY"][0]["id"]
    r = await anon.post("/tools", json={
        "name": "weather", "base_url": "https://api.weather.io",
        "bindings": [{"secret_id": sid, "injector": "env", "location": "header",
                      "name": "X-Api-Key", "format": "{secret}"}]}, headers=_h(tok))
    assert r.status_code == 200, r.text
    call = (await anon.get("/call/https://api.weather.io/today", headers=_h(tok))).json()
    assert call["injected"]["headers"]["X-Api-Key"] == "s3cr3t"


async def test_caps(anon):
    tok = (await anon.post("/demo/sandbox")).json()["token"]  # starts seeded
    n_secrets = len(sandbox.DEFAULTS)                              # every DEFAULT seeds a secret
    n_tools = sum(1 for d in sandbox.DEFAULTS if d.get("tool"))    # only entries with a "tool" seed an endpoint
    # secrets: seeded=n_secrets, add up to MAX, then reject
    for i in range(sandbox.MAX_SECRETS - n_secrets):
        assert (await anon.post("/secrets", json={"name": f"S{i}", "value": "v"}, headers=_h(tok))).status_code == 200
    over_secret = await anon.post(
        "/secrets", json={"name": "OVER", "value": "v"}, headers=_h(tok))
    assert over_secret.status_code == 422
    assert over_secret.json()["detail"] == "the sandbox is limited to 3 secrets — sign up for more"
    # endpoints: seeded=n_tools, add up to MAX, then reject
    for i in range(sandbox.MAX_TOOLS - n_tools):
        assert (await anon.post("/tools", json={"name": f"t{i}", "base_url": f"https://h{i}.io"},
                                headers=_h(tok))).status_code == 200
    over_tool = await anon.post(
        "/tools", json={"name": "over", "base_url": "https://x.io"}, headers=_h(tok))
    assert over_tool.status_code == 422
    assert over_tool.json()["detail"] == "the sandbox is limited to 3 endpoints — sign up for more"


async def test_call_still_org_scoped(anon):
    """Interception doesn't loosen resolution — an unregistered host still 404s."""
    tok = (await anon.post("/demo/sandbox")).json()["token"]
    assert (await anon.get("/call/https://not-registered.com/", headers=_h(tok))).status_code == 404


async def test_export_skill(anon):
    tok = (await anon.post("/demo/sandbox")).json()["token"]
    r = await anon.get("/demo/sandbox/skill", headers=_h(tok))
    assert r.status_code == 200, r.text
    exp = r.json()
    assert exp["manifest"]["tools"] and exp["manifest"]["secrets"]
    assert STRIPE["tool"] in exp["treg_json"]
    assert "SKILL" in exp["skill_md"].upper() or "skill" in exp["skill_md"]
    # a manifest never carries real secret values — only placeholders
    assert STRIPE["value"] not in exp["treg_json"]


async def test_skill_samples(anon):
    r = await anon.get("/skills/samples")
    assert r.status_code == 200, r.text
    names = {s["name"] for s in r.json()}
    assert "posthog-insights" in names and "stripe-billing" in names
    ph = next(s for s in r.json() if s["name"] == "posthog-insights")
    assert ph["key"] == "POSTHOG_KEY" and "SKILL.md" in ph["files"] and "treg.json" in ph["files"]


async def test_skill_install_script(anon):
    r = await anon.get("/skills/posthog-insights/install.sh", params={"token": "sbx_tok_123"})
    assert r.status_code == 200, r.text
    body = r.text
    assert body.startswith("#!/bin/sh")
    assert ".claude/skills" in body and "sbx_tok_123" in body        # writes the folder + bakes the token
    assert "/call/https://app.posthog.com" in body                    # calls through the treg proxy
    assert (await anon.get("/skills/nope/install.sh")).status_code == 404


async def test_export_skill_rejects_non_sandbox(anon):
    tok = (await anon.post("/users", json={"email": "real@x.io"})).json()["token"]  # a normal org
    assert (await anon.get("/demo/sandbox/skill", headers=_h(tok))).status_code == 400


async def test_rate_limited_per_ip(anon):
    for _ in range(SANDBOX_RATE_MAX):
        assert (await anon.post("/demo/sandbox")).status_code == 200
    rejected = await anon.post("/demo/sandbox")
    assert rejected.status_code == 429
    assert rejected.json()["detail"] == "too many demo sandboxes from here — try again later"


async def test_gc_reaps_expired_sandboxes(anon):
    tok = (await anon.post("/demo/sandbox")).json()["token"]
    async with session_maker() as db:
        u = (await db.execute(
            select(User).where(User.email.like(f"visitor-%@{sandbox.SANDBOX_DOMAIN}")))).scalar_one()
        u.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=sandbox.SANDBOX_TTL_MIN + 5)
        await db.commit()
        assert await sandbox.gc(db) == 1
    assert (await anon.get(f"/call/{STRIPE['base']}/{STRIPE['example']['path']}", headers=_h(tok))).status_code in (401, 404)
    async with session_maker() as db:
        assert not (await db.execute(select(Tool))).scalars().all()
        assert not (await db.execute(select(Secret))).scalars().all()


async def test_gc_reaps_a_sandbox_that_made_an_idempotent_call(anon):
    """The production failure of 2026-09-02, end to end. A visitor's call carrying an
    `Idempotency-Key` leaves an `IdempotentCall` row pointing at their membership. The sandbox
    reaper kept its OWN list of org-scoped tables, which never learned about that table, so Postgres
    refused to delete the membership and `gc` raised - from inside `mint_sandbox`, so every visitor
    after the first expired one got a 500 instead of a sandbox.

    SQLite does not enforce foreign keys, so on SQLite this test only sees the orphaned row; on
    Postgres (CI) it sees the raise itself. Both are the same bug."""
    from treg.models import IdempotentCall, Membership, Org

    tok = (await anon.post("/demo/sandbox")).json()["token"]
    r = await anon.get(f"/call/{STRIPE['base']}/{STRIPE['example']['path']}",
                       headers={**_h(tok), "Idempotency-Key": "retry-1"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        assert (await db.execute(select(IdempotentCall))).scalars().all(), (
            "the call did not leave an IdempotentCall row - this test no longer reproduces the bug")
        u = (await db.execute(
            select(User).where(User.email.like(f"visitor-%@{sandbox.SANDBOX_DOMAIN}")))).scalar_one()
        u.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=sandbox.SANDBOX_TTL_MIN + 5)
        await db.commit()
        assert await sandbox.gc(db) == 1
    async with session_maker() as db:
        for model in (IdempotentCall, Membership, Org, User):
            assert not (await db.execute(select(model))).scalars().all(), f"{model.__name__} rows survived gc"
    # and the front door still opens afterwards
    assert (await anon.post("/demo/sandbox")).status_code == 200


async def test_a_failing_reaper_does_not_close_the_front_door(anon, monkeypatch):
    """gc runs inside the mint request. Whatever it raises is a bug to log, never a 500 for the
    visitor - that coupling is what turned one undeletable sandbox into six hours of downtime."""
    from treg.application.onboard import sandbox as onboard_sandbox

    async def boom(db):
        raise RuntimeError("reaper bug")

    monkeypatch.setattr(onboard_sandbox, "gc", boom)
    r = await anon.post("/demo/sandbox")
    assert r.status_code == 200, r.text
    assert r.json()["token"]


async def test_gc_skips_a_sandbox_it_cannot_delete_and_reaps_the_rest(anon, monkeypatch):
    """Reaping is per visitor, not all-or-nothing. Before, one undeletable sandbox rolled back every
    other delete too, and since gc only runs from mint, nothing was ever reaped again."""
    from treg.application.onboard import sandbox as onboard_sandbox
    from treg.models import Org

    slugs = [(await anon.post("/demo/sandbox")).json()["org_slug"] for _ in range(2)]
    poisoned = slugs[0]
    real_cascade = onboard_sandbox.cascade_delete_org

    async def cascade_unless_poisoned(org, db):
        if org.slug == poisoned:
            raise RuntimeError("this one will not delete")
        await real_cascade(org, db)

    monkeypatch.setattr(onboard_sandbox, "cascade_delete_org", cascade_unless_poisoned)
    async with session_maker() as db:
        for u in (await db.execute(
                select(User).where(User.email.like(f"visitor-%@{sandbox.SANDBOX_DOMAIN}")))).scalars().all():
            u.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=sandbox.SANDBOX_TTL_MIN + 5)
        await db.commit()
        assert await sandbox.gc(db) == 1
    async with session_maker() as db:
        assert {o.slug for o in (await db.execute(select(Org))).scalars().all()} == {poisoned}
    # once the poisoned one is deletable again, the next reap takes it
    monkeypatch.setattr(onboard_sandbox, "cascade_delete_org", real_cascade)
    async with session_maker() as db:
        assert await sandbox.gc(db) == 1
    async with session_maker() as db:
        assert not (await db.execute(select(Org))).scalars().all()
