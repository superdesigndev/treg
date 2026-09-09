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
    monkeypatch.setattr(get_settings(), "hub_enabled", True)


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
    r1 = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=h)
    r2 = await matrix_clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r2.json()["run_id"] == r1.json()["run_id"]
    assert len(fake_provider.hits) == 1
