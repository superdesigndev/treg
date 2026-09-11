"""Arena's paid execution, attributed results, and repeat-safe feedback boundaries."""
import asyncio
import json
from datetime import timedelta

import pytest
from sqlmodel import select

from treg import crypto
from treg.application import arena
from treg.application.call import service
from treg.domain import arena as rules
from treg.infra.db import session_maker
from treg.models import ArenaEvaluation, ArenaRun, Hold, LedgerEntry
from treg.timeutil import utcnow_naive
from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import enrichment_on, _relay_by_provider  # noqa: F401

IDENTITY = {"full_name": "Test Person", "domain": "example.com"}
SECOND_IDENTITY = {"full_name": "Second Person", "domain": "second.example"}
HUNTER_HIT = {"data": {"email": "test@example.com", "score": 90, "verification": {"status": "valid"}}}
TOMBA_HIT = {"data": {"email": "another@example.com", "score": 80, "verification": {"status": "accept_all"}}}


@pytest.fixture(autouse=True)
async def drain_arena():
    yield
    await arena.shutdown()


async def plan(c, mode="compare", providers=None, **extra):
    r = await c.post("/arena/plans", json={"capability": "people.email.find", "identity": IDENTITY,
        "mode": mode, "providers": providers or ["hunter", "tomba"], "max_cost_micro": 1_000_000, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def finish(c, quote):
    r = await c.post(f"/arena/runs/{quote['id']}/start")
    assert r.status_code == 200, r.text
    task = arena._owners.get(quote["id"])
    if task:
        await asyncio.wait_for(asyncio.shield(task), 15)
    r = await c.get(f"/arena/runs/{quote['id']}")
    assert r.status_code == 200, r.text
    return r.json()


async def batch_plan(c, mode="compare", **extra):
    return await plan(c, mode=mode, identity=None, identities=[IDENTITY, SECOND_IDENTITY], **extra)


async def test_batch_battle_preserves_each_request_and_bills_once(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({
        "hunter": [(200, HUNTER_HIT)] * 2, "tomba": [(200, TOMBA_HIT)] * 2}, seen))
    single = await plan(clients)
    before = await _balance(clients)
    q = await batch_plan(clients)
    assert q["required_micro"] == single["required_micro"] * 2
    assert q["entry_count"] == 2 and len(q["providers"]) == 2
    assert q["estimate_micro"] == sum(p["estimate_micro"] for p in q["providers"])
    assert await _balance(clients) == before and not seen
    result = await finish(clients, q)
    assert result["identities"] == [IDENTITY, SECOND_IDENTITY]
    assert len(seen) == 4 and len(result["results"]) == 4
    for provider in ("hunter", "tomba"):
        queries = [s[2] for s in seen if s[0] == provider]
        assert {q["domain"] for q in queries} == {"example.com", "second.example"}
        assert len([r for r in result["results"] if r["provider"] == provider]) == 2
    assert result["charged_micro"] == before - await _balance(clients)
    await finish(clients, q)
    assert len(seen) == 4
    history = (await clients.get("/arena/runs")).json()
    assert history[0]["entry_count"] == 2
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()
        row = await db.get(ArenaRun, q["id"])
        assert "second.example" not in row.payload


async def test_batch_waterfall_stops_per_entry_and_manual_uses_correct_identity(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({
        "tomba": [(200, TOMBA_HIT), (200, {"data": {"email": None}})],
        "hunter": [(200, HUNTER_HIT), (200, HUNTER_HIT)]}, seen))
    result = await finish(clients, await batch_plan(clients, "waterfall"))
    first = [r for r in result["results"] if r["entry_index"] == 0]
    second = [r for r in result["results"] if r["entry_index"] == 1]
    assert [r["state"] for r in first] == ["hit", "not_attempted"]
    assert [r["state"] for r in second] == ["miss", "hit"]
    assert [s[0] for s in seen] == ["tomba", "tomba", "hunter"]
    assert seen[-1][2]["domain"] == "second.example"
    root = "/arena/runs/" + result["id"]
    rating = root + "/attempts/" + second[1]["id"] + "/rating"
    assert (await clients.post(rating, json={"value": "down"})).status_code == 200
    saved = (await clients.get(root)).json()
    assert sum(bool(r["rating"]) for r in saved["results"]) == 1
    path = root + "/attempts/" + first[1]["id"]
    q = (await clients.post(path + "/plan")).json()
    assert (await clients.post(path + "/start", json={"quote_id": q["id"]})).status_code == 200
    task = arena._owners.get(result["id"])
    if task:
        await task
    assert seen[-1][2]["domain"] == "example.com"
    assert len(seen) == 4
    assert (await clients.post(path + "/plan")).status_code == 409


async def test_batch_waterfall_admits_first_step_for_every_entry(clients, enrichment_on, monkeypatch):
    single = await plan(clients, mode="waterfall")
    q = await batch_plan(clients, "waterfall")
    assert q["required_micro"] == single["required_micro"] * 2
    async def one_entry_balance(*args):
        return single["required_micro"]
    monkeypatch.setattr(arena.money, "balance_of", one_entry_balance)
    q = await batch_plan(clients, "waterfall")
    assert not q["affordable"]
    assert (await clients.post(f"/arena/runs/{q['id']}/start")).status_code == 402
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


@pytest.mark.parametrize("entries", [[], [IDENTITY] * 51, [IDENTITY, IDENTITY],
    [IDENTITY, {"full_name": "Incomplete", "domain": "example.com"}],
    [IDENTITY, {"linkedin_url": "https://www.linkedin.com/in/example"}]])
async def test_batch_invalid_entries_never_create_a_quote(clients, entries):
    response = await clients.post("/arena/plans", json={"capability": "people.email.find", "identities": entries})
    assert response.status_code == 422
    async with session_maker() as db:
        assert not (await db.execute(select(ArenaRun))).scalars().all()


async def test_batch_aggregate_budget_is_enforced(clients, enrichment_on):
    single = await plan(clients)
    response = await clients.post("/arena/plans", json={"capability": "people.email.find",
        "identities": [IDENTITY, SECOND_IDENTITY], "providers": ["hunter", "tomba"],
        "mode": "compare", "max_cost_micro": single["required_micro"]})
    assert response.status_code == 422


async def test_batch_raw_snapshot_limit_keeps_normalized_answers(clients, enrichment_on, monkeypatch):
    monkeypatch.setattr(rules, "MAX_BATCH_RAW_BYTES", 50)
    monkeypatch.setattr(service, "relay", _relay_by_provider({"hunter": [(200, HUNTER_HIT)] * 2}, []))
    result = await finish(clients, await batch_plan(clients, providers=["hunter"]))
    assert all(r["state"] == "hit" and r["output"]["email"] for r in result["results"])
    assert all(r["raw"] is None and r["raw_omitted"] for r in result["results"])


async def test_batch_cancel_bounds_concurrency_and_releases_every_hold(clients, enrichment_on, monkeypatch):
    entered = asyncio.Event()
    seen = []
    async def blocked(*args, **kwargs):
        seen.append(True)
        if len(seen) == 4:
            entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "relay", blocked)
    before = await _balance(clients)
    identities = [{"full_name": "Test Person", "domain": f"company{i}.example"} for i in range(8)]
    q = await plan(clients, providers=["hunter"], identity=None, identities=identities)
    path = f"/arena/runs/{q['id']}"
    await asyncio.gather(clients.post(path + "/start"), clients.post(path + "/start"))
    owner = arena._owners[q["id"]]
    await asyncio.wait_for(entered.wait(), 5)
    assert len(seen) == 4
    await clients.post(path + "/cancel")
    await asyncio.wait_for(asyncio.shield(owner), 5)
    result = (await clients.get(path)).json()
    assert result["state"] == "cancelled"
    assert sum(r["state"] == "not_attempted" for r in result["results"]) == 4
    assert len(seen) == 4 and await _balance(clients) == before
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


async def test_batch_own_keys_remain_free_at_maximum_list_size(clients, enrichment_on, monkeypatch):
    await clients.post('/secrets', json={'name': 'hunter', 'value': 'OWN-KEY'})
    identities = [{"full_name": "Test Person", "domain": f"company{i}.example"} for i in range(50)]
    q = await plan(clients, providers=["hunter"], identity=None, identities=identities)
    assert q["entry_count"] == 50 and q["required_micro"] == 0
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"hunter": [(200, HUNTER_HIT)] * 50}, seen))
    before = await _balance(clients)
    result = await finish(clients, q)
    assert result["charged_micro"] == 0 and len(seen) == 50
    assert {r["entry_index"] for r in result["results"] if r["state"] == "hit"} == set(range(50))
    assert {request[2]["domain"] for request in seen} == {entry["domain"] for entry in identities}
    assert await _balance(clients) == before


async def test_public_page_and_tasks_but_no_anonymous_spending(clients):
    token = clients.headers.pop("X-Treg-Token")
    assert (await clients.get("/enrich-arena")).status_code == 200
    leaderboard = await clients.get("/enrich-arena/leaderboard")
    assert leaderboard.status_code == 200
    assert 'aria-label="Arena pages"' in leaderboard.text
    benchmark = await clients.get("/enrich-arena/people-search-bench")
    assert benchmark.status_code == 200
    assert 'href="/enrich-arena/people-search-bench"' in benchmark.text
    assert (await clients.get("/enrich-arena/bench.js")).status_code == 200
    tasks = (await clients.get("/arena/tasks")).json()
    assert len(tasks) == 10
    work = next(t for t in tasks if t["id"] == "people.email.find")
    names, linkedin = work["provider_previews"]
    assert "hunter" in {p["provider"] for p in names}
    assert "hunter" not in {p["provider"] for p in linkedin}
    assert "fiber-ai" in {p["provider"] for p in linkedin}
    assert all(isinstance(p["estimate_micro"], int) and p["estimate_micro"] >= 0 for p in names)
    assert all(p["price_type"] == "per_success" for p in names)
    async with session_maker() as db:
        assert not (await db.execute(select(ArenaRun))).scalars().all()
        assert not (await db.execute(select(Hold))).scalars().all()
    assert (await clients.get("/enrich-arena/arena.js")).status_code == 200
    assert (await clients.get("/enrich-arena/insights.json")).status_code == 404
    snapshot = await clients.get("/arena/insights")
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json()["version"] == 2
    assert snapshot.json()["rows"] == []
    assert (await clients.get("/enrich-arena/original-records.private.jsonl.gz")).status_code == 404
    assert (await clients.get("/enrich-arena/../../models.py")).status_code == 404
    assert (await clients.post("/arena/plans", json={"capability":"people.email.find", "identity":IDENTITY})).status_code == 401
    for path in ("/arena/runs", "/arena/runs/anything"):
        assert (await clients.get(path)).status_code == 401
    clients.headers["X-Treg-Token"] = token


async def test_compare_shows_vendors_and_costs_before_one_click_vote(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"hunter": [(200,HUNTER_HIT)], "tomba": [(200,TOMBA_HIT)]}, seen))
    before = await _balance(clients)
    quote = await plan(clients)
    assert quote["estimate_micro"] > 0
    assert await _balance(clients) == before
    result = await finish(clients, quote)
    assert result["state"] == "completed" and len(seen) == 2
    assert result["revealed"] is True and result["vote"] is None
    assert {r["label"] for r in result["results"]} == {"A", "B"}
    assert all(r["state"] == "hit" for r in result["results"])
    assert {r["provider"] for r in result["results"]} == {"hunter", "tomba"}
    assert result["charged_micro"] == before - await _balance(clients)
    assert all(r["duration_ms"] >= 0 and r["raw"] for r in result["results"])
    async with session_maker() as db:
        assert not (await db.execute(select(ArenaEvaluation))).scalars().all()
    chosen = result["results"][0]["id"]
    path = f"/arena/runs/{quote['id']}"
    assert (await clients.post(path+"/evaluations",json={"kind":"winner","selected":[chosen]})).status_code == 200
    revealed = (await clients.get(path)).json()
    assert revealed["revealed"] and revealed["vote"]["selected"] == [chosen]
    assert revealed["charged_micro"] == before - await _balance(clients)
    assert revealed["charged_micro"] > 0
    assert all(r["duration_ms"] >= 0 for r in revealed["results"])
    assert {r["provider"] for r in revealed["results"]} == {"hunter", "tomba"}
    # A duplicate start, reveal, and changed vote are all inert once the run/vote is claimed.
    await clients.post(path+"/start")
    await clients.post(path+"/reveal")
    await clients.post(path+"/evaluations",json={"kind":"none"})
    assert (await clients.get(path)).json()["vote"]["selected"] == [chosen]
    assert len(seen) == 2
    async with session_maker() as db:
        assert len((await db.execute(select(ArenaEvaluation))).scalars().all()) == 1
        evaluation = (await db.execute(select(ArenaEvaluation))).scalar_one()
        feedback = arena._unpack(evaluation.payload)
        assert feedback["feedback_context"] == "attributed" and feedback["version"] == "2"
        stored = await db.get(ArenaRun, quote["id"])
        plaintext = crypto.decrypt(stored.payload)
        assert stored.payload != plaintext
        assert json.loads(plaintext)["identity"] == IDENTITY


async def test_waterfall_exposes_steps_and_stops_on_first_hit(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"tomba": [(200,TOMBA_HIT)]}, seen))
    result = await finish(clients, await plan(clients, mode="waterfall"))
    assert len(seen) == 1
    assert [r["state"] for r in result["results"]] == ["hit", "not_attempted"]
    assert result["results"][0]["provider"] == "tomba"
    assert "first result" in result["stop_reason"]


async def test_waterfall_miss_then_hit_records_both_costs(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"tomba": [(200,{"data":{"email":None}})],
        "hunter": [(200,HUNTER_HIT)]}, seen))
    before = await _balance(clients)
    result = await finish(clients, await plan(clients, mode="waterfall"))
    assert [r["state"] for r in result["results"]] == ["miss","hit"]
    assert result["charged_micro"] == before - await _balance(clients)
    assert result["results"][0]["charged_micro"] == 0


async def test_compare_budget_rejects_aggregate_not_each_leg(clients,enrichment_on):
    q = await plan(clients)
    cap = max(p["estimate_micro"] for p in q["providers"])
    r = await clients.post("/arena/plans",json={"capability":"people.email.find","identity":IDENTITY,
        "providers":["hunter","tomba"],"max_cost_micro":cap})
    assert r.status_code == 422
    assert "select fewer" in r.text


async def test_own_key_priority_is_not_platform_metered(clients,enrichment_on,monkeypatch):
    await clients.post("/secrets",json={"name":"hunter","value":"OWN-KEY"})
    seen=[]
    monkeypatch.setattr(service,"relay",_relay_by_provider({"hunter":[(200,HUNTER_HIT)]},seen))
    before=await _balance(clients)
    q=await plan(clients,providers=["hunter"])
    assert q["estimate_micro"]==0 and q["providers"][0]["tier"]=="credential"
    result=await finish(clients,q)
    await clients.post(f"/arena/runs/{q['id']}/reveal")
    result=(await clients.get(f"/arena/runs/{q['id']}")).json()
    assert result["charged_micro"]==0 and await _balance(clients)==before


async def test_no_partial_or_foreign_vote_and_no_cross_origin_start(clients,enrichment_on,monkeypatch):
    gate=asyncio.Event()
    original=_relay_by_provider({"hunter":[(200,HUNTER_HIT)]},[])
    async def blocked(*args,**kwargs):
        await gate.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(service,"relay",blocked)
    q=await plan(clients,providers=["hunter"])
    path=f"/arena/runs/{q['id']}"
    assert (await clients.post(path+"/start",headers={"Origin":"https://evil.example"})).status_code==403
    await clients.post(path+"/start")
    progress = (await clients.get(path)).json()
    assert progress["vote"] is None
    assert progress["results"][0]["provider"] == "hunter"
    assert progress["results"][0]["state"] in {"queued", "running"}
    assert (await clients.post(path+"/evaluations",json={"kind":"none"})).status_code==409
    gate.set()
    task=arena._owners.get(q["id"])
    if task: await task
    assert (await clients.post(path+"/evaluations",json={"kind":"winner","selected":["invented"]})).status_code==422
    token=clients.headers["X-Treg-Token"]
    other=(await clients.post('/users',json={"email":"other@superdesign.dev"})).json()["token"]
    clients.headers['X-Treg-Token']=other
    assert (await clients.get(path)).status_code==404
    assert (await clients.post(path+"/reveal")).status_code==404
    clients.headers['X-Treg-Token']=token


async def test_concurrent_start_dispatches_only_once(clients,enrichment_on,monkeypatch):
    seen=[]
    monkeypatch.setattr(service,"relay",_relay_by_provider({"hunter":[(200,HUNTER_HIT)]},seen))
    q=await plan(clients,providers=["hunter"])
    path=f"/arena/runs/{q['id']}"
    responses=await asyncio.gather(clients.post(path+"/start"),clients.post(path+"/start"))
    assert all(r.status_code==200 for r in responses)
    task=arena._owners.get(q['id'])
    if task: await task
    assert len(seen)==1


async def test_stale_owner_never_redispatches_and_keeps_charge_unknown(clients,enrichment_on):
    q=await plan(clients,providers=["hunter"])
    async with session_maker() as db:
        row=await db.get(ArenaRun,q['id'])
        payload=arena._unpack(row.payload)
        payload['attempts'][0].update(state='running',charged_micro=None)
        row.state='running';row.deadline_at=utcnow_naive()-timedelta(seconds=1);row.payload=arena._pack(payload)
        db.add(row);await db.commit()
    r=(await clients.get(f"/arena/runs/{q['id']}")).json()
    assert r['state']=='interrupted'
    await clients.post(f"/arena/runs/{q['id']}/reveal")
    r=(await clients.get(f"/arena/runs/{q['id']}")).json()
    assert r['charge_pending'] is True
    assert q['id'] not in arena._owners


@pytest.mark.parametrize('kind,selected', [('tie',['a','b']),('none',[]),('cannot_judge',[]),('skip',[])])
def test_non_winner_feedback(kind,selected):
    rules.validate_vote(kind,selected,[],[{'id':'a','state':'hit'},{'id':'b','state':'hit'}])


def test_false_mailbox_verdict_is_not_a_miss():
    from treg.domain.catalog import store
    cat=store.load();ep=cat.by_id['hunter.people.email.verify']
    outcome,out=rules.classify(cat.contracts['people.email.verify'],cat.adapters[ep['id']],ep,200,
        {'data':{'status':'invalid','score':0}})
    assert outcome=='hit' and out['valid'] is False


async def test_oauth_return_only_allows_arena(clients,monkeypatch):
    from types import SimpleNamespace
    from starlette.requests import Request
    from treg.routers.auth import _finish_oauth_login
    u=SimpleNamespace(id=1,token_version=0)
    for value,expected in [
        ('/enrich-arena', '/enrich-arena'),
        ('/enrich-arena?run=saved&team=my-team', '/enrich-arena?run=saved&team=my-team'),
        ('/enrich-arena?capability=people.phone.find&variant=0', '/enrich-arena?capability=people.phone.find&variant=0'),
        ('/enrich-arena/leaderboard', '/enrich-arena/leaderboard'),
        ('/enrich-arena/people-search-bench', '/enrich-arena/people-search-bench'),
        ('https://evil.example', '/app'), ('//evil.example', '/app'),
        ('/enrich-arena/../app', '/app'), ('/enrich-arena?redirect=https://evil.example', '/app'),
        ('/enrich-arena?team=a&team=b', '/app'), ('/enrich-arena#evil', '/app'),
        ('/enrich-arena\r\nSet-Cookie: injected=1', '/app'),
    ]:
        req=Request({'type':'http','scheme':'http','server':('registry',80),'path':'/auth/google/callback',
                     'headers':[(b'cookie',('treg_arena_return='+crypto.encrypt(value)).encode())]})
        response=_finish_oauth_login(req,u,None)
        assert response.headers['location']==expected
        from treg.routers.auth import _arena_return_target
        assert (_arena_return_target(value) or '/app') == expected
        assert _finish_oauth_login(req,u,('cli-id',)).headers['location']=='/login?cli=cli-id'
    for invalid_cookie in ('', '/enrich-arena', crypto.encrypt('/enrich-arena')[:-4] + 'xxxx'):
        req = Request({'type': 'http', 'scheme': 'http', 'server': ('registry', 80),
                       'path': '/auth/google/callback',
                       'headers': [(b'cookie', ('treg_arena_return=' + invalid_cookie).encode())]})
        assert _finish_oauth_login(req, u, None).headers['location'] == '/app'


async def test_cancel_releases_reserved_credit_once(clients, enrichment_on, monkeypatch):
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "relay", blocked)
    before = await _balance(clients)
    q = await plan(clients, providers=["hunter"])
    path = f"/arena/runs/{q['id']}"
    await clients.post(path + "/start")
    owner = arena._owners[q['id']]
    await asyncio.wait_for(entered.wait(), 5)
    async with session_maker() as db:
        assert len((await db.execute(select(Hold))).scalars().all()) == 1
    await clients.post(path + "/cancel")
    await asyncio.wait_for(asyncio.shield(owner), 5)
    await clients.post(path + "/cancel")
    assert (await clients.get(path)).json()['state'] == 'cancelled'
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


async def test_quote_expiration_does_not_dispatch(clients, enrichment_on, monkeypatch):
    q = await plan(clients)
    async with session_maker() as db:
        row = await db.get(ArenaRun, q['id'])
        row.deadline_at = utcnow_naive() - timedelta(seconds=1)
        db.add(row)
        await db.commit()
    r = await clients.post(f"/arena/runs/{q['id']}/start")
    assert r.status_code == 409 and 'expired' in r.text
    assert q['id'] not in arena._owners


async def test_vote_racing_reveal_keeps_one_immutable_evaluation(clients, enrichment_on, monkeypatch):
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'hunter': [(200, HUNTER_HIT)]}, []))
    q = await plan(clients, providers=['hunter'])
    r = await finish(clients, q)
    path = f"/arena/runs/{q['id']}"
    responses = await asyncio.gather(
        clients.post(path + '/evaluations', json={'kind': 'winner', 'selected': [r['results'][0]['id']]}),
        clients.post(path + '/reveal'))
    assert all(r.status_code == 200 for r in responses)
    async with session_maker() as db:
        assert len((await db.execute(select(ArenaEvaluation))).scalars().all()) == 1


async def test_interrupted_receipt_recovers_durable_settlement(clients, enrichment_on, monkeypatch):
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'hunter': [(200, HUNTER_HIT)]}, []))
    q = await plan(clients, providers=['hunter'])
    await finish(clients, q)
    async with session_maker() as db:
        row = await db.get(ArenaRun, q['id'])
        payload = arena._unpack(row.payload)
        attempt = payload['attempts'][0]
        expected = attempt['charged_micro']
        assert expected > 0
        assert (await db.execute(select(LedgerEntry).where(LedgerEntry.call_id == attempt['call_ref'],
            LedgerEntry.kind == 'settle'))).scalar_one()
        attempt['charged_micro'] = None
        row.state = 'interrupted'
        row.payload = arena._pack(payload)
        db.add(row)
        await db.commit()
    path = f"/arena/runs/{q['id']}"
    await clients.post(path + '/reveal')
    result = (await clients.get(path)).json()
    assert result['charged_micro'] == expected and not result['charge_pending']


@pytest.mark.parametrize("capability", ["people.email.find", "people.enrich", "people.phone.find"])
async def test_incomplete_name_is_rejected_before_quote_or_charge(clients, capability):
    before = await _balance(clients)
    response = await clients.post("/arena/plans", json={
        "capability": capability, "identity": {"full_name": "jason", "domain": "example.com"}})
    assert response.status_code == 422
    assert "first and last name" in response.json()["detail"]
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert not (await db.execute(select(ArenaRun))).scalars().all()
        assert not (await db.execute(select(Hold))).scalars().all()


@pytest.mark.parametrize("name", ["  Test   Person  ", "Mary-Jane O’Neill", "José García", "李 小龙"])
def test_name_validation_preserves_real_name_characters(name):
    identity = rules.validate_identity("people.email.find", {"full_name": name, "domain": "example.com"})
    assert identity["full_name"] == " ".join(name.split())


async def test_report_and_manual_vendor_extend_same_session_once(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({
        'tomba': [(200, TOMBA_HIT)], 'hunter': [(200, HUNTER_HIT)]}, seen))
    result = await finish(clients, await plan(clients, mode='waterfall'))
    first, remaining = result['results']
    root = '/arena/runs/' + result['id']
    assert first['can_try'] is False and remaining['can_try'] is True
    report_path = root + '/attempts/' + first['id'] + '/report'
    assert (await clients.post(report_path, json={'reason':'wrong_person', 'comment':'This belongs to someone else.'})).status_code == 200
    # Reports are durable and idempotent, independent of comparison votes.
    await clients.post(report_path, json={'reason':'other', 'comment':'Duplicate click'})
    reported = (await clients.get(root)).json()['results'][0]['report']
    assert reported['reason'] == 'wrong_person'
    before = await _balance(clients)
    path = root + '/attempts/' + remaining['id']
    q = (await clients.post(path + '/plan')).json()
    assert await _balance(clients) == before and len(seen) == 1
    starts = await asyncio.gather(*(clients.post(path + '/start', json={'quote_id':q['id']}) for _ in range(2)))
    assert sorted(r.status_code for r in starts) == [200, 409]
    task = arena._owners.get(result['id'])
    if task: await task
    extended = (await clients.get(root)).json()
    assert extended['id'] == result['id'] and extended['state'] == 'completed'
    assert len(seen) == 2
    assert extended['results'][0]['raw'] == first['raw']
    assert extended['results'][0]['report'] == reported
    extra = extended['results'][1]
    assert extra['state'] == 'hit' and extra['manual'] is True and not extra['can_try']
    assert extra['charged_micro'] == before - await _balance(clients) > 0
    assert extra['started_ms'] >= first['started_ms'] + first['duration_ms']
    assert (await clients.post(path + '/plan')).status_code == 409
    assert (await clients.post(path + '/start', json={'quote_id':q['id']})).status_code == 409
    assert len((await clients.get('/arena/runs')).json()) == 1
    async with session_maker() as db:
        row = await db.get(ArenaRun, result['id'])
        assert 'someone else' not in row.payload


async def test_manual_plan_rechecks_own_key_and_credit_admission(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)], 'hunter':[(200,HUNTER_HIT)]}, seen))
    result = await finish(clients, await plan(clients, mode='waterfall'))
    target = result['results'][1]
    path = '/arena/runs/' + result['id'] + '/attempts/' + target['id']
    q = (await clients.post(path+'/plan')).json()
    from treg.domain import money
    original = money.balance_of
    async def empty(*args, **kwargs): return 0
    monkeypatch.setattr(money, 'balance_of', empty)
    assert (await clients.post(path+'/start',json={'quote_id':q['id']})).status_code == 402
    assert len(seen) == 1
    monkeypatch.setattr(money, 'balance_of', original)
    await clients.post('/secrets',json={'name':'hunter','value':'OWN-KEY'})
    before = await _balance(clients)
    async def negative(*args, **kwargs): return -100
    monkeypatch.setattr(money, 'balance_of', negative)
    q = (await clients.post(path+'/plan')).json()
    assert q['estimate_micro'] == 0 and q['affordable'] is True
    assert (await clients.post(path+'/start',json={'quote_id':q['id']})).status_code == 200
    monkeypatch.setattr(money, 'balance_of', original)
    task = arena._owners.get(result['id'])
    if task: await task
    extra = (await clients.get('/arena/runs/'+result['id'])).json()['results'][1]
    assert extra['charged_micro'] == 0 and await _balance(clients) == before


async def test_manual_and_feedback_scope_validation_and_expiry(clients, enrichment_on, monkeypatch):
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)]}, []))
    result = await finish(clients, await plan(clients, mode='waterfall'))
    target = result['results'][1]
    path = '/arena/runs/' + result['id'] + '/attempts/' + target['id']
    assert (await clients.post(path+'/report',json={'reason':'incorrect_data'})).status_code == 422
    assert (await clients.post(path+'/rating',json={'value':'down'})).status_code == 422
    assert (await clients.post(path+'/plan',headers={'Origin':'https://evil.example'})).status_code == 403
    q = (await clients.post(path+'/plan')).json()
    async with session_maker() as db:
        row = await db.get(ArenaRun, result['id'])
        payload = arena._unpack(row.payload)
        payload['attempts'][1]['manual_quote']['expires_at'] = (utcnow_naive()-timedelta(seconds=1)).isoformat()
        row.payload = arena._pack(payload)
        db.add(row)
        await db.commit()
    assert (await clients.post(path+'/start',json={'quote_id':q['id']})).status_code == 409
    token = clients.headers['X-Treg-Token']
    other = (await clients.post('/users',json={'email':'manual-other@superdesign.dev'})).json()['token']
    clients.headers['X-Treg-Token'] = other
    for suffix, body in [('plan',{}),('start',{'quote_id':q['id']}),('report',{'reason':'incorrect_data'}),('rating',{'value':'down'})]:
        assert (await clients.post(path+'/'+suffix,json=body)).status_code == 404
    clients.headers['X-Treg-Token'] = token


@pytest.mark.parametrize('mode', ['waterfall', 'compare'])
async def test_ratings_persist_without_details_and_can_change(clients, enrichment_on, monkeypatch, mode):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({
        'tomba': [(200, TOMBA_HIT)], 'hunter': [(200, HUNTER_HIT)]}, seen))
    result = await finish(clients, await plan(clients, mode=mode))
    first = result['results'][0]
    root = '/arena/runs/' + result['id']
    path = root + '/attempts/' + first['id']
    before, calls = await _balance(clients), len(seen)
    response = await clients.post(path+'/rating', json={'value':'down'})
    assert response.status_code == 200
    rating = response.json()['rating']
    saved = (await clients.get(root)).json()['results'][0]
    assert saved['rating'] == rating and not saved.get('report')
    assert (await clients.post(path+'/rating', json={'value':'down'})).json()['rating'] == rating
    assert (await clients.post(path+'/report', json={'comment':'Wrong person'})).status_code == 200
    saved = (await clients.get(root)).json()['results'][0]
    assert saved['rating'] == rating and saved['report']['comment'] == 'Wrong person'
    assert (await clients.post(path+'/rating', json={'value':'up'})).status_code == 200
    saved = (await clients.get(root)).json()['results'][0]
    assert saved['rating']['value'] == 'up' and saved['rating']['created_at'] == rating['created_at']
    assert saved['output'] == first['output'] and saved['state'] == first['state']
    assert (await clients.post(path+'/report', json={'reason':'other'})).status_code == 409
    assert (await clients.get(root)).json()['vote'] is None
    assert await _balance(clients) == before and len(seen) == calls
    assert (await clients.post(path+'/rating', json={'value':'bad'})).status_code == 422
    assert (await clients.post(path+'/rating', json={'value':'down'}, headers={'Origin':'https://evil.example'})).status_code == 403
    async with session_maker() as db:
        row = await db.get(ArenaRun, result['id'])
        assert 'Wrong person' not in row.payload


@pytest.mark.parametrize('extra_credit,hit', [(-1, True), (0, True), (1, True), (0, False)])
async def test_waterfall_admits_cheapest_and_checks_later_balance(clients, enrichment_on, monkeypatch, extra_credit, hit):
    from treg.config import get_settings
    from treg.domain import money
    monkeypatch.setenv('TREG_PLATFORM_MARGIN', '0')
    get_settings.cache_clear()
    q = await plan(clients, mode='waterfall')
    prices = [p['estimate_micro'] for p in q['providers']]
    assert prices == sorted(prices) and q['required_micro'] == prices[0] < q['estimate_micro']
    target = prices[0] + extra_credit
    async with session_maker() as db:
        call_id = await money.reserve(db, q['org_id'], 'test.credit-spend', q['balance_micro'] - target)
        await money.settle(db, call_id)
    assert await _balance(clients) == target
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({
        'tomba': [(200, TOMBA_HIT if hit else {'data': {'email': None}})]}, seen))
    current = await plan(clients, mode='waterfall')
    assert current['affordable'] is (extra_credit >= 0)
    # Battle still requires all selected estimates, both at quote and start.
    battle = await plan(clients)
    assert battle['affordable'] is False
    assert (await clients.post('/arena/runs/'+battle['id']+'/start')).status_code == 402
    if extra_credit < 0:
        # The previously affordable quote is rechecked against the live balance.
        assert (await clients.post('/arena/runs/'+q['id']+'/start')).status_code == 402
        assert not seen
    else:
        result = await finish(clients, current)
        assert len(seen) == 1
        assert result['results'][0]['state'] == ('hit' if hit else 'miss')
        if not hit:
            assert result['results'][1]['state'] == 'error'
            assert result['results'][1]['charged_micro'] == 0
            assert 'balance' in result['stop_reason']
        assert await _balance(clients) == target - result['charged_micro'] >= 0
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


async def test_waterfall_can_start_own_key_at_zero_balance(clients, enrichment_on, monkeypatch):
    from treg.config import get_settings
    from treg.domain import money
    monkeypatch.setenv('TREG_PLATFORM_MARGIN', '0')
    get_settings.cache_clear()
    await clients.post('/secrets', json={'name':'hunter', 'value':'OWN-KEY'})
    q = await plan(clients, mode='waterfall')
    async with session_maker() as db:
        call_id = await money.reserve(db, q['org_id'], 'test.credit-spend', q['balance_micro'])
        await money.settle(db, call_id)
    q = await plan(clients, mode='waterfall')
    assert q['required_micro'] == 0 and q['affordable'] and q['estimate_micro'] > 0
    assert q['providers'][0]['provider'] == 'hunter'
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'hunter': [(200, HUNTER_HIT)]}, seen))
    result = await finish(clients, q)
    assert len(seen) == 1 and result['charged_micro'] == 0 and await _balance(clients) == 0


async def test_automatic_quotes_do_not_use_execution_limit_or_erase_history(clients, enrichment_on, monkeypatch):
    import uuid
    q = await plan(clients, mode='waterfall')
    async with session_maker() as db:
        template = (await db.get(ArenaRun, q['id'])).model_dump()
        for index in range(135):
            db.add(ArenaRun(**{**template, 'id':uuid.uuid4().hex, 'request_key':uuid.uuid4().hex,
                'created_at':utcnow_naive()-timedelta(minutes=10,seconds=index)}))
        historical = uuid.uuid4().hex
        db.add(ArenaRun(**{**template, 'id':historical, 'request_key':uuid.uuid4().hex, 'state':'completed'}))
        await db.commit()
    before = await _balance(clients)
    fresh = await plan(clients, mode='waterfall')
    async with session_maker() as db:
        assert len((await db.execute(select(ArenaRun).where(ArenaRun.state=='quoted'))).scalars().all()) == 100
        assert (await db.get(ArenaRun, historical)).state == 'completed'
    assert await _balance(clients) == before
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)]},seen))
    result = await finish(clients, fresh)
    assert result['results'][0]['state']=='hit' and len(seen)==1


async def test_hourly_limit_applies_at_start_but_pricing_remains_available(clients, enrichment_on, monkeypatch):
    import uuid
    q = await plan(clients, mode='waterfall')
    async with session_maker() as db:
        template = (await db.get(ArenaRun, q['id'])).model_dump()
        for _ in range(100):
            db.add(ArenaRun(**{**template, 'id':uuid.uuid4().hex, 'request_key':uuid.uuid4().hex, 'state':'completed'}))
        await db.commit()
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({},seen))
    fresh = await plan(clients, mode='waterfall')
    response = await clients.post('/arena/runs/'+fresh['id']+'/start')
    assert response.status_code == 429 and "price previews don't count" in response.text
    assert not seen


@pytest.mark.parametrize('verdict', ['valid', 'invalid'])
async def test_tomba_verification_sends_email_query_and_settles(clients, enrichment_on, monkeypatch, verdict):
    from treg.application.call.types import UpstreamResponse
    seen = []
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        seen.append(upstream_url)
        assert upstream_url == 'https://api.tomba.io/v1/email-verifier'
        assert dict(request.query_items) == {'email': 'person+tag@example.com'}
        async def body():
            yield json.dumps({'data': {'email': {'status': verdict, 'score': 99}}}).encode()
        async def close():
            pass
        return UpstreamResponse(200, ((b'content-type', b'application/json'),), body(), close)
    monkeypatch.setattr(service, 'relay', relay)
    q = await plan(clients, providers=['tomba'], capability='people.email.verify', identity={'email':'person+tag@example.com'})
    run = await finish(clients, q)
    assert len(seen) == 1
    assert run['results'][0]['output']['valid'] is (verdict == 'valid')
    assert run['results'][0]['output']['status'] == verdict
    assert run['results'][0]['state'] == 'hit'
    assert run['results'][0]['upstream_status'] == 200
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


def test_arena_masked_required_fields_are_not_hits_and_negative_verdicts_survive():
    from treg.domain.catalog import store
    cat = store.load()
    ep = cat.by_id['pdl.people.enrich']
    outcome, out = rules.classify(cat.contracts['people.enrich'], cat.adapters[ep['id']], ep, 200,
                                  {'data':{'full_name':True,'location_name':True}})
    assert outcome == 'miss'
    assert out['full_name'] is None and out['location'] is None
    ep = cat.by_id['hunter.people.email.verify']
    outcome, out = rules.classify(cat.contracts['people.email.verify'], cat.adapters[ep['id']], ep, 200,
                                  {'data':{'status':'invalid','score':0}})
    assert outcome == 'hit' and out['valid'] is False and out['score'] == 0


def test_arena_normalizes_history_without_changing_raw_responses_or_charges():
    raw = {'full_name':'Test Person','location':True,'linkedin_url':'person-example',
           'company_domain':'https://EXAMPLE.com/capital','website':'example.com',
           'verified':False,'employees':120,'founded':'2010'}
    attempt = {'id':'one','entry_index':0,'order':0,'display_order':0,'state':'hit',
               'output':raw.copy(),'raw':raw.copy(),'charged_micro':380000,'status':200}
    payload = {'attempts':[attempt]}
    row = rules.present(payload, mode='compare', state='completed', capability='people.enrich')['results'][0]
    assert row['output']['location'] is None
    assert row['output']['linkedin_url'] == 'https://www.linkedin.com/in/person-example'
    assert row['output']['company_domain'] == 'example.com'
    assert row['output']['website'] == 'https://example.com'
    assert row['output']['verified'] is False and row['output']['employees'] == 120
    assert row['output']['founded'] == '2010'
    assert row['raw'] == raw and attempt['output'] == raw and row['charged_micro'] == 380000
    assert rules.safe_output({'linkedin_url':'linkedin.com/company/example/'},capability='companies.enrich')['linkedin_url'] == 'https://www.linkedin.com/company/example'
    for value in ['javascript:alert(1)', 'https://linkedin.com.evil.test/in/person', 'https://a@linkedin.com/in/person', 'https://[invalid']:
        assert rules.safe_output({'linkedin_url':value})['linkedin_url'] is None


def test_provider_402_is_distinct_from_team_balance_refusal():
    attempt = {'id':'one','order':0,'display_order':0,'state':'error','status':402,'charged_micro':0}
    assert rules.present({'attempts':[attempt]},mode='compare',state='completed')['results'][0]['upstream_status'] == 402
    attempt['failure_kind'] = 'insufficient_balance'
    assert rules.present({'attempts':[attempt]},mode='compare',state='completed')['results'][0]['upstream_status'] is None


@pytest.mark.parametrize('capability,identity', [
    *[('people.email.verify', {'email': email}) for email in
      ['@', 'person@', '@example.com', 'person@@example.com', 'person@example',
       'person@ex\tample.com', 'person@example..com', 'person@example.com/path']],
    *[('companies.enrich', {'domain': domain}) for domain in
      ['https://[invalid', 'https://example.com:bad', 'https://user@example.com', 'ftp://example.com']],
    *[('people.enrich', {'linkedin_url': url}) for url in
      ['https://[invalid', 'https://linkedin.com:bad/in/person',
       'https://user@linkedin.com/in/person', 'https://linkedin.com/in/a b']],
])
async def test_malformed_identity_is_rejected_before_quote_or_charge(clients, enrichment_on, capability, identity):
    before = await _balance(clients)
    response = await clients.post('/arena/plans', json={'capability': capability, 'identity': identity})
    assert response.status_code == 422, response.text
    async with session_maker() as db:
        assert not (await db.execute(select(ArenaRun))).scalars().all()
        assert not (await db.execute(select(Hold))).scalars().all()
    assert await _balance(clients) == before


@pytest.mark.parametrize('email', ['person+tag@example.co.uk', "o\u0027connor@example.com", '名@example.com'])
def test_valid_email_forms_remain_accepted(email):
    assert rules.validate_identity('people.email.verify', {'email': email}) == {'email': email}


@pytest.mark.parametrize('mode', ['compare', 'waterfall'])
async def test_aviato_company_not_found_is_a_free_miss(clients, enrichment_on, monkeypatch, mode):
    from treg.application.call.types import UpstreamResponse
    async def relay(*args, **kwargs):
        async def body():
            yield b'Not Found'
        async def close():
            pass
        return UpstreamResponse(404, ((b'content-type', b'text/plain'),), body(), close)
    monkeypatch.setattr(service, 'relay', relay)
    before = await _balance(clients)
    run = await finish(clients, await plan(clients, mode=mode, providers=['aviato'],
        capability='companies.enrich', identity={'domain': 'microsoft.com'}))
    assert run['state'] == 'completed'
    assert run['results'][0]['state'] == 'miss'
    assert run['results'][0]['upstream_status'] == 404
    assert run['charged_micro'] == 0 and await _balance(clients) == before
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json', 'oversized'])
async def test_bad_vendor_response_finishes_and_finalizes_hold_once(clients, enrichment_on, monkeypatch, failure):
    from treg.application.call.types import UpstreamResponse
    closed = []
    async def relay(*args, **kwargs):
        if failure == 'timeout':
            raise TimeoutError('controlled upstream timeout')
        async def body():
            yield b'not-json' if failure == 'invalid_json' else b'x' * (rules.MAX_RESULT_BYTES + 1)
        async def close():
            closed.append(True)
        return UpstreamResponse(200, ((b'content-type', b'application/json'),), body(), close)
    monkeypatch.setattr(service, 'relay', relay)
    before = await _balance(clients)
    q = await plan(clients, providers=['hunter'])
    run = await finish(clients, q)
    attempt = run['results'][0]
    assert run['state'] == 'completed'
    assert attempt['state'] == ('timeout' if failure == 'timeout' else 'error')
    assert not run.get('charge_pending')
    assert run['charged_micro'] == before - await _balance(clients)
    assert closed == ([] if failure == 'timeout' else [True])
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()
        ledger = (await db.execute(select(LedgerEntry).where(LedgerEntry.call_id == attempt['call_ref']))).scalars().all()
        assert sum(entry.kind in {'settle', 'release'} for entry in ledger) == 1


async def test_history_pages_keep_tied_rows_stable_and_never_dispatch(clients, enrichment_on, monkeypatch):
    import uuid
    q = await plan(clients)
    before_balance = await _balance(clients)
    async def no_relay(*args, **kwargs):
        pytest.fail('Reading history must never call a vendor')
    monkeypatch.setattr(service, 'relay', no_relay)
    ids = [f'{i:032x}' for i in range(65)]
    async with session_maker() as db:
        template = (await db.get(ArenaRun, q['id'])).model_dump()
        # Identical timestamps force the ID tie-breaker across page boundaries.
        for run_id in ids:
            db.add(ArenaRun(**{**template, 'id': run_id, 'request_key': uuid.uuid4().hex, 'state': 'completed'}))
        await db.commit()
    default = (await clients.get('/arena/runs')).json()
    assert len(default) == 30 and [r['id'] for r in default] == list(reversed(ids))[:30]
    page = (await clients.get('/arena/runs?limit=31')).json()
    collected = page[:30]
    async with session_maker() as db:
        db.add(ArenaRun(**{**template, 'id': uuid.uuid4().hex, 'request_key': uuid.uuid4().hex,
            'state': 'completed', 'created_at': utcnow_naive() + timedelta(seconds=1)}))
        await db.commit()
    while len(page) > 30:
        response = await clients.get('/arena/runs', params={'limit': 31, 'before': collected[-1]['id']})
        assert response.status_code == 200
        page = response.json()
        collected.extend(page[:30])
    assert [r['id'] for r in collected] == list(reversed(ids))
    assert (await clients.get('/arena/runs', params={'before': ids[0]})).json() == []
    assert (await clients.get('/arena/runs', params={'before': q['id']})).status_code == 404
    for limit in [0, -1, 101]:
        assert (await clients.get('/arena/runs', params={'limit': limit})).status_code == 422
    other = (await clients.post('/users', json={'email': 'history-other@superdesign.dev'})).json()['token']
    assert (await clients.get('/arena/runs', params={'before': ids[-1]}, headers={'X-Treg-Token': other})).status_code == 404
    assert await _balance(clients) == before_balance
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


async def test_arena_and_dashboard_share_setup_components(clients):
    asset = await clients.get('/agent-setup.js')
    assert asset.status_code == 200 and 'no-cache' in asset.headers['cache-control']
    assert 'javascript' in asset.headers['content-type']
    assert 'SetupInstructions' in asset.text and 'AgentPicker' in asset.text
    page = (await clients.get('/enrich-arena')).text
    dashboard = (await clients.get('/app')).text
    assert '/agent-setup.js' in page and '/agent-setup.js' in dashboard
    assert 'treg-setup-instructions' in page and 'treg-setup-instructions' in dashboard
    assert 'Setup treg in' in page and 'ref="setupDialog"' in page


def test_discovery_public_cohorts_keep_all_requested_constraints():
    tasks = {t['id']: t for t in arena.public_tasks()}
    assert tasks['people.search']['discovery']
    role_country = tasks['people.search']['provider_previews'][1]
    assert role_country and 'lusha' not in {p['provider'] for p in role_country}
    company_role = tasks['people.company.search']['provider_previews'][1]
    assert company_role and not {'hunter','lusha','leadsforge'} & {p['provider'] for p in company_role}
    similar = tasks['companies.similar']['provider_previews'][0]
    assert {'tomba','companyenrich'} <= {p['provider'] for p in similar}
    assert tasks['companies.similar']['max_entries'] == 10
    assert next(p for p in similar if p['provider']=='companyenrich')['estimate_micro'] > next(p for p in similar if p['provider']=='tomba')['estimate_micro']


def test_search_outputs_are_bounded_sanitized_and_survive_presentation():
    output = rules.safe_output({'people':[{'name':'Example Person','linkedin_url':'javascript:bad','email':True,'title':False}]*30,'count':99999}, capability='people.search')
    assert output['count']==10 and len(output['people'])==10
    assert output['people'][0]=={'name':'Example Person'}
    assert rules.safe_output(output, capability='people.company.search') == output
    assert rules.safe_output({'companies':[{'name':False,'domain':'javascript:bad'}]},capability='companies.similar') == {'companies':[],'count':0}
    with pytest.raises(rules.ArenaError, match='10 entries'):
        rules.validate_entries('people.company.search',None,[{'company_domain':f'example{i}.test'} for i in range(11)])
    assert rules.validate_identity('people.company.search',{'company_domain':'https://Example.test/path'}) == {'company_domain':'example.test'}


async def test_people_search_executes_a_bounded_direct_query(clients, enrichment_on, monkeypatch):
    seen=[]
    monkeypatch.setattr(service,'relay',_relay_by_provider({'aviato':[(200,{'items':[{'fullName':'Example Person','headline':'Engineer','linkedinUrl':'https://www.linkedin.com/in/example'}]})]},seen))
    response=await clients.post('/arena/plans',json={'capability':'people.search','identity':{'q':'software engineer'},'providers':['aviato'],'mode':'compare','max_cost_micro':1_000_000})
    assert response.status_code==200,response.text
    result=await finish(clients,response.json())
    assert seen[0][3]['dsl']['limit']==10
    row=result['results'][0]
    assert row['state']=='hit' and row['output']['count']==1
    assert row['output']['people'][0]['name']=='Example Person'
    assert row['output']['people'][0]['linkedin_url']=='https://www.linkedin.com/in/example'


async def test_company_people_batch_preserves_each_list_and_settles_once(clients, enrichment_on, monkeypatch):
    seen=[]
    answer={'data':{'emails':[{'first_name':'Example','last_name':'Person','value':'example@example.test','position':'Engineer'}]}}
    monkeypatch.setattr(service,'relay',_relay_by_provider({'hunter':[(200,answer),(200,answer)]},seen))
    response=await clients.post('/arena/plans',json={'capability':'people.company.search','identities':[{'company_domain':'one.test'},{'company_domain':'two.test'}],'providers':['hunter'],'mode':'compare','max_cost_micro':1_000_000})
    assert response.status_code==200,response.text
    before=await _balance(clients)
    result=await finish(clients,response.json())
    assert len(seen)==2 and {r[2]['domain'] for r in seen}=={'one.test','two.test'}
    assert all(str(r[2]['limit'])=='10' for r in seen)
    assert all(r['state']=='hit' and r['output']['count']==1 for r in result['results'])
    assert {r['entry_index'] for r in result['results']}=={0,1}
    assert result['charged_micro']==before-await _balance(clients)
    await finish(clients,response.json())
    assert len(seen)==2
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


async def test_similar_companies_uses_saved_adapter_and_renders_company_rows(clients, enrichment_on, monkeypatch):
    seen=[]
    monkeypatch.setattr(service,'relay',_relay_by_provider({'tomba':[(200,{'data':[{'name':'Example Peer','website_url':'https://peer.test'}]})]},seen))
    response=await clients.post('/arena/plans',json={'capability':'companies.similar','identity':{'domain':'seed.test'},'providers':['tomba'],'mode':'compare','max_cost_micro':1_000_000})
    assert response.status_code==200,response.text
    result=await finish(clients,response.json())
    assert seen[0][2]['domain']=='seed.test'
    row=result['results'][0]
    assert row['state']=='hit' and row['output']['companies']==[{'name':'Example Peer','domain':'peer.test'}]


async def test_discovery_rejects_unknown_country_before_planning(clients, enrichment_on, monkeypatch):
    async def no_plan(*args, **kwargs):
        pytest.fail('Invalid countries must not be silently dropped during planning')
    monkeypatch.setattr(arena, '_plan_entry', no_plan)
    response = await clients.post('/arena/plans', json={
        'capability': 'people.search', 'identity': {'title': 'Engineer', 'country': 'ZZ'},
        'providers': ['icypeas'], 'mode': 'compare', 'max_cost_micro': 1_000_000})
    assert response.status_code == 422
    assert 'recognized two-letter country' in response.text


@pytest.mark.parametrize('source', [
    {'firstname': 'Example', 'lastname': 'Person', 'profileUrl': 'https://www.linkedin.com/in/example', 'lastJobTitle': 'Engineer'},
    {'name': 'Example Person', 'socials': {'linkedin_url': 'https://www.linkedin.com/in/example'}, 'position': 'Engineer'},
    {'fullName': 'Example Person', 'URLs': {'linkedin': 'https://www.linkedin.com/in/example'}, 'headline': 'Engineer'},
])
def test_discovery_normalizes_saved_vendor_response_shapes(source):
    output = rules.safe_output({'people': [source]}, capability='people.search')
    assert output == {'people': [{'name': 'Example Person', 'title': 'Engineer', 'linkedin_url': 'https://www.linkedin.com/in/example'}], 'count': 1}


async def test_companyenrich_similar_quotes_and_dispatches_the_explicit_page_limit(clients, enrichment_on, monkeypatch):
    from treg.config import get_settings
    monkeypatch.setenv('TREG_PLATFORM_KEY_COMPANYENRICH', 'PLATFORM-COMPANYENRICH-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'companyenrich')
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'companyenrich': [(200, {'items': [{'name': 'Example Peer', 'domain': 'peer.test'}], 'totalItems': 500})]}, seen))
    response = await clients.post('/arena/plans', json={
        'capability': 'companies.similar', 'identity': {'domain': 'seed.test'},
        'providers': ['companyenrich'], 'mode': 'compare', 'max_cost_micro': 1_000_000})
    assert response.status_code == 200, response.text
    result = await finish(clients, response.json())
    assert seen[0][3] == {'domains': ['seed.test'], 'page': 1, 'pageSize': 10}
    assert result['results'][0]['state'] == 'hit'
    assert result['results'][0]['output']['count'] == 1, 'Upstream total is not the number of displayed matches'
    assert result['results'][0]['output']['companies'][0]['domain'] == 'peer.test'
    task = next(t for t in arena.public_tasks() if t['id'] == 'companies.similar')
    assert response.json()['required_micro'] == next(p for p in task['provider_previews'][0] if p['provider'] == 'companyenrich')['estimate_micro']


@pytest.mark.parametrize("cancel", [False, True])
async def test_parallel_manual_providers_preserve_results_and_settle_once(clients, enrichment_on, monkeypatch, cancel):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba': [(200, TOMBA_HIT)]}, seen))
    result = await finish(clients, await plan(clients, mode='waterfall', providers=['tomba', 'findymail', 'hunter']))
    root = '/arena/runs/' + result['id']
    remaining = [r for r in result['results'] if r['can_try']]
    assert len(remaining) == 2
    gate = {r['provider']: asyncio.Event() for r in remaining}
    entered = {r['provider']: asyncio.Event() for r in remaining}
    relay = _relay_by_provider({'hunter': [(200, HUNTER_HIT)], 'findymail': [(200, {'contact': {'name':'Test Person','email':'test@example.com'}})]}, seen)
    async def blocked(request, upstream_url, *args, **kwargs):
        provider = 'hunter' if 'hunter' in upstream_url else 'findymail'
        entered[provider].set()
        await gate[provider].wait()
        return await relay(request, upstream_url, *args, **kwargs)
    monkeypatch.setattr(service, 'relay', blocked)
    before = await _balance(clients)
    for target in remaining:
        path = root + '/attempts/' + target['id']
        q = await clients.post(path + '/plan')
        assert q.status_code == 200, q.text
        starts = await asyncio.gather(*(clients.post(path + '/start', json={'quote_id': q.json()['id']}) for _ in range(2)))
        assert sorted(r.status_code for r in starts) == [200, 409]
        await asyncio.wait_for(entered[target['provider']].wait(), 5)
    owners = list(arena._owners.values())
    assert len(owners) == 2
    running = (await clients.get(root)).json()
    assert sum(r['state'] == 'running' for r in running['results']) == 2
    assert running['results'][0]['raw'] == result['results'][0]['raw']
    if cancel:
        await clients.post(root + '/cancel')
        await asyncio.wait_for(asyncio.gather(*owners), 5)
    else:
        gate[remaining[0]['provider']].set()
        await asyncio.wait(owners, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        middle = (await clients.get(root)).json()
        assert middle['state'] == 'running'
        assert sum(r['state'] == 'running' for r in middle['results']) == 1
        gate[remaining[1]['provider']].set()
        await asyncio.wait_for(asyncio.gather(*owners), 5)
    final = (await clients.get(root)).json()
    assert final['state'] == ('cancelled' if cancel else 'completed')
    assert final['results'][0]['raw'] == result['results'][0]['raw']
    extras = [r for r in final['results'] if r.get('manual')]
    assert len(extras) == 2 and all(not r['can_try'] for r in extras)
    assert all(r['state'] == ('cancelled' if cancel else 'hit') for r in extras)
    assert before - await _balance(clients) == sum(r['charged_micro'] for r in extras)
    async with session_maker() as db:
        for r in extras:
            settled = (await db.execute(select(LedgerEntry).where(LedgerEntry.call_id == r['call_ref'], LedgerEntry.kind.in_(['settle','release'])))).scalars().all()
            assert len(settled) == 1


async def test_try_uncalled_batch_cell_while_original_waterfall_runs(clients, enrichment_on, monkeypatch):
    seen = []
    gate, entered = asyncio.Event(), asyncio.Event()
    relay = _relay_by_provider({'tomba': [(200, TOMBA_HIT), (200, {'data': {}})], 'hunter': [(200, HUNTER_HIT)] * 2}, seen)
    async def blocked(request, upstream_url, *args, **kwargs):
        if 'hunter' in upstream_url and dict(request.query_items).get('domain') == SECOND_IDENTITY['domain']:
            entered.set()
            await gate.wait()
        return await relay(request, upstream_url, *args, **kwargs)
    monkeypatch.setattr(service, 'relay', blocked)
    q = await batch_plan(clients, mode='waterfall')
    root = '/arena/runs/' + q['id']
    assert (await clients.post(root + '/start')).status_code == 200
    original = arena._owners[q['id']]
    await asyncio.wait_for(entered.wait(), 5)
    current = (await clients.get(root)).json()
    target = next(r for r in current['results'] if r['entry_index'] == 0 and r['provider'] == 'hunter')
    assert target['can_try']
    path = root + '/attempts/' + target['id']
    quote = (await clients.post(path + '/plan')).json()
    assert (await clients.post(path + '/start', json={'quote_id':quote['id']})).status_code == 200
    manual = next(t for t in arena._owners.values() if t is not original)
    await asyncio.wait_for(asyncio.shield(manual), 5)
    assert (await clients.get(root)).json()['state'] == 'running'
    gate.set()
    await asyncio.wait_for(asyncio.shield(original), 5)
    final = (await clients.get(root)).json()
    assert final['state'] == 'completed'
    extra = next(r for r in final['results'] if r['id'] == target['id'])
    assert extra['state'] == 'hit' and extra['manual'] and extra['call_ref']
    assert len(seen) == 4


async def test_auto_email_verification_is_priced_and_invalid_verdict_preserves_email(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)], 'leadmagic':[(200,{'email_status':'invalid'})]}, seen))
    base = await plan(clients, mode='waterfall')
    q = await plan(clients, mode='waterfall', auto_verify=True)
    assert q['auto_verify']['provider'] == 'treg'
    assert q['required_micro'] == base['required_micro'] + q['verification_estimate_micro']
    before = await _balance(clients)
    final = await finish(clients, q)
    hit = next(r for r in final['results'] if r['state'] == 'hit')
    assert hit['output']['email'] == 'another@example.com'
    v = hit['verification']
    assert v['state'] == 'hit' and v['output']['valid'] is False and v['output']['status'] == 'invalid'
    assert not hit['can_verify'] and final['state'] == 'completed'
    assert seen[1][3]['email'] == hit['output']['email']
    assert before - await _balance(clients) == final['charged_micro'] == hit['charged_micro']
    assert hit['charged_micro'] == hit['lookup_charged_micro'] + v['charged_micro']
    assert hit['report']['reason'] == 'incorrect_data'
    assert hit['report']['feedback_context'] == 'automated_verification'
    assert hit['report']['verification_id'] == v['id']
    reread = (await clients.get('/arena/runs/'+final['id'])).json()
    assert reread['results'][0]['report'] == hit['report']


async def test_auto_verification_skips_misses_and_enforces_budget(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,{'data':{}})]}, seen))
    final = await finish(clients, await plan(clients, mode='waterfall', providers=['tomba'], auto_verify=True))
    assert len(seen) == 1 and 'verification' not in final['results'][0]
    q = await plan(clients, mode='waterfall', providers=['tomba'], auto_verify=True)
    rejected = await clients.post('/arena/plans', json={'capability':'people.email.find','identity':IDENTITY,'mode':'waterfall','providers':['tomba'],'auto_verify':True,'max_cost_micro':q['required_micro']-1})
    assert rejected.status_code == 422


@pytest.mark.parametrize('cancel', [False, True])
async def test_one_click_verification_claims_once_and_retains_lookup_on_cancel(clients, enrichment_on, monkeypatch, cancel):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)]}, seen))
    final = await finish(clients, await plan(clients, mode='waterfall'))
    hit = final['results'][0]
    assert hit['can_verify']
    root = '/arena/runs/'+final['id']
    path = root+'/attempts/'+hit['id']+'/verification'
    before = await _balance(clients)
    q = (await clients.post(path+'/plan')).json()
    assert await _balance(clients) == before
    entered, gate = asyncio.Event(), asyncio.Event()
    relay = _relay_by_provider({'leadmagic':[(200,{'email_status':'catch_all'})]}, seen)
    async def blocked(*args, **kwargs):
        entered.set()
        await gate.wait()
        return await relay(*args, **kwargs)
    monkeypatch.setattr(service, 'relay', blocked)
    starts = await asyncio.gather(*(clients.post(path+'/start',json={'quote_id':q['id']}) for _ in range(2)))
    assert sorted(r.status_code for r in starts) == [200,409]
    owner = arena._owners[final['id']]
    await asyncio.wait_for(entered.wait(),5)
    running = (await clients.get(root)).json()
    assert running['results'][0]['verification']['state'] == 'running'
    assert not running['results'][0]['can_verify']
    if cancel:
        await clients.post(root+'/cancel')
    else:
        gate.set()
    await asyncio.wait_for(asyncio.shield(owner),5)
    checked = (await clients.get(root)).json()['results'][0]
    assert checked['output'] == hit['output']
    assert checked['verification']['state'] == ('cancelled' if cancel else 'hit')
    if not cancel: assert checked['verification']['output']['status'] == 'catch_all'
    assert checked['charged_micro'] - hit['charged_micro'] == before - await _balance(clients)
    assert (await clients.post(path+'/plan')).status_code == 409


async def test_phone_verification_task_and_auto_phone_lookup(clients, enrichment_on, monkeypatch):
    seen = []
    response = {'data':{'valid':False,'e164_format':'+14155550100','country_code':'US','line_type':'FIXED_LINE_OR_MOBILE','carrier':''}}
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,response)]}, seen))
    q = await clients.post('/arena/plans', json={'capability':'people.phone.verify','identity':{'phone':'+1 (415) 555-0100'},'providers':['tomba'],'mode':'waterfall'})
    assert q.status_code == 200, q.text
    final = await finish(clients,q.json())
    assert final['results'][0]['state'] == 'hit' and final['results'][0]['output']['valid'] is False
    assert seen[0][2]['phone'] == '+14155550100'
    seen.clear()
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,{'data':{'e164_format':'+14155550100'}}),(200,response)]}, seen))
    q = await clients.post('/arena/plans', json={'capability':'people.phone.find','identity':{'linkedin_url':'https://www.linkedin.com/in/example'},'providers':['tomba'],'mode':'waterfall','auto_verify':True})
    assert q.status_code == 200, q.text
    final = await finish(clients,q.json())
    assert final['results'][0]['verification']['output']['valid'] is False
    assert len(seen) == 2


async def test_verification_credit_admission_and_ownership(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)]}, seen))
    final = await finish(clients,await plan(clients,mode='waterfall'))
    path = '/arena/runs/'+final['id']+'/attempts/'+final['results'][0]['id']+'/verification'
    q = (await clients.post(path+'/plan')).json()
    from treg.domain import money
    async def empty(*args,**kwargs): return 0
    monkeypatch.setattr(money,'balance_of',empty)
    assert (await clients.post(path+'/start',json={'quote_id':q['id']})).status_code == 402
    assert (await clients.post(path+'/plan')).json()['affordable'] is False
    assert len(seen) == 1
    assert (await clients.post(path+'/plan',headers={'Origin':'https://evil.example'})).status_code == 403
    other = (await clients.post('/users',json={'email':'verify-other@superdesign.dev'})).json()['token']
    assert (await clients.post(path+'/start',json={'quote_id':q['id']},headers={'X-Treg-Token':other})).status_code == 404


def test_phone_identity_requires_country_calling_code():
    assert rules.validate_identity('people.phone.verify',{'phone':'+44 20 7946 0958'}) == {'phone':'+442079460958'}
    with pytest.raises(rules.ArenaError): rules.validate_identity('people.phone.verify',{'phone':'4155550100'})

async def test_batch_auto_verifies_each_found_entry(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)]*2, 'leadmagic':[(200,{'email_status':'valid'})]*2}, seen))
    q = await batch_plan(clients, mode='compare', providers=['tomba'], auto_verify=True)
    final = await finish(clients, q)
    assert len(seen) == 4
    assert len(final['results']) == 2
    assert all(r['verification']['output']['valid'] is True for r in final['results'])
    assert final['charged_micro'] == sum(r['charged_micro'] for r in final['results'])


async def test_extra_lookup_admits_auto_verification_price(clients, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)], 'hunter':[(200,HUNTER_HIT)], 'leadmagic':[(200,{'email_status':'valid'})]*2}, seen))
    final = await finish(clients, await plan(clients, mode='waterfall', auto_verify=True))
    target = next(r for r in final['results'] if r['can_try'])
    root = '/arena/runs/'+final['id']
    path = root+'/attempts/'+target['id']
    q = (await clients.post(path+'/plan')).json()
    assert q['required_micro'] == q['estimate_micro'] + final['auto_verify']['estimate_micro']
    assert (await clients.post(path+'/start',json={'quote_id':q['id']})).status_code == 200
    await asyncio.wait_for(asyncio.shield(arena._owners[final['id']]),5)
    result = (await clients.get(root)).json()
    assert all(r['verification']['state'] == 'hit' for r in result['results'])
    assert len(seen) == 4

@pytest.mark.parametrize('status,prior_report', [('invalid',False),('invalid',True),('catch_all',False),('unknown',False),('valid',False)])
async def test_table_verification_reports_only_invalid_email_and_preserves_feedback(clients, enrichment_on, monkeypatch, status, prior_report):
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'tomba':[(200,TOMBA_HIT)], 'leadmagic':[(200,{'email_status':status})]}, seen))
    final = await finish(clients, await plan(clients, mode='waterfall', providers=['tomba']))
    lookup = final['results'][0]
    root = '/arena/runs/'+final['id']
    path = root+'/attempts/'+lookup['id']
    existing = None
    if prior_report:
        response = await clients.post(path+'/report',json={'reason':'wrong_person','comment':'Existing user feedback'})
        assert response.status_code == 200
        existing = response.json()['report']
        assert (await clients.post(path+'/rating',json={'value':'up'})).status_code == 200
    q = (await clients.post(path+'/verification/plan')).json()
    assert (await clients.post(path+'/verification/start',json={'quote_id':q['id']})).status_code == 200
    await asyncio.wait_for(asyncio.shield(arena._owners[final['id']]),5)
    checked = (await clients.get(root)).json()['results'][0]
    assert checked['output'] == lookup['output'] and checked['state'] == 'hit'
    if prior_report:
        assert checked['report'] == existing and checked['rating']['value'] == 'up'
    elif status == 'invalid':
        assert checked['report']['feedback_context'] == 'automated_verification'
        assert checked['report']['verification_id'] == checked['verification']['id']
        # A later human decision takes precedence and survives subsequent reads.
        assert (await clients.post(path+'/rating',json={'value':'up'})).status_code == 200
        assert (await clients.get(root)).json()['results'][0]['rating']['value'] == 'up'
    else:
        assert not checked.get('report')


@pytest.mark.parametrize('task,state,status,valid,expected', [
    ('people.email.verify','hit','undeliverable',False,True),
    ('people.email.verify','hit','risky',False,False),
    ('people.email.verify','hit','',False,False),
    ('people.email.verify','error','invalid',False,False),
    ('people.email.verify','cancelled','invalid',False,False),
    ('people.phone.verify','hit','invalid',False,False),
    ('people.email.verify','hit','invalid',True,False),
])
def test_issue_requires_completed_explicit_negative_email_verdict(task,state,status,valid,expected):
    attempt = {'state':'hit','verification':{'capability':task,'state':state,'output':{'valid':valid,'status':status}}}
    assert rules.verification_rejected_email(attempt,'people.email.find') is expected
    assert not rules.verification_rejected_email(attempt,'people.phone.find')

async def test_new_free_verifier_is_selected_for_auto_verification(clients, enrichment_on, monkeypatch):
    from treg.config import get_settings
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS','tomba,contactout,trykitt,millionverifier')
    for name in ['CONTACTOUT','TRYKITT','MILLIONVERIFIER']:
        monkeypatch.setenv('TREG_PLATFORM_KEY_'+name,'SYNTHETIC-VERIFICATION-KEY')
    get_settings.cache_clear()
    seen=[]
    monkeypatch.setattr(service,'relay',_relay_by_provider({'tomba':[(200,TOMBA_HIT)],'contactout':[(200,{'status_code':200,'data':{'status':'valid'}})]},seen))
    q=await plan(clients,mode='waterfall',providers=['tomba'],auto_verify=True)
    assert q['auto_verify']['provider']=='treg'
    assert q['auto_verify']['estimate_micro']>0
    final=await finish(clients,q)
    result=final['results'][0]
    assert result['verification']['provider']=='contactout'
    assert result['verification']['output']['valid'] is True
    assert result['verification']['charged_micro']==0
    assert result['charged_micro']==result['lookup_charged_micro']
    assert [c[0] for c in seen]==['tomba','contactout']

@pytest.mark.parametrize('automatic', [True, False])
@pytest.mark.parametrize('first_response', [(403, {'status_code':403,'message':'Out of credits'}), (200, {'status_code':200,'data':{}})])
@pytest.mark.parametrize('verdict', ['valid', 'invalid', 'catch_all'])
async def test_email_verification_uses_routed_fallback_and_accounts_for_children(clients, enrichment_on, monkeypatch, automatic, first_response, verdict):
    from treg.config import get_settings
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS','tomba,contactout,leadmagic')
    monkeypatch.setenv('TREG_PLATFORM_KEY_CONTACTOUT','SYNTHETIC-VERIFICATION-KEY')
    get_settings.cache_clear()
    seen=[]
    monkeypatch.setattr(service,'relay',_relay_by_provider({
        'tomba':[(200,TOMBA_HIT)], 'contactout':[first_response],
        'leadmagic':[(200,{'email_status':verdict})]},seen))
    before=await _balance(clients)
    final=await finish(clients,await plan(clients,mode='waterfall',providers=['tomba'],auto_verify=automatic))
    if not automatic:
        path='/arena/runs/'+final['id']+'/attempts/'+final['results'][0]['id']+'/verification'
        q=(await clients.post(path+'/plan')).json()
        assert q['provider']=='treg' and q['estimate_micro']>0
        assert (await clients.post(path+'/start',json={'quote_id':q['id']})).status_code==200
        await asyncio.wait_for(asyncio.shield(arena._owners[final['id']]),15)
        final=(await clients.get('/arena/runs/'+final['id'])).json()
    v=final['results'][0]['verification']
    assert v['state']=='hit' and v['provider']=='leadmagic'
    assert v['served_by']=='leadmagic.people.email.verify'
    assert v['output']['status']==verdict
    assert [t['provider'] for t in v['tried']]==['contactout','leadmagic']
    assert [c[0] for c in seen]==['tomba','contactout','leadmagic']
    assert before-await _balance(clients)==final['charged_micro']
    assert v['charged_micro']>0
    assert bool(final['results'][0].get('report'))==(verdict=='invalid')
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()


def test_every_task_input_type_has_two_valid_public_examples():
    tasks=arena.public_tasks()
    for task in tasks:
        assert len(task['examples'])==len(task['variants'])
        for variant,entries in zip(task['variants'],task['examples']):
            assert len(entries)==2
            assert all(set(row)==set(variant) for row in entries)
            assert len(rules.validate_entries(task['id'],None,entries))==2
        if task['id']=='companies.enrich':
            linked=next(entries for variant,entries in zip(task['variants'],task['examples']) if variant==('linkedin_url',))
            assert all('/company/' in row['linkedin_url'] for row in linked)


async def test_tracking_counts_one_batch_run_and_keeps_inputs_out(clients, enrichment_on, monkeypatch):
    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, "capture", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(service, "relay", _relay_by_provider({
        "hunter": [(200, HUNTER_HIT)] * 2, "tomba": [(200, TOMBA_HIT)] * 2}, []))
    q = await batch_plan(clients)
    assert not [a for a, k in events if a[1].startswith("arena_run_")]
    await finish(clients, q)
    await finish(clients, q)
    logical = [(a, k) for a, k in events if a[1].startswith("arena_run_")]
    assert [a[1] for a, k in logical] == ["arena_run_started", "arena_run_completed"]
    assert all(a[2]["entry_count"] == 2 and a[2]["run_id"] == q["id"] for a, k in logical)
    assert logical[-1][0][2]["successful_call"] is True
    assert logical[-1][0][2]["returned_data"] is True
    assert all(k["groups"]["team"] for a, k in logical)
    calls = [a for a, k in events if a[1] == "tool_called"]
    assert len(calls) == 4
    assert all(a[2]["client"] == "enrich-arena" for a in calls)
    # Event identity is the account email; search inputs and returned contact data are absent.
    props = json.dumps([a[2] for a, k in events])
    assert IDENTITY["domain"] not in props and SECOND_IDENTITY["domain"] not in props

@pytest.mark.parametrize('automatic', [True, False])
@pytest.mark.parametrize('country', ['US', 'GB', None, 'N/A'])
async def test_national_phone_verification_preserves_provider_country(clients, enrichment_on, monkeypatch, automatic, country):
    from treg.config import get_settings
    monkeypatch.setenv('TREG_PLATFORM_KEY_QUICKENRICH', 'PLATFORM-QUICKENRICH-KEY')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'quickenrich,tomba')
    get_settings.cache_clear()
    phone = '020 7946 0958' if country == 'GB' else '415-555-0100'
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({
        'quickenrich': [(200, {'success':True,'data':{'employee_phone':phone,'country_code':country},'meta':{'credits_used':1}})],
        'tomba': [(200, {'data':{'valid':True,'country_code':country}})],
    }, seen))
    q = await clients.post('/arena/plans', json={'capability':'people.phone.find','identity':{'linkedin_url':'https://www.linkedin.com/in/example'},'providers':['quickenrich'],'mode':'waterfall','auto_verify':automatic})
    assert q.status_code == 200, q.text
    before = await _balance(clients)
    final = await finish(clients, q.json())
    if not automatic:
        root = '/arena/runs/'+final['id']+'/attempts/'+final['results'][0]['id']+'/verification'
        quote = await clients.post(root+'/plan')
        if country not in ('US','GB'):
            assert quote.status_code == 422 and 'country code' in quote.json()['detail']
        else:
            assert quote.status_code == 200, quote.text
            assert (await clients.post(root+'/start', json={'quote_id':quote.json()['id']})).status_code == 200
            await asyncio.wait_for(asyncio.shield(arena._owners[final['id']]), 10)
            final = (await clients.get('/arena/runs/'+final['id'])).json()
    hit = final['results'][0]
    assert hit['output']['phone'] == phone, 'Keep the provider value as returned'
    if country in ('US','GB'):
        assert hit['verification']['state'] == 'hit'
        assert seen[1][2] == {'phone':phone.replace(' ','').replace('-',''), 'country_code':country}
        assert len(seen) == 2
    else:
        assert len(seen) == 1, 'Do not guess US or dispatch an uncheckable number'
        if automatic:
            assert hit['verification']['charged_micro'] == 0
            assert hit['verification']['not_started'] is True
            assert 'country code' in hit['verification']['detail']
    assert final['charged_micro'] == before - await _balance(clients)


def test_phone_verification_context_does_not_override_explicit_calling_code():
    assert rules.verification_identity('people.phone.verify', {'phone':'+44 20 7946 0958','country_code':'US'}) == {'phone':'+442079460958'}
    assert rules.validate_identity('people.phone.verify', {'phone':'020 7946 0958','country_code':'gb'}) == {'phone':'02079460958','country_code':'GB'}
    with pytest.raises(rules.ArenaError):
        rules.validate_identity('people.phone.verify', {'phone':'4155550100','country_code':'USA'})


async def test_run_finishes_while_cancel_poll_is_reading(clients, enrichment_on, monkeypatch):
    """Finish a real run while the cancellation poll is materializing a SQLite SELECT."""
    from aiosqlite import Cursor
    from treg.infra import db as database

    if not database._is_sqlite:
        pytest.skip("SQLite cursor cancellation regression")
    queried = asyncio.Event()
    release = asyncio.Event()
    interrupted = []
    execute = Cursor.execute
    save = arena._save

    async def slow_poll(cursor, sql, parameters=None):
        result = await execute(cursor, sql, parameters)
        if ("watch_cancel" in asyncio.current_task().get_coro().__qualname__
                and sql.startswith("SELECT") and not queried.is_set()):
            queried.set()
            asyncio.get_running_loop().call_later(0.05, release.set)
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.append(True)
                raise
        return result

    async def last_save(run_id, payload, state=None, **kwargs):
        await save(run_id, payload, state, **kwargs)
        if state is None and all(a["state"] in {"hit", "miss"} for a in payload["attempts"]):
            await asyncio.wait_for(queried.wait(), 5)

    monkeypatch.setattr(Cursor, "execute", slow_poll)
    monkeypatch.setattr(arena, "_save", last_save)
    monkeypatch.setattr(service, "relay", _relay_by_provider({
        "hunter": [(200, HUNTER_HIT)], "tomba": [(200, TOMBA_HIT)]}, []))
    try:
        result = await finish(clients, await plan(clients))
        assert result["state"] == "completed"
        assert not interrupted, "run completion cancelled the poll inside SQLite cursor execution"
        # Match the next fixture boundary: drain the writes, then rebuild the schema.
        from treg import archive, audit
        await audit.drain()
        await archive.drain()
        await database.reset_db()
    finally:
        release.set()
