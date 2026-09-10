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
from tests.test_marketplace_call import platform_on  # noqa: F401 — tier 4 on, so catalog steps resolve

EP = "tikhub.tiktok.video.comments"      # a real catalog id

from treg.application.call import service as call_service


def _fake_relay(status: int, body: bytes):
    from tests.test_marketplace_call import _fake_relay as real
    return real(status, body)


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
    """Through the ENVIRONMENT, like platform_on: that fixture clears the settings cache, and a
    value patched onto the old settings object would vanish with it."""
    monkeypatch.setenv("TREG_HUB_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _own_supabase(clients: AsyncClient) -> None:
    sid = (await clients.post("/secrets", json={"name": "sb-key", "value": "SB"})).json()["id"]
    r = await clients.post("/tools", json={"name": "supabase", "base_url": "https://x.supabase.co", "secret_id": sid})
    assert r.status_code in (200, 201), r.text


async def test_hub_routes_do_not_exist_with_the_flag_off(clients: AsyncClient):
    r = await clients.post("/hub/tools", json={"manifest": {}, "check": {}, "readme": "x"})
    assert r.status_code == 404
    assert (await clients.get("/hub/tools/mine")).status_code == 404


async def test_publish_stores_unchecked_and_reads_back(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "figma.com"}, "body": [1]}'))
    await _own_supabase(clients)
    r = await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(), "check": CHECK, "readme": "Leads from my table."})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1 and body["status"] == "live" and body["kind"] == "steps"
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


async def test_a_script_tool_runs_in_the_sandbox(clients: AsyncClient, hub_on):
    await _own_supabase(clients)
    tool_id = (await clients.post("/hub/tools", json={
        "manifest": _script_manifest(), "script": "export default async function run(ctx) { return {rows: [ctx.inputs.search], count: ctx.inputs.limit}; }",
        "check": {"inputs": {}, "fields": ["rows"]}, "readme": "x"})).json()["tool_id"]
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import HubTool
    async with session_maker() as s:
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="live"))
        await s.commit()
    r = await clients.post(f"/call/{tool_id}", json={"search": "figma", "limit": 500})   # limit clamps to max 100
    assert r.status_code == 200, r.text
    assert r.json()["output"] == {"rows": ["figma"], "count": 100}
    assert r.headers["X-Treg-Steps"] == "0" and r.headers["X-Treg-Cost-Micro"] == "0"


async def test_a_steps_tool_refuses_a_missing_required_input_by_name(clients: AsyncClient, hub_on):
    tool_id = await _publish_live(clients)
    r = await clients.post(f"/call/{tool_id}", json={})          # `domain` has no default
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "hub_input_invalid", "field": "domain", "rule": "required (it has no default)"}
    assert r.headers.get("x-treg-error") == "1"


async def test_unchecked_version_is_not_on_the_call_road(clients: AsyncClient, hub_on):
    await _own_supabase(clients)
    tool_id = (await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(), "check": CHECK, "readme": "x"})).json()["tool_id"]
    assert (await clients.get(f"/call/{tool_id}")).status_code == 404


async def test_flag_off_the_call_road_is_exactly_as_today(clients: AsyncClient, hub_on, monkeypatch):
    tool_id = await _publish_live(clients)
    monkeypatch.setenv("TREG_HUB_ENABLED", "0")
    get_settings.cache_clear()
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



# ---------------------------------------------------------------------------------------------
# The reference language and the graph (phase 2), pure

from treg.domain.hub import graph as hub_graph
from treg.domain.hub import refs


def test_refs_parse_every_form_and_nothing_else():
    assert refs.parse("$input.domain").root == "input"
    assert refs.parse("$company.data.domain").path == ("data", "domain")
    assert refs.parse("$news.results[0].title").path == ("results", 0, "title")
    assert refs.parse("$verify[]").path == (None,)
    assert refs.parse("$verify.length").path == ("length",)
    assert refs.parse("$0.data").is_positional
    for bad in ("company.data", "$", "$Company", "$a.b c", "$a[x]"):
        with pytest.raises(refs.RefError):
            refs.parse(bad)


def test_refs_read_and_resolve():
    scope = {"input": {"domain": "figma.com"},
             "company": {"data": {"name": "Figma", "employee_count": 1200}},
             "news": {"results": [{"title": "Funding"}, {"title": "Launch"}]},
             "verify": [{"status": "valid"}, {"status": "invalid"}, None]}
    pos = {"0": "company"}
    assert refs.resolve("$company.data.name", scope, pos) == "Figma"
    assert refs.resolve("$news.results[0].title", scope, pos) == "Funding"
    assert refs.resolve("$news.results[9].title", scope, pos) is None        # missing is data
    assert refs.resolve("$verify[]", scope, pos) == scope["verify"]
    assert refs.resolve("$verify.length", scope, pos) == 3
    assert refs.resolve("$0.data.employee_count", scope, pos) == 1200
    assert refs.resolve("$company.data.name: $verify.length leads", scope, pos) == "Figma: 3 leads"
    assert refs.resolve({"q": "$input.domain funding", "n": 3}, scope, pos) == {"q": "figma.com funding", "n": 3}
    with pytest.raises(refs.RefError):
        refs.resolve("$nobody.x", scope, pos)


def _g(*steps):
    return hub_graph.build([{"name": n, "call": "c", "input": i} for n, i in steps])


def test_graph_examples_from_the_decisions():
    # four needs nobody; two needs one; three needs two ⇒ one+four together, then two, then three
    g = _g(("one", {}), ("two", {"a": "$one.x"}), ("three", {"b": "$two.y"}), ("four", {}))
    assert g.wave == {"one": 0, "four": 0, "two": 1, "three": 2}
    # three needs one and two; four needs nobody ⇒ one, two, four together; three after both
    g = _g(("one", {}), ("two", {}), ("three", {"a": "$one.x", "b": "$two.y"}), ("four", {}))
    assert g.wave == {"one": 0, "two": 0, "four": 0, "three": 1}
    assert g.parents["three"] == frozenset({"one", "two"})
    assert g.positions == {"0": "one", "1": "two", "2": "three", "3": "four"}


def test_graph_refuses_cycles_and_unknown_steps():
    with pytest.raises(ManifestError) as e:
        _g(("a", {"x": "$b.y"}), ("b", {"x": "$a.y"}))
    assert "cycle" in e.value.rule
    with pytest.raises(ManifestError) as e:
        _g(("a", {"x": "$ghost.y"}))
    assert "$ghost" in e.value.rule
    with pytest.raises(ManifestError) as e:
        _g(("a", {"x": "$a.y"}))
    assert "itself" in e.value.rule


def test_a_cycle_is_refused_at_publish():
    m = _steps_manifest(steps=[
        {"name": "people", "call": EP, "input": {"aweme_id": "$rows.body"}},
        {"name": "rows", "call": "supabase/rest/v1/x", "input": {"q": "$people.data"}},
    ])
    err = _refused(m)
    assert "cycle" in err.rule


def test_own_tool_step_takes_a_path_and_a_method():
    m = _steps_manifest(steps=[
        {"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}},
        {"name": "rows", "call": "supabase/rest/v1/leads", "method": "POST", "input": {"q": "$people.data"}},
    ])
    v = validate(m, catalog_ids=CATALOG, own_tools=OWN)
    assert v.steps[1]["call"] == "supabase/rest/v1/leads" and v.steps[1]["method"] == "POST"
    err = _refused(_steps_manifest(steps=[{"name": "a", "call": EP + "/extra", "input": {}}]))
    assert err.field == "steps[0].call" and "takes no path" in err.rule


# ---------------------------------------------------------------------------------------------
# Phase 4: the check run, versions, the dry run, the team limiter

async def test_publish_runs_the_check_and_goes_live_on_pass(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """The maker's own balance pays the check's steps; the answer carries the verdict and the call line."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "figma.com"}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data", "count": "$people.data.length"})
    r = await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "x"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "live" and body["check"]["status"] == "passed" and body["check"]["run_id"]
    assert body["call"] == f"POST /call/{body['tool_id']}"
    assert (await clients.get(f"/hub/tools/{body['tool_id']}")).json()["check_result"]["status"] == "passed"
    # and it is on the call road now, without any hand flip
    r2 = await clients.post(f"/call/{body['tool_id']}", json={"domain": "figma.com"})
    assert r2.status_code == 200 and r2.json()["output"]["leads"] == {"domain": "figma.com"}


async def test_publish_keeps_a_failed_version_with_the_reason(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b'{"error": "down"}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"})
    r = await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "x"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "failed" and body["check"]["status"] == "failed"
    assert body["check"]["error"]["error"] == "hub_step_failed" and body["check"]["error"]["step"] == "people"
    assert "call" not in body
    # a failed version is never on the call road
    assert (await clients.post(f"/call/{body['tool_id']}", json={"domain": "x"})).status_code == 404


async def test_a_check_that_needs_a_missing_field_fails_honestly(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data.rows"})
    r = await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "x"})
    body = r.json()
    assert body["status"] == "failed" and body["check"]["error"]["missing"] == ["leads"]


async def test_put_publishes_a_new_version_and_pins_the_old_one(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "v"}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"})
    tool_id = (await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "v1"})).json()["tool_id"]
    r = await clients.put(f"/hub/tools/{tool_id}", json={"manifest": {**m, "summary": "second"}, "check": CHECK, "readme": "v2"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["status"] == "live"
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).json()["recipe"] == f"{tool_id}@2"
    assert (await clients.post(f"/call/{tool_id}@1", json={"domain": "x"})).json()["recipe"] == f"{tool_id}@1"
    # a name that does not match the id is refused by field and rule
    bad = await clients.put(f"/hub/tools/{tool_id}", json={"manifest": {**m, "name": "other"}, "check": CHECK, "readme": "x"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "name"
    # PUT on a tool the team does not have
    assert (await clients.put("/hub/tools/x.nope", json={"manifest": {**m, "name": "nope"}, "check": CHECK, "readme": "x"})).status_code == 404


async def test_an_old_version_stops_serving_thirty_days_after_a_newer_one(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "v"}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"})
    tool_id = (await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "v1"})).json()["tool_id"]
    await clients.put(f"/hub/tools/{tool_id}", json={"manifest": m, "check": CHECK, "readme": "v2"})
    from datetime import timedelta
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import HubTool
    from sqlalchemy import select as _select
    async with session_maker() as s:                 # v2 landed 31 days ago
        v2 = (await s.execute(_select(HubTool).where(HubTool.tool_id == tool_id, HubTool.version == 2))).scalars().one()
        v2.created_at = v2.created_at - timedelta(days=31)
        s.add(v2)
        await s.commit()
    assert (await clients.post(f"/call/{tool_id}@1", json={"domain": "x"})).status_code == 404
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 200


async def test_a_dry_run_from_a_folder_runs_but_stores_nothing(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "dry"}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"})
    r = await clients.post("/hub/run", json={"manifest": m, "check": CHECK, "readme": "x", "inputs": {"domain": "figma.com"}})
    assert r.status_code == 200, r.text
    assert r.json()["output"] == {"leads": {"domain": "dry"}} and r.json()["recipe"].endswith("@0")
    assert (await clients.get("/hub/tools/mine")).json() == []
    bad = await clients.post("/hub/run", json={"manifest": {**m, "uses": ["ghost"]}, "check": CHECK, "readme": "x", "inputs": {}})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "uses[0]"


async def test_the_fifth_concurrent_run_is_refused_with_a_retry_time(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    import asyncio
    from treg.application.hub import limits
    tool_id = await _publish_live(clients)
    gate = asyncio.Event()

    async def slow_relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        await gate.wait()
        from tests.test_marketplace_call import _fake_relay as real
        return await real(200, b'{"data": {"domain": "x"}}')(request, upstream_url, tool, secrets, client)
    monkeypatch.setattr(call_service, "relay", slow_relay)
    m = {"domain": "figma.com"}
    tasks = [asyncio.create_task(clients.post(f"/call/{tool_id}", json=m)) for _ in range(4)]
    for _ in range(50):                               # let the four take their slots
        await asyncio.sleep(0.01)
        if limits.active(1) >= 4 or any(limits.active(o) >= 4 for o in list(limits._active)):
            break
    fifth = await clients.post(f"/call/{tool_id}", json=m)
    assert fifth.status_code == 429, fifth.text
    assert fifth.json()["detail"]["error"] == "hub_busy" and fifth.json()["detail"]["retry_after_s"] == 5
    gate.set()
    done = await asyncio.gather(*tasks)
    assert all(r.status_code in (200, 424) for r in done)
    assert not limits._active                         # every slot given back


# ---------------------------------------------------------------------------------------------
# Phase 7.1: the agent-facing files and catalog_get on a hub id

async def test_catalog_get_answers_for_a_hub_id_and_hides_what_the_maker_hides(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "figma.com"}, "body": [1]}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"}, price_usd=0.02)
    tool_id = (await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "About it."})).json()["tool_id"]
    r = await clients.get(f"/catalog/endpoints/{tool_id}")
    assert r.status_code == 200, r.text
    e = r.json()["endpoint"]
    assert e["kind"] == "hub" and e["id"] == tool_id and e["version"] == 1 and e["status"] == "live"
    assert e["cost"]["usd"] == 0.02 and e["price_line"].startswith("seller $0.02")
    assert e["inputs"]["domain"]["example"] == "figma.com" and e["output"] == {"leads": "$people.data"}
    assert e["call_template"]["cli"].startswith(f"treg call {tool_id} --data")
    assert e["page"].endswith(f"/hub/{tool_id}") and e["readme"] == "About it."
    text = r.text
    assert "supabase" not in text and "tikhub" not in text          # the maker's tools are not on the contract
    assert "script" not in e or e.get("script") is None
    # not in search, only by id
    s = await clients.get("/catalog/search", params={"q": "leads-db"})
    assert tool_id not in s.text
    # the same through the MCP-shaped miss path: an unknown hub-shaped id is still a 404 with hints
    assert (await clients.get("/catalog/endpoints/nobody.nothing")).status_code == 404


async def test_catalog_get_on_a_hub_id_with_the_flag_off_is_a_plain_404(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_live(clients)
    monkeypatch.setenv("TREG_HUB_ENABLED", "0")
    get_settings.cache_clear()
    r = await clients.get(f"/catalog/endpoints/{tool_id}")
    assert r.status_code == 404 and "hub" not in r.text


async def test_the_agent_files_mention_the_hub_only_when_it_is_on(clients: AsyncClient, monkeypatch):
    for on in ("1", "0"):
        monkeypatch.setenv("TREG_HUB_ENABLED", on)
        get_settings.cache_clear()
        llms = (await clients.get("/llms.txt")).text
        skill = (await clients.get("/skill.md")).text
        for body in (llms, skill):
            assert "<!--hub-->" not in body and "<!--/hub-->" not in body     # markers never leak
            assert ("treg hub init" in body) is (on == "1")
            assert ("hub_create" in body) is (on == "1")
        if on == "1":
            assert "never paste a credential" in llms.lower() or "never paste a credential" in skill.lower()
    get_settings.cache_clear()



# ---------------------------------------------------------------------------------------------
# Phase 7.2: the public share page

async def _live_tool_with_readme(clients, monkeypatch, price=0.01):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "figma.com"}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"}, price_usd=price)
    r = await clients.post("/hub/tools", json={"manifest": m, "check": CHECK,
                                               "readme": "# Leads\n\nWhat it **returns**, with `code`.\n\n- one\n- two\n\n<script>alert(1)</script>"})
    assert r.status_code == 201 and r.json()["status"] == "live", r.text
    return r.json()


async def test_the_public_page_shows_the_contract_and_hides_the_makers_side(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch)
    tool_id = pub["tool_id"]
    assert pub["page"].endswith(f"/hub/{tool_id}")
    r = await clients.get(f"/hub/{tool_id}")            # no token: readable without sign-in
    assert r.status_code == 200, r.text
    html = r.text
    assert "leads-db" in html and "seller $0.01 + steps" in html and "$10.00 per 1,000 runs" in html
    assert f"treg call {tool_id} --data" in html and f"/call/{tool_id}" in html
    assert "<b>returns</b>" in html and "<code>code</code>" in html and "<li>one</li>" in html   # the readme, rendered
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html                  # and escaped
    assert '<meta name="robots" content="noindex"/>' in html
    assert "supabase" not in html and "tikhub" not in html and "SUPABASE" not in html          # the maker's tools and keys
    assert "made of 2 tool(s)" in html
    assert "run at publish" in html and "passed" in html                                          # the check trace


async def test_the_public_page_has_a_markdown_twin(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = (await _live_tool_with_readme(clients, monkeypatch))["tool_id"]
    r = await clients.get(f"/hub/{tool_id}.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.headers.get("x-robots-tag") == "noindex"
    assert r.text.startswith("# leads-db") and "## Call it" in r.text and "| domain |" in r.text
    assert "supabase" not in r.text


async def test_the_public_page_is_404_when_off_failed_or_unknown(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = (await _live_tool_with_readme(clients, monkeypatch))["tool_id"]
    assert (await clients.get("/hub/nobody.nothing")).status_code == 404
    # a failed version alone is not a page
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b'{}'))
    m = _steps_manifest(name="broken", steps=[{"name": "people", "call": EP, "input": {"aweme_id": "x"}}], output={"leads": "$people.data"})
    broken = (await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "x"})).json()["tool_id"]
    assert (await clients.get(f"/hub/{broken}")).status_code == 404
    monkeypatch.setenv("TREG_HUB_ENABLED", "0"); get_settings.cache_clear()
    assert (await clients.get(f"/hub/{tool_id}")).status_code == 404


async def test_the_public_page_serves_a_pinned_older_version(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch)
    tool_id = pub["tool_id"]
    m = _steps_manifest(summary="second", steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}], output={"leads": "$people.data"})
    await clients.put(f"/hub/tools/{tool_id}", json={"manifest": m, "check": CHECK, "readme": "v2"})
    assert "second" in (await clients.get(f"/hub/{tool_id}")).text
    assert "v1" in (await clients.get(f"/hub/{tool_id}@1")).text and "second" not in (await clients.get(f"/hub/{tool_id}@1")).text
    assert f"{tool_id}@1</code> until" in (await clients.get(f"/hub/{tool_id}")).text
