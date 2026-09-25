"""The tool hub, phase 1: the manifest validator, the table, the publish route, and where a hub
id sits on the call road (behind an own tool and a catalog id, and behind TREG_HUB_ENABLED).

Nothing runs yet. A published tool is stored `unchecked`; the call road answers 501 for it when
the flag is on and 404 (exactly as today) when the flag is off.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from conftest import funded_user

from treg.config import get_settings
from treg.domain.hub import ManifestError, price_label, validate, validate_check
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


# ---------------------------------------------------------------------------------------------
# Pricing: one rule per kind (docs/hub-pricing-decisions.md round 4, 2026-09-24). A steps recipe
# has one fixed price; a script declares `max_price_usd` and prices itself with ctx.charge.

def _no_price(m):
    m.pop("price_usd", None)
    return m


def test_a_recipe_prices_with_one_fixed_price():
    v = validate(_no_price(_steps_manifest(pricing={"price_usd": 0.03})), catalog_ids=CATALOG, own_tools=OWN)
    assert v.price_micro == 30_000
    assert v.pricing == {"mode": "per_call", "price_micro": 30_000, "max_charge_micro": 0}
    assert v.manifest["pricing"] == {"mode": "per_call", "price_usd": 0.03}


def test_a_top_level_price_usd_still_prices_a_recipe():
    v = validate(_steps_manifest(price_usd=0.02), catalog_ids=CATALOG, own_tools=OWN)
    assert v.price_micro == 20_000 and v.pricing["mode"] == "per_call"
    assert v.manifest["pricing"] == {"mode": "per_call", "price_usd": 0.02}


def test_a_script_declares_the_cap_on_its_charges():
    v = validate(_script_manifest(pricing={"max_price_usd": 0.05}), catalog_ids=CATALOG, own_tools=OWN)
    assert v.price_micro == 0
    assert v.pricing == {"mode": "charge", "price_micro": 0, "max_charge_micro": 50_000}
    assert v.manifest["pricing"] == {"mode": "charge", "max_price_usd": 0.05}
    assert "price_usd" not in v.manifest


def test_a_script_with_no_pricing_is_free():
    v = validate(_script_manifest(), catalog_ids=CATALOG, own_tools=OWN)
    assert v.pricing == {"mode": "charge", "price_micro": 0, "max_charge_micro": 0}
    assert price_label(v.manifest) == "free"


def test_a_stored_manifest_validates_again_unchanged():
    """A manifest read back from the database carries its `mode`; validating it again is a no-op."""
    for m in (_script_manifest(pricing={"max_price_usd": 0.05}), _no_price(_steps_manifest(pricing={"price_usd": 0.03}))):
        once = validate(m, catalog_ids=CATALOG, own_tools=OWN).manifest
        assert validate(dict(once, name="leads-db"), catalog_ids=CATALOG, own_tools=OWN).manifest == once


@pytest.mark.parametrize("m,field,words", [
    (_steps_manifest(pricing={"price_usd": 0.01}), "price_usd", "not a top-level"),
    (_no_price(_steps_manifest(pricing={"mode": "percent", "percent": 30})), "pricing.mode", "only `price_usd`"),
    (_no_price(_steps_manifest(pricing={"max_price_usd": 0.1})), "pricing.max_price_usd", "needs a script"),
    (_no_price(_steps_manifest(pricing={"price_usd": 500})), "pricing.price_usd", "0 to 100"),
    (_script_manifest(price_usd=0.01), "pricing.price_usd", "ctx.charge"),
    (_script_manifest(pricing={"price_usd": 0.01}), "pricing.price_usd", "ctx.charge"),
    (_script_manifest(pricing={"max_price_usd": 0.1, "per_result_usd": 0.001}), "pricing.per_result_usd", "only `max_price_usd`"),
    (_script_manifest(pricing={"max_price_usd": 0}), "pricing.max_price_usd", "above 0"),
    (_script_manifest(pricing=5), "pricing", "an object"),
])
def test_pricing_refusals(m, field, words):
    err = _refused(m)
    assert err.field == field, (err.field, err.rule)
    assert words in err.rule


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
    # cost.usd is what a run has cost (the check: $0.001 of steps + the $0.02 price), never the price alone
    assert e["cost"]["usd"] == 0.021 and e["price_line"].startswith("seller $0.02") and e["price_samples"] >= 1
    assert e["inputs"]["domain"]["example"] == "figma.com" and e["output"] == {"leads": "$people.data"}
    assert e["call_template"]["cli"].startswith(f"treg call {tool_id} --data")
    assert e["page"].endswith(f"/hub/{tool_id}") and e["readme"] == "About it."
    text = r.text
    # the maker's own server is disclosed by host (a caller's inputs reach it); the tool names are not
    assert e["sends_inputs_to"] == ["x.supabase.co"]
    text = text.replace("x.supabase.co", "")
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
    assert "leads-db" in html and "seller $0.01 + provider fees" in html and "$10.00 per 1,000 runs" in html
    assert f"treg call {tool_id} --data" in html and f"/call/{tool_id}" in html
    assert "<b>returns</b>" in html and "<code>code</code>" in html and "<li>one</li>" in html   # the readme, rendered
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html                  # and escaped
    assert '<meta name="robots" content="noindex"/>' in html
    assert "sends your inputs to the maker&#39;s own server at <b>x.supabase.co</b>" in html   # disclosed by host
    html = html.replace("x.supabase.co", "")
    assert "supabase" not in html and "tikhub" not in html and "SUPABASE" not in html          # the maker's tools and keys
    assert "made of 2 tool(s)" in html
    assert "run at publish" in html and "passed" in html                                          # the check trace


async def test_the_public_page_has_a_markdown_twin(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = (await _live_tool_with_readme(clients, monkeypatch))["tool_id"]
    r = await clients.get(f"/hub/{tool_id}.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.headers.get("x-robots-tag") == "noindex"
    assert r.text.startswith("# leads-db") and "## Call it" in r.text and "| domain |" in r.text
    assert "sends your inputs to the maker's own server at x.supabase.co" in r.text
    assert "supabase" not in r.text.replace("x.supabase.co", "")


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


# ---------------------------------------------------------------------------------------------
# Phase 7.3: health, the scheduled check, retire, price

async def test_retire_takes_every_version_off_the_call_road_and_keeps_history(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch)
    tool_id = pub["tool_id"]
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 200
    r = await clients.delete(f"/hub/tools/{tool_id}")
    assert r.status_code == 200 and r.json() == {"tool_id": tool_id, "status": "retired", "versions": 1}
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 404
    assert (await clients.get(f"/hub/{tool_id}")).status_code == 404
    mine = (await clients.get("/hub/tools/mine")).json()
    assert mine[0]["status"] == "retired"
    assert (await clients.get(f"/hub/tools/{tool_id}/earnings")).status_code == 200      # history readable
    assert (await clients.delete(f"/hub/tools/{tool_id}")).status_code == 404            # already retired


async def test_price_edit_applies_to_later_runs_without_a_version_bump(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 0.05})
    assert r.status_code == 200 and r.json() == {"tool_id": tool_id, "version": 1, "price_label": "$0.05 a run",
                                                 "pricing": {"mode": "per_call", "price_usd": 0.05}}
    one = (await clients.get(f"/hub/tools/{tool_id}")).json()
    assert one["version"] == 1 and one["price_usd"] == 0.05
    assert "seller $0.05" in (await clients.get(f"/hub/{tool_id}")).text
    bad = await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 500})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "price_usd"
    # a stranger pays the new price
    token = (await funded_user(clients, "buyer@example.com"))["token"]
    run = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={"X-Treg-Token": token})
    assert run.status_code == 200 and run.json()["usage"]["price_micro"] == 50_000


async def test_health_is_failing_after_three_failed_runs_and_clears_on_a_pass(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch)
    tool_id = pub["tool_id"]
    h = (await clients.get(f"/hub/tools/{tool_id}/health")).json()
    assert h["health"] == "ok" and h["fails_in_a_row"] == 0 and h["runs"][0]["kind"] == "maker's run"
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b'{}'))
    for _ in range(3):
        assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 424
    h = (await clients.get(f"/hub/tools/{tool_id}/health")).json()
    assert h["health"] == "failing" and h["fails_in_a_row"] == 3
    assert (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["endpoint"]["health"] == "failing"
    assert "failing (the last 3 runs failed)" in (await clients.get(f"/hub/{tool_id}")).text
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 424    # still callable, still failing
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "back"}}'))
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 200
    assert (await clients.get(f"/hub/tools/{tool_id}/health")).json()["health"] == "ok"


async def test_the_scheduled_check_runs_as_the_maker_and_records_its_verdict(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    from treg.application.hub.health import CHECK_EMAIL, check_as_maker
    from treg.api import app
    from treg.infra.db import session_maker
    from treg.models import HubRun, HubTool
    from sqlalchemy import select
    pub = await _live_tool_with_readme(clients, monkeypatch)
    tool_id = pub["tool_id"]
    balance_before = (await clients.get(f"/orgs/{(await clients.get('/orgs')).json()[0]['org_id']}/balance")).json()["balance_micro"]
    async with session_maker() as s:
        row = (await s.execute(select(HubTool).where(HubTool.tool_id == tool_id))).scalars().one()
        v = await check_as_maker(s, row, app.state.http)
        await s.commit()
    assert v["status"] == "passed" and v["scheduled"] is True and v["run_id"]
    async with session_maker() as s:
        runs = (await s.execute(select(HubRun).where(HubRun.run_id == v["run_id"]))).scalars().all()
    assert len(runs) == 1 and runs[0].caller_email == CHECK_EMAIL and runs[0].price_micro == 0
    h = (await clients.get(f"/hub/tools/{tool_id}/health")).json()
    assert h["last_check"]["scheduled"] is True and h["runs"][0]["kind"] == "scheduled check"
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    assert (await clients.get(f"/orgs/{org}/balance")).json()["balance_micro"] == balance_before - 1000   # one metered step, no price
    # a failing check keeps the tool live and says so
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b'{}'))
    async with session_maker() as s:
        row = (await s.execute(select(HubTool).where(HubTool.tool_id == tool_id))).scalars().one()
        v = await check_as_maker(s, row, app.state.http)
        await s.commit()
    assert v["status"] == "failed"
    assert (await clients.get(f"/hub/tools/{tool_id}")).json()["status"] == "live"


async def test_the_worker_hub_check_walks_every_live_tool(clients: AsyncClient, hub_on, platform_on, monkeypatch, capsys):
    import argparse
    from treg import worker
    from treg.config import get_settings
    pub = await _live_tool_with_readme(clients, monkeypatch)
    # _hub_check runs the worker's startup guard verify_db(), which refuses a non-SQLite database
    # with no TREG_SECRET_KEY. A real worker always has the key; give it one here.
    monkeypatch.setenv("TREG_SECRET_KEY", "HCmUPIPieol_HNAh92Q6qgmJp85kHPMms_QUwRJMNMc=")
    get_settings.cache_clear()
    # `--only` this tool: the check walks EVERY live tool in the database, and on a shared
    # database (Postgres, serial) earlier test files leave their own live tools behind, so the
    # test must name its tool rather than assume it is the only one.
    rc = await worker._hub_check(argparse.Namespace(only=pub["tool_id"], json=True))
    out = capsys.readouterr().out
    import json as _json
    rows = _json.loads(out)
    assert rc == 0 and [r["tool_id"] for r in rows] == [pub["tool_id"]] and rows[0]["check"] == "passed"


# ---------------------------------------------------------------------------------------------
# Phase 7.4: the dashboard's data: the run record, the maker's list, the SPA path

async def test_a_run_is_readable_by_its_caller_and_its_maker_with_different_views(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    token = (await funded_user(clients, "reader@example.com"))["token"]
    h = {"X-Treg-Token": token}
    run = await clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=h)
    run_id = run.json()["run_id"]
    # the caller: output, what they paid, the trace; never the script's log
    mine = (await clients.get(f"/hub/runs/{run_id}", headers=h)).json()
    assert mine["you_are"] == "caller" and mine["output"] == {"leads": {"domain": "figma.com"}}
    assert mine["usage"] == {"cost_micro": 1000 + 10_000, "steps_micro": 1000, "price_micro": 10_000}
    assert "log" not in mine and mine["kind"] == "caller's run"
    # the maker: the log and the error, never the output or the caller's identity
    theirs = (await clients.get(f"/hub/runs/{run_id}")).json()
    assert theirs["you_are"] == "maker" and "output" not in theirs and "caller_email" not in theirs and "log" in theirs
    # a third team: 404
    other = (await funded_user(clients, "third@example.com"))["token"]
    assert (await clients.get(f"/hub/runs/{run_id}", headers={"X-Treg-Token": other})).status_code == 404
    # a failed run: the caller's error carries the step and status, not the upstream body; the maker gets it whole
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b'{"vendor": "secret error body"}'))
    bad = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=h)
    bad_id = bad.json()["detail"]["run_id"]
    c = (await clients.get(f"/hub/runs/{bad_id}", headers=h)).json()
    assert c["error"]["step"] == "people" and "secret error body" not in json.dumps(c)
    m = (await clients.get(f"/hub/runs/{bad_id}")).json()
    assert "secret error body" in json.dumps(m["error"])
    # the SPA path for the run page answers with the app
    page = await clients.get(f"/app/runs/{run_id}")
    assert page.status_code == 200 and "shared run" in page.text


async def test_the_makers_list_carries_health_and_thirty_day_numbers(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.02)
    token = (await funded_user(clients, "buyer2@example.com"))["token"]
    for _ in range(2):
        assert (await clients.post(f"/call/{pub['tool_id']}", json={"domain": "x"}, headers={"X-Treg-Token": token})).status_code == 200
    mine = (await clients.get("/hub/tools/mine")).json()
    assert mine[0]["health"] == "ok" and mine[0]["runs_30d"] == 2 and mine[0]["earned_30d_micro"] == 40_000


# ---------------------------------------------------------------------------------------------
# Pricing in the run (round 4): a recipe's fixed price, a script's ctx.charge lines under its cap.
# The runner holds the price or the cap, settles the real amount, releases the rest.

async def _publish_priced(clients, monkeypatch, price_usd, *, n=5):
    """A live steps tool with a fixed price whose one catalog step returns a list of `n` items."""
    monkeypatch.setattr(call_service, "relay",
                        _fake_relay(200, ("{\"data\": [" + ",".join(["1"] * n) + "]}").encode()))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"rows": "$people.data"}, pricing={"price_usd": price_usd})
    m.pop("price_usd", None)
    r = await clients.post("/hub/tools", json={"manifest": m, "readme": "x",
                                               "check": {"inputs": {"domain": "figma.com"}, "fields": ["rows"]}})
    assert r.status_code == 201 and r.json()["status"] == "live", r.text
    return r.json()["tool_id"]


# One catalog call returning a list; the price is per item, a margin on the call's cost, or both.
PER_ITEM = ("export default async function run(ctx) {"
            "  const r = await ctx.call('" + EP + "', {query: {aweme_id: ctx.inputs.search || 'x'}});"
            "  if (r.status !== 200) throw new Error('step failed ' + r.status);"
            "  const rows = r.json.data;"
            "  ctx.charge(rows.length * 0.002, 'per row');"
            "  return { rows, count: rows.length };"
            "}")
MARGIN = PER_ITEM.replace("ctx.charge(rows.length * 0.002, 'per row');", "ctx.charge(r.cost_usd * 0.5, '50% margin');")


async def _publish_script_priced(clients, monkeypatch, max_price_usd, script, *, n=5):
    """A live script tool over one catalog step (a $0.001 call returning `n` items)."""
    monkeypatch.setattr(call_service, "relay",
                        _fake_relay(200, ("{\"data\": [" + ",".join(["1"] * n) + "]}").encode()))
    m = _script_manifest(uses=[EP], pricing={"max_price_usd": max_price_usd})
    r = await clients.post("/hub/tools", json={"manifest": m, "script": script, "readme": "x",
                                               "check": {"inputs": {}, "fields": ["rows"]}})
    assert r.status_code == 201 and r.json()["status"] == "live", r.text
    return r.json()["tool_id"]


async def test_a_script_charges_per_result(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    token = (await funded_user(clients, "b1@example.com"))["token"]
    run = await clients.post(f"/call/{tool_id}", json={}, headers={"X-Treg-Token": token})
    assert run.status_code == 200, run.text
    assert run.json()["usage"]["price_micro"] == 10_000        # 5 rows x $0.002
    assert run.json()["usage"]["cost_micro"] == 11_000         # + one $0.001 step


async def test_a_script_charges_a_margin_on_the_call_cost(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, MARGIN, n=3)
    token = (await funded_user(clients, "b4@example.com"))["token"]
    run = await clients.post(f"/call/{tool_id}", json={}, headers={"X-Treg-Token": token})
    assert run.json()["usage"]["steps_micro"] == 1_000
    assert run.json()["usage"]["price_micro"] == 500          # 50% of $0.001


async def test_a_recipe_charges_its_fixed_price(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_priced(clients, monkeypatch, 0.02, n=5)
    token = (await funded_user(clients, "b2@example.com"))["token"]
    run = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={"X-Treg-Token": token})
    assert run.json()["usage"]["price_micro"] == 20_000 and run.json()["usage"]["cost_micro"] == 21_000


async def test_a_charged_run_moves_money_on_both_teams(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    seller_org = (await clients.get("/orgs")).json()[0]["org_id"]
    seller_before = (await clients.get(f"/orgs/{seller_org}/balance")).json()["balance_micro"]
    hdr = {"X-Treg-Token": (await funded_user(clients, "b5@example.com"))["token"]}
    buyer_org = (await clients.get("/orgs", headers=hdr)).json()[0]["org_id"]
    buyer_before = (await clients.get(f"/orgs/{buyer_org}/balance", headers=hdr)).json()["balance_micro"]
    await clients.post(f"/call/{tool_id}", json={}, headers=hdr)
    seller_after = (await clients.get(f"/orgs/{seller_org}/balance")).json()["balance_micro"]
    buyer_after = (await clients.get(f"/orgs/{buyer_org}/balance", headers=hdr)).json()["balance_micro"]
    assert seller_after - seller_before == 10_000             # the maker earns the charges, not the cap
    assert buyer_before - buyer_after == 11_000               # the buyer pays the charges plus one step


async def test_a_failed_charged_run_pays_the_seller_nothing(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    seller_org = (await clients.get("/orgs")).json()[0]["org_id"]
    seller_before = (await clients.get(f"/orgs/{seller_org}/balance")).json()["balance_micro"]
    hdr = {"X-Treg-Token": (await funded_user(clients, "b6@example.com"))["token"]}
    buyer_org = (await clients.get("/orgs", headers=hdr)).json()[0]["org_id"]
    buyer_before = (await clients.get(f"/orgs/{buyer_org}/balance", headers=hdr)).json()["balance_micro"]
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b'{"error": "down"}'))   # the step now fails
    run = await clients.post(f"/call/{tool_id}", json={}, headers=hdr)
    assert run.status_code == 424
    assert (await clients.get(f"/orgs/{seller_org}/balance")).json()["balance_micro"] == seller_before
    assert (await clients.get(f"/orgs/{buyer_org}/balance", headers=hdr)).json()["balance_micro"] == buyer_before


# ---------------------------------------------------------------------------------------------
# The surfaces that show the price.

async def test_the_public_page_shows_the_cap_before_any_run(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    page = (await clients.get(f"/hub/{tool_id}")).text
    assert "seller up to $0.5 a run" in page


async def test_the_mine_list_carries_the_price_label(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, MARGIN, n=3)
    row = [t for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id][0]
    assert row["pricing"] == {"mode": "charge", "max_price_usd": 0.5} and row["price_label"] == "up to $0.5 a run"


async def test_every_price_surface_leads_with_the_observed_range(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """2026-09-18: the headline price is what a run actually cost (steps + seller part) over recent
    successful runs, one number or a low–high range. Before any run, the declared price or cap."""
    from treg.domain.hub import range_label, seller_part_micro
    sc = {"uses": ["crustdata.companies.enrich"], "pricing": {"mode": "charge", "max_price_usd": 0.25}}
    assert range_label(sc, None, None) == "up to $0.25/run + provider fees"
    assert range_label({"uses": ["a.b"], "pricing": {"mode": "per_call", "price_usd": 0.01}}, None, None) == "$0.01/run + provider fees"
    assert range_label({"uses": ["own"], "pricing": {"mode": "per_call", "price_usd": 0.01}}, None, None) == "$0.01/run"
    assert range_label({"uses": ["a.b"], "pricing": {"mode": "per_call", "price_usd": 0}}, None, None) == "free + provider fees"
    assert range_label(sc, 750_000, 750_000) == "$0.75/run so far · seller up to $0.25 a run"
    assert range_label(sc, 750_000, 1_225_000) == "$0.75–$1.225/run so far · seller up to $0.25 a run"
    assert range_label({"uses": ["a.b"], "pricing": {"mode": "per_call", "price_usd": 0.01}}, 11_000, 11_000) == "$0.011/run"
    assert seller_part_micro(sc, None, 150_000) == 150_000          # the maker's own run: its charge lines
    assert seller_part_micro(sc, 20_000, 150_000) == 20_000         # a caller's run: what they paid
    assert seller_part_micro({"pricing": {"mode": "per_call", "price_usd": 0.01}}, None) == 10_000
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=3)
    row = [t for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id][0]
    # the publish check was the maker's own run: 3 rows charged $0.006 on top of the $0.001 step
    # only the maker's own check run so far: the figure says so
    assert row["price_samples"] >= 1 and row["price_range"] == "$0.007/run so far · seller up to $0.5 a run (from the maker's own tests)"
    assert row["price_from_tests"] is True
    page = (await clients.get(f"/hub/{tool_id}.md")).text
    assert f"**Price:** {row['price_range']}" in page
    got = (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["endpoint"]
    assert got["price_range"] == row["price_range"] and got["price_samples"] == row["price_samples"]


async def test_earnings_report_carries_the_average_price(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """9.4: avg_price_micro = earned / successful runs, per day and overall, and in the CSV."""
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    hdr = {"X-Treg-Token": (await funded_user(clients, "b7@example.com"))["token"]}
    for _ in range(2):                                             # two sales at 5 rows x $0.002
        assert (await clients.post(f"/call/{tool_id}", json={}, headers=hdr)).status_code == 200
    d = (await clients.get(f"/hub/tools/{tool_id}/earnings")).json()
    assert d["earned_micro"] == 20_000 and d["runs"] == 2 and d["avg_price_micro"] == 10_000
    assert d["by_day"][0]["ok"] == 2 and d["by_day"][0]["avg_price_micro"] == 10_000
    csv = (await clients.get(f"/hub/tools/{tool_id}/earnings", params={"format": "csv"})).text
    assert csv.splitlines()[0] == "day,runs,ok,failed,earned_usd,avg_price_usd"
    assert csv.splitlines()[1].endswith(",0.020000,0.010000")


# ---------------------------------------------------------------------------------------------
# Listing (docs/hub-listing-decisions.md round 2, 2026-09-24): `listed` is a request; a superadmin
# approves or rejects it; only an approved tool is in search; the approval outlives a version.

ADMIN = "hub-admin-token"


async def _decide(clients, monkeypatch, tool_id, decision, reason=""):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    return await clients.post(f"/admin/hub/listings/{tool_id}", json={"decision": decision, "reason": reason},
                              headers={"X-Treg-Token": ADMIN})


async def test_listed_is_a_request_and_the_switches_flip_without_a_version_bump(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    one = (await clients.get(f"/hub/tools/{tool_id}")).json()
    assert one["listed"] is False and one["listing"] == {"state": "none"} and one["public_log"] is True
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    assert r.status_code == 200 and r.json()["listed"] is False and r.json()["listing"]["state"] == "requested"
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"public_log": False})
    assert r.status_code == 200 and r.json() == {"tool_id": tool_id, "version": 1, "public_log": False}
    mine = [t for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id][0]
    assert mine["version"] == 1 and mine["listing"]["state"] == "requested" and mine["public_log"] is False
    # an empty body names the rule; another team's tool is 404, and it never sees the request
    assert (await clients.patch(f"/hub/tools/{tool_id}", json={})).status_code == 422
    token = (await funded_user(clients, "stranger@example.com"))["token"]
    assert (await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True},
                                headers={"X-Treg-Token": token})).status_code == 404
    assert "listing" not in (await clients.get(f"/hub/tools/{tool_id}", headers={"X-Treg-Token": token})).json()


Q_OWN_WORDS = "decision makers verified emails"     # the test tool's own summary words


async def _search_ids(clients):
    return [r["id"] for r in (await clients.get("/catalog/search", params={"q": Q_OWN_WORDS})).json()["results"]]


async def test_only_an_approved_tool_is_in_search(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    assert tool_id not in await _search_ids(clients)                                 # not listed
    assert (await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})).status_code == 200
    assert tool_id not in await _search_ids(clients)                                 # requested, not approved
    r = await _decide(clients, monkeypatch, tool_id, "approve")
    assert r.status_code == 200 and r.json()["listed"] is True
    body = (await clients.get("/catalog/search", params={"q": Q_OWN_WORDS})).json()
    row = [r for r in body["results"] if r["id"] == tool_id][0]
    assert row["kind"] == "hub" and row["provider"] and row["price_line"].startswith("seller ")
    assert row["cost"]["usd"] == 0.011 and row["price_range"] == "$0.011/run (from the maker's own tests)" and "score" in row and "script" not in row and "uses" not in row
    assert body["hints"][0] == f"treg catalog get {tool_id}   # params, cost and an example response" or tool_id in json.dumps(body["hints"])
    assert (await clients.delete(f"/hub/tools/{tool_id}")).status_code == 200      # retired never appears
    assert tool_id not in await _search_ids(clients)


async def test_a_rejection_carries_a_reason_and_listing_again_asks_again(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    assert (await _decide(clients, monkeypatch, tool_id, "reject")).status_code == 422      # no reason
    assert (await _decide(clients, monkeypatch, tool_id, "reject", "the summary does not say what it returns")).status_code == 200
    mine = [t for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id][0]
    assert mine["listing"]["state"] == "rejected" and mine["listing"]["reason"].startswith("the summary")
    queue = (await clients.get("/admin/hub/listings", params={"state": "rejected"}, headers={"X-Treg-Token": ADMIN})).json()
    assert [q["tool_id"] for q in queue] == [tool_id] and queue[0]["price_label"] == "$0.01 a run"
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    # asking again keeps the last reason, so the maker can still read what to fix
    assert r.json()["listing"] == {**r.json()["listing"], "state": "requested",
                                   "reason": "the summary does not say what it returns", "decided_at": None}
    queue = (await clients.get("/admin/hub/listings", headers={"X-Treg-Token": ADMIN})).json()
    assert [q["tool_id"] for q in queue] == [tool_id]
    # unlisting withdraws it: nothing left to decide
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": False})
    assert (await _decide(clients, monkeypatch, tool_id, "approve")).status_code == 404


async def _v2(clients, tool_id, summary="Decision makers of a company, with verified emails, now faster."):
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"}, summary=summary)
    return await clients.put(f"/hub/tools/{tool_id}", json={"manifest": m, "check": CHECK, "readme": "x"})


async def _decide_update(clients, monkeypatch, tool_id, decision, reason=""):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    return await clients.post(f"/admin/hub/updates/{tool_id}", json={"decision": decision, "reason": reason},
                              headers={"X-Treg-Token": ADMIN})


async def test_a_new_version_of_a_listed_tool_waits_for_review(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Round 4 (Jason): every update to a listed tool goes through treg. v2 passes its check and
    waits; v1 keeps serving callers and search; only the maker can try v2 by @2."""
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "approve")
    r = await _v2(clients, tool_id)
    assert r.status_code == 200 and r.json()["status"] == "review" and "waits for treg" in r.json()["message"]
    assert (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["endpoint"]["version"] == 1
    assert tool_id in await _search_ids(clients)                                          # still listed, as v1
    stranger = {"X-Treg-Token": (await funded_user(clients, "v2-stranger@example.com"))["token"]}
    run = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=stranger)
    assert run.status_code == 200 and run.json()["recipe"] == f"{tool_id}@1"
    assert (await clients.post(f"/call/{tool_id}@2", json={"domain": "x"}, headers=stranger)).status_code == 404
    assert (await clients.post(f"/call/{tool_id}@2", json={"domain": "x"})).status_code == 200   # the maker
    assert (await clients.get(f"/catalog/endpoints/{tool_id}@2")).status_code == 404            # not public
    assert (await clients.get(f"/hub/{tool_id}@2")).status_code == 404
    queue = (await clients.get("/admin/hub/updates", headers={"X-Treg-Token": ADMIN})).json()
    assert queue[0]["tool_id"] == tool_id and queue[0]["now"]["version"] == 1 and queue[0]["new"]["version"] == 2
    assert queue[0]["new"]["summary"].endswith("now faster.")
    r = await _decide_update(clients, monkeypatch, tool_id, "approve")
    assert r.status_code == 200 and r.json()["listing"]["update"] is None
    run = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=stranger)
    assert run.json()["recipe"] == f"{tool_id}@2"
    assert tool_id in await _search_ids(clients)


async def test_a_rejected_update_keeps_the_approved_version(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "approve")
    await _v2(clients, tool_id)
    assert (await _decide_update(clients, monkeypatch, tool_id, "reject")).status_code == 422      # no reason
    r = await _decide_update(clients, monkeypatch, tool_id, "reject", "v2 drops the verified emails")
    assert r.json()["listing"]["update"] == {"state": "rejected", "version": None, "pricing": None,
                                             "reason": "v2 drops the verified emails"}
    mine = {t["version"]: t["status"] for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id}
    assert mine == {1: "live", 2: "rejected"}
    assert (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["endpoint"]["version"] == 1
    # v3 waits again, and a newer waiting version replaces an older one
    await _v2(clients, tool_id, summary="Decision makers of a company, with verified emails, v3.")
    await _v2(clients, tool_id, summary="Decision makers of a company, with verified emails, v4.")
    mine = {t["version"]: t["status"] for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id}
    assert mine == {1: "live", 2: "rejected", 3: "superseded", 4: "review"}


async def test_a_price_change_on_a_listed_tool_waits_for_review(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "approve")
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 0.5})
    assert r.json()["price_label"] == "$0.01 a run" and r.json()["pending_price_usd"] == 0.5
    assert (await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 500})).status_code == 422   # checked now
    stranger = {"X-Treg-Token": (await funded_user(clients, "price-stranger@example.com"))["token"]}
    run = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=stranger)
    assert run.json()["usage"]["price_micro"] == 10_000                                   # the approved price
    queue = (await clients.get("/admin/hub/updates", headers={"X-Treg-Token": ADMIN})).json()
    assert queue[0]["new_price_usd"] == 0.5 and queue[0]["new"] is None
    await _decide_update(clients, monkeypatch, tool_id, "approve")
    run = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=stranger)
    assert run.json()["usage"]["price_micro"] == 500_000


async def test_unlisting_an_approved_tool_keeps_it_under_review(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Hub simulation run 2: approve, unlist, then change was a way round the review. Now an approved
    tool stays reviewed: unlisted, it is out of search, callers by id keep the approved version and
    price, and every change still waits. Listing it again needs no new review."""
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "approve")
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"listed": False})
    assert r.json()["listing"]["state"] == "unlisted" and r.json()["listed"] is False
    assert tool_id not in await _search_ids(clients)
    assert (await _v2(clients, tool_id)).json()["status"] == "review"
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 0.5})
    assert r.json()["pending_price_usd"] == 0.5
    got = (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["endpoint"]
    assert got["version"] == 1 and got["price_line"].startswith("seller $0.01 a run")
    queue = (await clients.get("/admin/hub/updates", headers={"X-Treg-Token": ADMIN})).json()
    assert [u["tool_id"] for u in queue] == [tool_id]
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    assert r.json()["listing"]["state"] == "approved" and tool_id in await _search_ids(clients)
    # a rejection that takes the approval back keeps the review too
    await _decide(clients, monkeypatch, tool_id, "reject", "not now")
    assert (await _v2(clients, tool_id, summary="Decision makers of a company, with verified emails, v3.")).json()["status"] == "review"


async def test_a_request_never_approved_is_withdrawn_by_unlisting(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"listed": False})
    assert r.json()["listing"] == {"state": "none"}
    assert (await _v2(clients, tool_id)).json()["status"] == "live"               # never reviewed: self-serve


async def test_the_review_queue_is_superadmin_only(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    await clients.patch(f"/hub/tools/{pub['tool_id']}", json={"listed": True})
    assert (await clients.get("/admin/hub/listings")).status_code in (401, 403)            # the maker
    r = await clients.post(f"/admin/hub/listings/{pub['tool_id']}", json={"decision": "approve"})
    assert r.status_code in (401, 403)


async def test_mcp_catalog_search_returns_an_approved_hub_tool(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    from tests.test_mcp import _call_tool, mcp_session
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "approve")
    token = (await funded_user(clients, "searcher@example.com"))["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": Q_OWN_WORDS, "limit": 10}, token=token)
    row = [r for r in out["results"] if r["endpoint_id"] == tool_id][0]
    assert row["kind"] == "hub" and row["no_key_needed"] is True and row["usd_per_call"] == 0.011


# ---------------------------------------------------------------------------------------------
# Phase 10.3: the public run log (docs/hub-listing-decisions.md, decisions 3-5).

async def test_the_public_page_shows_recent_runs_and_never_the_caller_inputs_or_output(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    # a stranger calls twice; the relay now answers with a marker that must never reach the page
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "OUTPUT-MARKER-77"}}'))
    hdr = {"X-Treg-Token": (await funded_user(clients, "stranger-log@example.com"))["token"]}
    for _ in range(2):
        assert (await clients.post(f"/call/{tool_id}", json={"domain": "INPUT-MARKER-55"}, headers=hdr)).status_code == 200
    page = (await clients.get(f"/hub/{tool_id}")).text
    assert "Recent runs" in page and "2 runs, 2 ok, 0 failed" in page and page.count("<span class=\"ok\">ok</span>") == 2
    assert "$0.01" in page and "runs per day" in page
    for leak in ("stranger-log@example.com", "INPUT-MARKER-55", "OUTPUT-MARKER-77"):
        assert leak not in page, leak
    md = (await clients.get(f"/hub/{tool_id}.md")).text
    assert "## Recent runs (30 days: 2 runs, 2 ok, 0 failed)" in md and "| ok |" in md
    for leak in ("stranger-log@example.com", "INPUT-MARKER-55", "OUTPUT-MARKER-77"):
        assert leak not in md, leak


async def test_the_public_log_hides_when_the_maker_switches_it_off(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    assert "Recent runs" in (await clients.get(f"/hub/{tool_id}")).text          # on by default
    assert (await clients.patch(f"/hub/tools/{tool_id}", json={"public_log": False})).status_code == 200
    assert "Recent runs" not in (await clients.get(f"/hub/{tool_id}")).text
    assert "## Recent runs" not in (await clients.get(f"/hub/{tool_id}.md")).text


# ---------------------------------------------------------------------------------------------
# Phase 10.4: the dashboard's Listing tab carries the two switches.

def test_the_dashboard_carries_the_listing_tab():
    from pathlib import Path
    page = (Path(__file__).parents[1] / "frontend/src/pages/HubPage.vue").read_text()
    for needle in ("setHubFlag('listed'", "setHubFlag('public_log'", "hub.tab==='listing'", "hubListingWords(hub.tool)"):
        assert needle in page, needle


# ---------------------------------------------------------------------------------------------
# The run ceiling a tool declares, and the cost a script can see (built with lead-pipeline)

def test_the_run_ceiling_comes_from_the_caller_then_the_tool_then_one_dollar():
    from treg.application.hub.runner import _ceiling
    assert _ceiling(None) == 1_000_000                      # nothing declared: $1.00
    assert _ceiling(None, 3.0) == 3_000_000                 # the tool's own limits.cost_usd
    assert _ceiling("0.5", 3.0) == 500_000                  # the caller's header always wins


async def test_a_script_sees_what_each_call_cost(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """A script keeps its own budget from `cost_usd` on each ctx.call result, because a run that
    passes its ceiling is stopped by treg and returns nothing. The x-treg-* headers stay hidden."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [1]}'))
    m = _script_manifest(uses=[EP], output={"fields": ["rows", "count"]})
    script = ("export default async function run(ctx) {"
              f"  const r = await ctx.call({EP!r}, {{ query: {{ aweme_id: 'x' }} }});"
              "  return { rows: [r.cost_usd, Object.keys(r.headers).filter(k => k.startsWith('x-treg-')).length], count: 1 };"
              "}")
    r = await clients.post("/hub/tools", json={"manifest": m, "script": script, "readme": "x",
                                               "check": {"inputs": {}, "fields": ["rows"]}})
    assert r.status_code == 201, r.text
    token = (await funded_user(clients, "cost-seen@example.com"))["token"]
    run = await clients.post(f"/call/{r.json()['tool_id']}", json={}, headers={"X-Treg-Token": token})
    assert run.status_code == 200, run.text
    assert run.json()["output"]["rows"] == [0.001, 0]        # the step's $0.001, and no x-treg header


# ---------------------------------------------------------------------------------------------
# TREG_HUB_TEAMS: the middle stage between off and open (owner, 2026-09-24)

def test_enabled_for_is_the_flag_then_the_team_list(monkeypatch):
    from treg.application import hub as hub_app
    monkeypatch.setenv("TREG_HUB_ENABLED", "1"); monkeypatch.setenv("TREG_HUB_TEAMS", "treg-hub, Acme")
    get_settings.cache_clear()
    assert hub_app.enabled() is True
    assert hub_app.enabled_for("treg-hub") and hub_app.enabled_for("acme") and hub_app.enabled_for("ACME")
    assert not hub_app.enabled_for("someone-else") and not hub_app.enabled_for(None)
    monkeypatch.setenv("TREG_HUB_TEAMS", "")
    get_settings.cache_clear()
    assert hub_app.enabled_for("someone-else"), "an empty list means every team"
    monkeypatch.setenv("TREG_HUB_ENABLED", "0"); monkeypatch.setenv("TREG_HUB_TEAMS", "treg-hub")
    get_settings.cache_clear()
    assert not hub_app.enabled_for("treg-hub"), "the flag off wins over the list"
    get_settings.cache_clear()


async def test_a_team_outside_the_list_sees_the_hub_as_off_but_can_read_a_contract(clients: AsyncClient, hub_on, monkeypatch):
    """The listed team publishes and runs; a second team gets 404 on every hub route and on
    /call/ of the tool, exactly as with the flag off. The public contract (catalog get, the share
    page) stays readable: it describes the hub, it does not run it."""
    tool_id = await _publish_live(clients)                  # the test client's team is the maker
    me = (await clients.get("/orgs")).json()[0]["slug"]
    other = (await funded_user(clients, "outsider@example.com"))["token"]
    monkeypatch.setenv("TREG_HUB_TEAMS", me)
    get_settings.cache_clear()
    try:
        assert (await clients.get("/hub/tools/mine")).status_code == 200
        r = await clients.get("/hub/tools/mine", headers={"X-Treg-Token": other})
        assert r.status_code == 404, r.text
        r = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={"X-Treg-Token": other})
        assert r.status_code == 404, r.text                    # the id is unknown to that team
        assert (await clients.get(f"/catalog/endpoints/{tool_id}")).status_code == 200
        assert (await clients.get(f"/hub/{tool_id}")).status_code == 200
    finally:
        monkeypatch.delenv("TREG_HUB_TEAMS", raising=False)
        get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# ctx.charge end to end: the caller pays fee + charges, the maker earns both, on one hold

async def _publish_charging_script(clients: AsyncClient, pricing: dict, script: str) -> str:
    m = _script_manifest(name="own-waterfall", uses=[], output={"fields": ["email", "results"]}, pricing=pricing)
    r = await clients.post("/hub/tools", json={"manifest": m, "script": script, "readme": "x",
                                               "check": {"inputs": {}, "fields": ["email"]}})
    assert r.status_code == 201, r.text
    tool_id = r.json()["tool_id"]
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import HubTool
    async with session_maker() as s:
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="live"))
        await s.commit()
    return tool_id


async def test_max_price_usd_needs_a_script_and_shows_in_the_public_pricing(clients: AsyncClient, hub_on):
    await _own_supabase(clients)
    m = _steps_manifest(pricing={"max_price_usd": 0.5})
    m.pop("price_usd", None)
    r = await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "x"})
    assert r.status_code == 422 and "max_price_usd" in r.text and "script" in r.text
    tool_id = await _publish_charging_script(
        clients, {"max_price_usd": 0.5},
        "export default async function run(ctx) { return { email: null, results: 0 }; }")
    view = (await clients.get(f"/catalog/endpoints/{tool_id}")).json()
    assert "up to $0.5 a run" in view["endpoint"]["price_line"], "the caller must see the cap before running"


async def test_charges_are_billed_to_the_caller_and_earned_by_the_maker(
    clients: AsyncClient, hub_on,
):
    """Jason's waterfall over vendors treg does not carry: the script bills a fee and what each step
    cost at the maker's vendor, the caller pays those lines, the maker earns all of it. Here: a fee
    of $0.01, $0.001 (vendor-a) and $0.03 (vendor-c hit), a zero line for a miss that is dropped:
    the caller pays $0.041, the maker earns $0.041, and the $0.05 cap is refunded down to that."""
    tool_id = await _publish_charging_script(
        clients, {"max_price_usd": 0.05},
        "export default async function run(ctx) {"
        "  ctx.charge(0.01, 'fee');"
        "  ctx.charge(0.001, 'vendor-a');"
        "  ctx.charge(0, 'vendor-b miss');"
        "  ctx.charge(0.03, 'vendor-c hit');"
        "  return { email: 'x@y.z', results: 1 };"
        "}")
    maker_org = (await clients.get("/orgs")).json()[0]["org_id"]
    maker_before = (await clients.get(f"/orgs/{maker_org}/balance")).json()["balance_micro"]
    buyer = await funded_user(clients, "waterfall-buyer@example.com")
    buyer_before = (await clients.get(f"/orgs/{buyer['org_id']}/balance", headers={"X-Treg-Token": buyer["token"]})).json()["balance_micro"]

    run = await clients.post(f"/call/{tool_id}", json={}, headers={"X-Treg-Token": buyer["token"]})
    assert run.status_code == 200, run.text
    u = run.json()["usage"]
    assert u["charged_micro"] == 41_000 and u["price_micro"] == 41_000 and u["steps_micro"] == 0
    assert u["cost_micro"] == 41_000
    lines = [t for t in run.json()["trace"] if t["outcome"] == "charged"]
    assert [(t["call"], t["cost_micro"]) for t in lines] == [("fee", 10_000), ("vendor-a", 1_000), ("vendor-c hit", 30_000)]

    buyer_after = (await clients.get(f"/orgs/{buyer['org_id']}/balance", headers={"X-Treg-Token": buyer["token"]})).json()["balance_micro"]
    maker_after = (await clients.get(f"/orgs/{maker_org}/balance")).json()["balance_micro"]
    assert buyer_before - buyer_after == 41_000
    assert maker_after - maker_before == 41_000


async def test_a_charge_over_the_cap_fails_the_run_and_the_caller_pays_nothing(clients: AsyncClient, hub_on):
    tool_id = await _publish_charging_script(
        clients, {"max_price_usd": 0.02},
        "export default async function run(ctx) { ctx.charge(0.5, 'vendor-d'); return { email: 'x@y.z', results: 1 }; }")
    buyer = await funded_user(clients, "capped-buyer@example.com")
    before = (await clients.get(f"/orgs/{buyer['org_id']}/balance", headers={"X-Treg-Token": buyer["token"]})).json()["balance_micro"]
    run = await clients.post(f"/call/{tool_id}", json={}, headers={"X-Treg-Token": buyer["token"]})
    assert run.status_code == 424 and "max_price_usd" in run.text
    after = (await clients.get(f"/orgs/{buyer['org_id']}/balance", headers={"X-Treg-Token": buyer["token"]})).json()["balance_micro"]
    assert before == after


async def test_a_price_edit_on_a_script_moves_its_cap(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """`treg hub price` on a script sets `max_price_usd`: its amounts live in run.js."""
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    r = await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 0.004})
    assert r.json()["pricing"] == {"mode": "charge", "max_price_usd": 0.004}
    assert (await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 0})).status_code == 422
    token = (await funded_user(clients, "b8@example.com"))["token"]
    run = await clients.post(f"/call/{tool_id}", json={}, headers={"X-Treg-Token": token})
    assert run.status_code == 424 and "max_price_usd" in run.text      # 5 rows x $0.002 passes the new cap


# ---------------------------------------------------------------------------------------------
# Capability (docs/hub-listing-decisions.md round 3): the maker proposes a catalog job; the approval
# sets it; the tool then sits beside that job's providers in catalog_get, with a seeded success rate.

CAP = "tiktok.video.comments"           # EP's own job, with several catalog providers


@pytest.mark.parametrize("bad,words", [
    ("tiktok.nothing.here", "not a catalog capability"),
    ("Not An Id", "catalog capability id"),
    (5, "catalog capability id"),
])
def test_capability_must_be_a_catalog_job(bad, words):
    caps = set(catalog_store.load().capabilities)
    v = validate(_steps_manifest(capability=CAP), catalog_ids=CATALOG, own_tools=OWN, capabilities=caps)
    assert v.manifest["capability"] == CAP
    with pytest.raises(ManifestError) as e:
        validate(_steps_manifest(capability=bad), catalog_ids=CATALOG, own_tools=OWN, capabilities=caps)
    assert e.value.field == "capability" and words in e.value.rule


def test_the_seed_gives_way_to_real_runs():
    from treg.domain.hub import seeded_observed
    assert seeded_observed(0, 0)["ok_rate"] == 0.9 and seeded_observed(0, 0)["estimated"] is True
    assert seeded_observed(20, 20)["ok_rate"] == 0.98 and seeded_observed(20, 20)["estimated"] is False
    assert seeded_observed(0, 20)["ok_rate"] == 0.18           # 20 failures outweigh the seed
    assert seeded_observed(3, 5)["ok_rate"] == 0.75


async def _approved_with_capability(clients, monkeypatch, capability=None):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "figma.com"}}'))
    await _own_supabase(clients)
    m = _steps_manifest(steps=[{"name": "people", "call": EP, "input": {"aweme_id": "$input.domain"}}],
                        output={"leads": "$people.data"}, price_usd=0.01, capability=CAP)
    r = await clients.post("/hub/tools", json={"manifest": m, "check": CHECK, "readme": "x"})
    assert r.status_code == 201 and r.json()["status"] == "live", r.text
    tool_id = r.json()["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    return tool_id


async def test_an_approved_capability_puts_the_tool_beside_the_providers(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _approved_with_capability(clients, monkeypatch)
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    queue = (await clients.get("/admin/hub/listings", headers={"X-Treg-Token": ADMIN})).json()
    assert queue[0]["proposed_capability"] == CAP and queue[0]["proposed_capability_providers"] >= 2
    # before approval: nobody sees it beside the providers
    sib = (await clients.get(f"/catalog/endpoints/{EP}")).json()["siblings"]
    assert tool_id not in [s["id"] for s in sib]
    r = await _decide(clients, monkeypatch, tool_id, "approve")
    assert r.json()["listing"]["capability"] == CAP
    sib = (await clients.get(f"/catalog/endpoints/{EP}")).json()["siblings"]
    mine = [s for s in sib if s["id"] == tool_id][0]
    assert mine["kind"] == "hub" and mine["price_line"].startswith("seller ")
    assert mine["observed"]["ok_rate"] == 0.9 and mine["observed"]["estimated"] is True and mine["observed"]["samples"] == 0
    # and the providers beside it, on its own page
    got = (await clients.get(f"/catalog/endpoints/{tool_id}")).json()
    assert got["endpoint"]["capability"] == CAP and got["endpoint"]["observed"]["estimated"] is True
    assert EP in [s["id"] for s in got["siblings"]]
    # a stranger's runs move the seed
    hdr = {"X-Treg-Token": (await funded_user(clients, "cap-buyer@example.com"))["token"]}
    for _ in range(2):
        assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=hdr)).status_code == 200
    mine = [s for s in (await clients.get(f"/catalog/endpoints/{EP}")).json()["siblings"] if s["id"] == tool_id][0]
    assert mine["observed"]["samples"] == 2 and mine["observed"]["ok_rate"] == round((4.5 + 2) / 7, 4)


async def test_the_admin_can_change_or_clear_the_capability(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    tool_id = await _approved_with_capability(clients, monkeypatch)
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    bad = await clients.post(f"/admin/hub/listings/{tool_id}", json={"decision": "approve", "capability": "no.such.job"},
                             headers={"X-Treg-Token": ADMIN})
    assert bad.status_code == 422
    r = await clients.post(f"/admin/hub/listings/{tool_id}", json={"decision": "approve", "capability": ""},
                           headers={"X-Treg-Token": ADMIN})
    assert r.json()["listed"] is True and r.json()["listing"]["capability"] == ""
    assert tool_id not in [s["id"] for s in (await clients.get(f"/catalog/endpoints/{EP}")).json()["siblings"]]
    assert (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["siblings"] == []
    # a rejection takes it away too
    await _decide(clients, monkeypatch, tool_id, "approve")
    await _decide(clients, monkeypatch, tool_id, "reject", "not yet")
    assert tool_id not in [s["id"] for s in (await clients.get(f"/catalog/endpoints/{EP}")).json()["siblings"]]


async def test_an_approved_tool_stays_visible_in_its_jobs_search_group(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """A routed job shows as one group, its providers capped; the hub tool of that job is in the
    group and never cut (found live 2026-09-24: it tied the providers and fell past the cap)."""
    from treg.domain.catalog.store import group_routed
    rows = [{"id": "r", "kind": "routed", "capability": "c"}] + [{"id": f"p{i}", "kind": "data", "capability": "c"} for i in range(6)] \
        + [{"id": "h", "kind": "hub", "capability": "c"}]
    out = group_routed(rows, max_children=5)
    assert "h" in [r["id"] for r in out] and out[0]["children_hidden"] == 1


async def test_the_public_price_follows_other_teams_runs_once_there_are_any(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Hub simulation run 2: the maker's own free runs shaped the public figure. Once another team
    has paid for a run, only such runs count."""
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, PER_ITEM, n=5)
    for _ in range(3):                                              # the maker's own runs: 5 rows, $0.011
        assert (await clients.post(f"/call/{tool_id}", json={})).status_code == 200
    mine = [t for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id][0]
    assert mine["price_from_tests"] is True
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [1, 1]}'))
    hdr = {"X-Treg-Token": (await funded_user(clients, "public-price@example.com"))["token"]}
    assert (await clients.post(f"/call/{tool_id}", json={}, headers=hdr)).status_code == 200   # 2 rows: $0.005
    mine = [t for t in (await clients.get("/hub/tools/mine")).json() if t["tool_id"] == tool_id][0]
    assert mine["price_from_tests"] is False and mine["price_samples"] == 1
    assert mine["price_range"] == "$0.005/run so far · seller up to $0.5 a run"


async def test_an_empty_answer_pays_no_seller_price(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Hub simulation run 2: a fee was taken for an answer whose every field was null."""
    script = ("export default async function run(ctx) {"
              "  ctx.charge(0.05, 'fee');"
              "  return ctx.inputs.search === 'none' ? { rows: null, count: null } : { rows: [1], count: 1 };"
              "}")
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, script, n=1)
    hdr = {"X-Treg-Token": (await funded_user(clients, "empty-answer@example.com"))["token"]}
    full = await clients.post(f"/call/{tool_id}", json={}, headers=hdr)
    assert full.status_code == 200 and full.json()["usage"]["price_micro"] == 50_000
    empty = await clients.post(f"/call/{tool_id}", json={"search": "none"}, headers=hdr)
    assert empty.status_code == 200 and empty.json()["usage"]["price_micro"] == 0
    from treg.application.hub.runner import _empty_answer
    assert _empty_answer({"rows": [], "count": 0}, ["rows", "count"]) is False            # 0 is an answer


# ---------------------------------------------------------------------------------------------
# Team names (hub simulation run 2): a team named treg, official, or a catalog provider would let a
# stranger's hub tool pass for ours or theirs.

@pytest.mark.parametrize("name", ["treg", "treg-official", "Treg Hub Verified", "Acme Official Data", "tikhub"])
async def test_a_reserved_team_name_is_refused(clients: AsyncClient, name):
    r = await clients.post("/orgs", json={"name": name})
    assert r.status_code == 422 and "reserved" in r.text, r.text


async def test_a_rename_to_a_reserved_slug_is_refused(clients: AsyncClient):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    r = await clients.patch(f"/orgs/{org_id}", json={"slug": "treg"})
    assert r.status_code == 400 and "reserved" in r.text
    r = await clients.patch(f"/orgs/{org_id}", json={"name": "Treg Official"})
    assert r.status_code == 400 and "reserved" in r.text


async def test_a_team_with_a_reserved_name_cannot_publish(clients: AsyncClient, hub_on):
    """Teams made before the rule keep their name; their hub tools are refused at publish."""
    from sqlalchemy import update
    from treg.infra.db import session_maker
    from treg.models import Org
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as s:
        await s.execute(update(Org).where(Org.id == org_id).values(name="treg"))
        await s.commit()
    await _own_supabase(clients)
    r = await clients.post("/hub/tools", json={"manifest": _steps_manifest(), "check": CHECK, "readme": "x"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "team_name_reserved"


async def test_a_publish_that_changes_the_price_says_so(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Hub simulation run 2: a new version's recipe.json price undid `treg hub price` silently."""
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"price_usd": 0.05})
    r = await _v2(clients, tool_id)                                            # recipe.json still says $0.01
    assert "$0.05 a run -> $0.01 a run" in r.json()["price_note"]


async def test_the_update_queue_names_fields_the_new_version_stopped_returning(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Hub simulation run 2: v4 never returned the industry and passed a check that asked only for
    the domain. The reviewer sees the fields the approved version filled and the new one does not."""
    script = ("export default async function run(ctx) {"
              "  const r = await ctx.call('" + EP + "', {query: {aweme_id: 'x'}});"
              "  return { rows: r.json.data, count: FULL ? r.json.data.length : null };"
              "}")
    tool_id = await _publish_script_priced(clients, monkeypatch, 0.5, script.replace("FULL", "true"), n=2)
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "approve")
    m = _script_manifest(uses=[EP], pricing={"max_price_usd": 0.5})
    r = await clients.put(f"/hub/tools/{tool_id}", json={"manifest": m, "script": script.replace("FULL", "false"),
                                                         "readme": "x", "check": {"inputs": {}, "fields": ["rows"]}})
    assert r.json()["status"] == "review", r.text
    queue = (await clients.get("/admin/hub/updates", headers={"X-Treg-Token": ADMIN})).json()
    assert queue[0]["fields_lost"] == ["count"]


async def test_a_check_can_hold_several_cases(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    """Hub simulation run 3: one sample could not reach both paths of a tool. Every case runs at
    publish; the first that fails fails the check and names its number."""
    script = ("export default async function run(ctx) {"
              "  return ctx.inputs.search === 'none' ? { rows: [], count: 0 } : { rows: [1], count: 1 };"
              "}")
    m = _script_manifest(uses=[], pricing={"max_price_usd": 0.01})
    ok = {"cases": [{"inputs": {}, "fields": ["rows"]}, {"inputs": {"search": "x"}, "fields": ["count"]}]}
    r = await clients.post("/hub/tools", json={"manifest": m, "script": script, "readme": "x", "check": ok})
    assert r.status_code == 201 and r.json()["status"] == "live" and r.json()["check"]["cases"] == 2, r.text
    bad = {"cases": [{"inputs": {}, "fields": ["rows"]}, {"inputs": {"search": "none"}, "fields": ["rows"]}]}
    r = await clients.put(f"/hub/tools/{r.json()['tool_id']}", json={"manifest": m, "script": script, "readme": "x", "check": bad})
    assert r.json()["status"] == "failed" and r.json()["check"]["case"] == 2
    err = await clients.post("/hub/tools", json={"manifest": {**m, "name": "other"}, "script": script, "readme": "x",
                                                 "check": {"cases": [{"inputs": {"nope": 1}, "fields": ["rows"]}]}})
    assert err.status_code == 422 and err.json()["detail"]["field"] == "check.cases[0].inputs.nope"


async def test_a_maker_cannot_review_its_own_hub_tool(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    run = await clients.post(f"/call/{pub['tool_id']}", json={"domain": "x"})
    assert run.status_code == 200
    r = await clients.post("/reviews", json={"call_id": run.json()["run_id"], "usefulness": "useful"})
    assert r.status_code == 400 and "own hub tool" in r.text, r.text


async def test_a_team_tool_may_not_point_at_treg(clients: AsyncClient, hub_on):
    sid = (await clients.post("/secrets", json={"name": "relay-key", "value": "K"})).json()["id"]
    from treg.config import get_settings
    host = get_settings().public_url.rstrip("/")
    r = await clients.post("/tools", json={"name": "relay", "base_url": f"{host}/call", "secret_id": sid})
    assert r.status_code == 422 and "treg itself" in r.text


async def test_a_rejected_tool_serves_only_its_maker(clients: AsyncClient, hub_on, platform_on, monkeypatch):
    pub = await _live_tool_with_readme(clients, monkeypatch, price=0.01)
    tool_id = pub["tool_id"]
    await clients.patch(f"/hub/tools/{tool_id}", json={"listed": True})
    await _decide(clients, monkeypatch, tool_id, "reject", "poses as someone else")
    other = {"X-Treg-Token": (await funded_user(clients, "rejected-caller@example.com"))["token"]}
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=other)).status_code == 404
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 200      # the maker
    assert (await clients.get(f"/hub/{tool_id}")).status_code == 410
    assert (await clients.get(f"/catalog/endpoints/{tool_id}")).status_code == 404
