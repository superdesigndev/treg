"""The tool hub, phase 1: the manifest validator, the table, the publish route, and where a hub
id sits on the call road (behind an own tool and a catalog id, and behind TREG_HUB_ENABLED).

Nothing runs yet. A published tool is stored `unchecked`; the call road answers 501 for it when
the flag is on and 404 (exactly as today) when the flag is off.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from treg.config import get_settings
from treg.domain.hub import ManifestError, validate, validate_check
from treg.domain.catalog import store as catalog_store

EP = "tikhub.tiktok.video.comments"      # a real catalog id
CATALOG = set(catalog_store.load().by_id)


def _steps_manifest(**over):
    m = {
        "name": "leads-db",
        "summary": "Decision makers of a company, with verified emails.",
        "inputs": {
            "domain": {"type": "string", "example": "figma.com"},
            "limit": {"type": "int", "default": 10, "max": 25},
        },
        "uses": [EP, "supabase"],
        "steps": [
            {"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}},
            {"name": "rows", "call": "supabase", "input": {"q": "$people.data"}},
        ],
        "output": {"leads": "$rows.body", "count": "$people.data.length"},
        "price_usd": 0.01,
    }
    m.update(over)
    return m


def _script_manifest(**over):
    m = {
        "name": "leads-db",
        "summary": "Rows from my Supabase table, filtered.",
        "inputs": {"search": {"type": "string", "default": ""},
                   "limit": {"type": "int", "default": 20, "max": 100}},
        "uses": ["supabase"],
        "script": "run.js",
        "output": {"fields": ["rows", "count"]},
    }
    m.update(over)
    return m


CHECK = {"inputs": {"domain": "figma.com"}, "fields": ["leads"]}
OWN = {"supabase"}


# ---------------------------------------------------------------------------------------------
# The validator: every refusal names the field and the rule

def _refused(manifest, **kw):
    with pytest.raises(ManifestError) as e:
        validate(manifest, catalog_ids=CATALOG, own_tools=OWN, **kw)
    return e.value


def test_valid_steps_manifest_normalizes():
    v = validate(_steps_manifest(), catalog_ids=CATALOG, own_tools=OWN)
    assert v.kind == "steps" and v.price_micro == 10_000
    assert v.limits == {"steps": 20, "wall_s": 120, "cost_usd": None}
    assert v.inputs["domain"]["secret"] is False


def test_valid_script_manifest():
    v = validate(_script_manifest(), catalog_ids=CATALOG, own_tools=OWN)
    assert v.kind == "script" and v.script == "run.js" and v.output == {"fields": ["rows", "count"]}


@pytest.mark.parametrize("change,field,rule_words", [
    ({"name": "Leads DB"}, "name", "lowercase"),
    ({"summary": ""}, "summary", "required"),
    ({"uses": [EP, "nope.tool"]}, "uses[1]", "not a catalog id"),
    ({"uses": [EP, "sheets"]}, "uses[1]", "not one of your team's tools"),
    ({"steps": [{"name": "a", "call": "supabase", "input": {}}, {"name": "a", "call": EP, "input": {}}]},
     "steps[1].name", "duplicate"),
    ({"steps": [{"name": "a", "call": "hunter.people.email.find", "input": {}}]}, "steps[0].call", "not in `uses`"),
    ({"steps": [{"name": "a", "call": EP, "input": {}, "for_each": "$x"}]}, "steps[0].for_each", "go together"),
    ({"output": {"leads": "literal"}}, "output.leads", "reference"),
    ({"price_usd": -1}, "price_usd", "0 to 100"),
    ({"price_usd": 0.0000001}, "price_usd", "6 decimal"),
    ({"limits": {"steps": 21}}, "limits.steps", "1-20"),
    ({"limits": {"wall_s": 121}}, "limits.wall_s", "1-120"),
    ({"bogus": 1}, "bogus", "unknown field"),
])
def test_refusals_name_field_and_rule(change, field, rule_words):
    err = _refused(_steps_manifest(**change))
    assert err.field == field, (err.field, err.rule)
    assert rule_words in err.rule


@pytest.mark.parametrize("spec,field,rule_words", [
    ({"type": "string", "required": True, "example": "x"}, "inputs.q.required", "no `default`"),
    ({"type": "string"}, "inputs.q", "example"),
    ({"type": "int", "default": 1}, "inputs.q.max", "clamp"),
    ({"type": "int", "default": 50, "max": 10}, "inputs.q.default", "within"),
    ({"type": "colour", "example": "x"}, "inputs.q.type", "one of"),
    ({"type": "int", "secret": True, "max": 3}, "inputs.q.secret", "string"),
])
def test_input_rules_from_crawl4ai_lessons(spec, field, rule_words):
    err = _refused(_steps_manifest(inputs={"q": spec}))
    assert err.field == field and rule_words in err.rule


def test_exactly_one_of_steps_or_script():
    both = _steps_manifest(script="run.js")
    assert "both" in _refused(both).rule
    neither = _steps_manifest(); del neither["steps"]
    assert "neither" in _refused(neither).rule


def test_a_hub_tool_may_not_use_a_hub_tool():
    err = _refused(_steps_manifest(uses=[EP, "acme.other"]), hub_ids={"acme.other"})
    assert err.field == "uses[1]" and "may not use a hub tool" in err.rule


def test_check_file_is_validated_against_the_manifest():
    v = validate(_steps_manifest(), catalog_ids=CATALOG, own_tools=OWN)
    fields = list(v.output)
    with pytest.raises(ManifestError) as e:
        validate_check({"inputs": {}, "fields": ["leads"]}, v.inputs, fields)
    assert e.value.field == "check.inputs.domain"          # required input missing from the sample
    with pytest.raises(ManifestError) as e:
        validate_check({"inputs": {"domain": "x"}, "fields": ["nope"]}, v.inputs, fields)
    assert e.value.field == "check.fields"
    ok = validate_check({"inputs": {"domain": "x", "limit": 3}, "fields": ["leads"]}, v.inputs, fields)
    assert ok == {"inputs": {"domain": "x", "limit": 3}, "fields": ["leads"], "min_rows": 0}


# ---------------------------------------------------------------------------------------------
# The route and the table

@pytest.fixture
def hub_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "hub_enabled", True)


async def _own_supabase(clients: AsyncClient) -> None:
    sid = (await clients.post("/secrets", json={"name": "sb-key", "value": "SB"})).json()["id"]
    r = await clients.post("/tools", json={"name": "supabase", "base_url": "https://x.supabase.co", "secret_id": sid})
    assert r.status_code in (200, 201), r.text


async def test_hub_routes_do_not_exist_with_the_flag_off(clients: AsyncClient):
    r = await clients.post("/hub/tools", json={"manifest": {}, "check": {}, "readme": "x"})
    assert r.status_code == 404
    assert (await clients.get("/hub/tools/mine")).status_code == 404


async def test_publish_stores_unchecked_and_reads_back(clients: AsyncClient, hub_on):
    await _own_supabase(clients)
    r = await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(), "check": CHECK, "readme": "Leads from my table."})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1 and body["status"] == "unchecked" and body["kind"] == "steps"
    tool_id = body["tool_id"]
    assert tool_id.endswith(".leads-db") and "." in tool_id

    mine = (await clients.get("/hub/tools/mine")).json()
    assert [m["tool_id"] for m in mine] == [tool_id]
    one = (await clients.get(f"/hub/tools/{tool_id}")).json()
    assert one["price_usd"] == 0.01 and one["uses"] == [EP, "supabase"] and one["readme"]

    # publishing again is a new version
    r2 = await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(summary="v2"), "check": CHECK, "readme": "v2"})
    assert r2.json()["version"] == 2
    assert (await clients.get(f"/hub/tools/{tool_id}@1")).json()["summary"] != "v2"
    assert (await clients.get(f"/hub/tools/{tool_id}")).json()["summary"] == "v2"


async def test_publish_refusal_names_field_and_rule(clients: AsyncClient, hub_on):
    r = await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(), "check": CHECK, "readme": "x"})   # no own tool "supabase"
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["error"] == "manifest_invalid" and d["field"] == "uses[1]" and "register it first" in d["rule"]


async def test_script_road_needs_the_script_body(clients: AsyncClient, hub_on):
    await _own_supabase(clients)
    r = await clients.post("/hub/tools", json={
        "manifest": _script_manifest(), "check": {"inputs": {}, "fields": ["rows"]}, "readme": "x"})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "script"
    r = await clients.post("/hub/tools", json={
        "manifest": _script_manifest(), "script": "export default async function run(ctx) { return {rows: [], count: 0}; }",
        "check": {"inputs": {}, "fields": ["rows"]}, "readme": "x"})
    assert r.status_code == 201 and r.json()["kind"] == "script"


async def test_viewer_cannot_publish(clients: AsyncClient, hub_on, monkeypatch):
    from sqlalchemy import select, update
    from treg.infra.db import session_maker
    from treg.models import Membership
    async with session_maker() as s:
        await s.execute(update(Membership).values(role="viewer"))
        await s.commit()
    r = await clients.post("/hub/tools", json={"manifest": _steps_manifest(), "check": CHECK, "readme": "x"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------------------------
# The call road: an own tool wins, a catalog id wins, then the hub; flag off ⇒ 404 as today

async def _publish_live(clients: AsyncClient, name="leads-db") -> str:
    await _own_supabase(clients)
    r = await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(name=name), "check": CHECK, "readme": "x"})
    tool_id = r.json()["tool_id"]
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import HubTool
    async with session_maker() as s:                # the check run is phase 4; flip by hand here
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="live"))
        await s.commit()
    return tool_id


async def test_hub_id_answers_501_until_the_runner_exists(clients: AsyncClient, hub_on):
    tool_id = await _publish_live(clients)
    r = await clients.get(f"/call/{tool_id}")
    assert r.status_code == 501, r.text
    assert r.json()["detail"]["error"] == "hub_not_runnable"
    assert r.json()["detail"]["tool_id"] == tool_id
    assert r.headers.get("x-treg-error") == "1"


async def test_unchecked_version_is_not_on_the_call_road(clients: AsyncClient, hub_on):
    await _own_supabase(clients)
    tool_id = (await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(), "check": CHECK, "readme": "x"})).json()["tool_id"]
    assert (await clients.get(f"/call/{tool_id}")).status_code == 404


async def test_flag_off_the_call_road_is_exactly_as_today(clients: AsyncClient, hub_on, monkeypatch):
    tool_id = await _publish_live(clients)
    monkeypatch.setattr(get_settings(), "hub_enabled", False)
    assert (await clients.get(f"/call/{tool_id}")).status_code == 404


async def test_an_own_tool_named_like_a_hub_id_wins(clients: AsyncClient, hub_on):
    tool_id = await _publish_live(clients)
    sid = (await clients.post("/secrets", json={"name": "k", "value": "K"})).json()["id"]
    await clients.post("/tools", json={"name": tool_id, "base_url": "http://upstream", "secret_id": sid})
    r = await clients.get(f"/call/{tool_id}")
    assert r.status_code != 501          # resolved as the own tool, never reached the hub


async def test_a_catalog_id_is_never_a_hub_tool(clients: AsyncClient, hub_on):
    # Even if a row carried a catalog id, the catalog branch runs first and the hub is never asked.
    from treg.application import hub as hub_app
    from treg.infra.db import session_maker
    assert hub_app.is_hub_id_shape(EP)
    async with session_maker() as s:
        assert await hub_app.tool_for(s, EP) is None
    r = await clients.get(f"/call/{EP}")
    assert r.status_code != 501
