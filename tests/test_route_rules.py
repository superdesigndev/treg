"""Published route rules: parsing, matching, the worker's snapshot, and the routed plan."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from treg.application.call import route_rules as reader_app
from treg.application.call import service as call_service
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import rules as R
from treg.infra import kv

from test_call_events import _flushed, events_on  # noqa: F401
from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _first_providers, _relay_by_provider, enrichment_on  # noqa: F401


def envelope(version=1, rules=None, enabled=True, **config):
    return json.dumps({"version": version, "config": {
        "schema_version": 1, "enabled": enabled,
        "rules": rules if rules is not None else [
            {"id": "find-fr", "capability": "people.email.find", "match": {"tld": "fr"},
             "order": ["tomba"]}],
        **config}})


# ---- parsing and matching ----------------------------------------------------------------------

def test_a_rule_set_parses_into_rules_and_a_canonical_form():
    parsed = R.parse(envelope(version=3))
    assert parsed.version == 3 and parsed.enabled
    assert parsed.rules == (R.Rule("find-fr", "people.email.find", (("tld", "fr"),), ("tomba",)),)
    assert parsed.canonical == R.parse(envelope(version=4)).canonical


def _rule(**overrides):
    return {"id": "r", "capability": "people.email.find", "match": {"tld": "fr"},
            "order": ["tomba"], **overrides}


@pytest.mark.parametrize("raw", [
    "{", " " * (R.MAX_BYTES + 1), '{"version":1,"version":1,"config":{}}',
    json.dumps({"version": 1}), envelope(version=0), envelope(version=True),
    envelope(version=2147483648), envelope(schema_version=2), envelope(enabled="yes"),
    envelope(extra=True), envelope(rules={}),
    envelope(rules=[_rule(), _rule()]),                    # duplicate id
    envelope(rules=[_rule(id="Upper")]), envelope(rules=[_rule(extra=1)]),
    envelope(rules=[{"id": "r", "capability": "people.email.find", "order": ["tomba"]}]),
    envelope(rules=[_rule(match={})]), envelope(rules=[_rule(match={"tld": ""})]),
    envelope(rules=[_rule(match={"tld": 1})]), envelope(rules=[_rule(match={"TLD": "fr"})]),
    envelope(rules=[_rule(order=[])]), envelope(rules=[_rule(order=["tomba", "tomba"])]),
    envelope(rules=[_rule(order=["Tomba"])]), envelope(rules=[_rule(order="tomba")]),
    envelope(rules=[_rule(reason="")]),
    envelope(rules=[_rule(id=f"r{i}") for i in range(R.MAX_RULES_PER_CAPABILITY + 1)]),
])
def test_a_malformed_rule_set_is_refused(raw):
    with pytest.raises((ValueError, TypeError)):
        R.parse(raw)


def test_a_rule_must_name_a_contract_its_recorded_segments_and_serving_providers():
    contracts = catalog_store.load().contracts
    providers = lambda cap: {"tomba", "hunter"}  # noqa: E731
    R.check_against(R.parse(envelope()), contracts, providers)
    for rule, why in ((_rule(capability="no.such"), "no contract"),
                      (_rule(match={"seniority": "vp"}), "records no seniority"),
                      (_rule(order=["tomba", "nobody"]), "nobody serve")):
        with pytest.raises(ValueError, match=why):
            R.check_against(R.parse(envelope(rules=[rule])), contracts, providers)


def test_the_most_specific_rule_wins_then_the_first_listed():
    parsed = R.parse(envelope(rules=[
        _rule(id="fr", order=["hunter"]), _rule(id="fr-again", order=["tomba"]),
        _rule(id="fr-named", match={"tld": "fr", "input_type": "domain+full_name"}, order=["enrow"]),
    ]))
    find = "people.email.find"
    assert R.match(parsed, find, {"tld": "fr", "input_type": "domain+full_name"}).id == "fr-named"
    assert R.match(parsed, find, {"tld": "fr", "input_type": "linkedin_url"}).id == "fr"
    assert R.match(parsed, find, {"tld": "de"}) is None
    assert R.match(parsed, "people.phone.find", {"tld": "fr"}) is None
    assert R.match(R.parse(envelope(enabled=False)), find, {"tld": "fr"}) is None
    assert R.match(None, find, {"tld": "fr"}) is None


# ---- the worker's snapshot ---------------------------------------------------------------------

@pytest.fixture
def snapshot_env(monkeypatch):
    clock = [100.0]
    store = SimpleNamespace(read_route_rules=AsyncMock(return_value=None))
    monkeypatch.setattr(reader_app, "_clock", lambda: clock[0])
    monkeypatch.setattr(reader_app, "_store", lambda: store)
    monkeypatch.setattr(reader_app, "_providers_of", lambda cap: {"tomba", "hunter", "enrow"})
    return reader_app.Reader(), store, clock


async def test_one_read_per_refresh_interval_and_a_missing_key_means_no_rules(snapshot_env):
    reader, store, clock = snapshot_env
    assert await reader.current(30) is None
    store.read_route_rules.return_value = envelope()
    assert await reader.current(30) is None, "the next read waits for the interval"
    clock[0] += 30
    assert (await reader.current(30)).version == 1
    assert store.read_route_rules.await_count == 2
    store.read_route_rules.return_value = None
    clock[0] += 30
    assert await reader.current(30) is None


@pytest.mark.parametrize("bad", [
    kv.FlowControlReadFailure.UNAVAILABLE, kv.FlowControlReadFailure.INVALID, "{",
    envelope(version=2, rules=[_rule(order=["nobody"])]),       # names a provider that serves nothing
    envelope(version=1, rules=[_rule(order=["hunter"])]),       # different content, same version
    envelope(version=0),
])
async def test_every_failure_keeps_the_last_valid_rule_set(snapshot_env, bad):
    reader, store, clock = snapshot_env
    store.read_route_rules.return_value = envelope(version=1)
    assert (await reader.current(30)).version == 1
    store.read_route_rules.return_value = bad
    clock[0] += 30
    kept = await reader.current(30)
    assert kept.version == 1 and kept.rules[0].order == ("tomba",)


async def test_a_read_that_raises_keeps_the_rules_and_a_higher_version_replaces_them(snapshot_env):
    reader, store, clock = snapshot_env
    store.read_route_rules.return_value = envelope(version=1)
    await reader.current(30)
    store.read_route_rules.side_effect = RuntimeError("boom")
    clock[0] += 30
    assert (await reader.current(30)).version == 1
    store.read_route_rules.side_effect = None
    store.read_route_rules.return_value = envelope(version=2, rules=[_rule(order=["hunter"])])
    clock[0] += 30
    assert (await reader.current(30)).rules[0].order == ("hunter",)


# ---- the routed plan and its event -------------------------------------------------------------

@pytest.fixture
def published(monkeypatch):
    """Route rules on, with the published set served from a fake store."""
    reader_app.reset()
    store = SimpleNamespace(read_route_rules=AsyncMock(return_value=envelope()))
    monkeypatch.setattr(reader_app, "_store", lambda: store)
    monkeypatch.setattr(reader_app, "get_settings", lambda: SimpleNamespace(
        route_rules_enabled=True, route_rules_refresh_s=30))
    yield store
    reader_app.reset()


async def test_a_published_rule_goes_ahead_of_the_yaml_order(clients, enrichment_on, monkeypatch, published):
    cat = catalog_store.load()
    monkeypatch.setitem(cat.contracts, "people.email.find", dataclasses.replace(
        cat.contracts["people.email.find"], prefer_by_domain_suffix={"fr": ("hunter",)}))
    published.read_route_rules.return_value = envelope(rules=[_rule(order=["leadmagic"])])
    assert await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.fr"}) == ["leadmagic", "hunter"]
    assert await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.de"}, n=1) != ["leadmagic"]
    header = await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.fr"}, header="hunter", n=1)
    assert header == ["hunter"], "the caller's prefer header replaces published rules"


async def test_off_by_default_the_published_set_is_never_read(clients, enrichment_on, monkeypatch):
    reader_app.reset()
    store = SimpleNamespace(read_route_rules=AsyncMock(return_value=envelope()))
    monkeypatch.setattr(reader_app, "_store", lambda: store)
    await _first_providers(ROUTED, {"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert store.read_route_rules.await_count == 0


async def test_the_routed_event_records_the_rule_it_ran_under(clients, enrichment_on, events_on,
                                                            monkeypatch, published):
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "j.dupont@acme.fr", "score": 96,
                                   "verification": {"status": "valid"}}})]}, []))
    routed = await clients.post(f"/call/{ROUTED}", json={"full_name": "Jean Dupont", "domain": "acme.fr"})
    assert routed.status_code == 200, routed.text
    events = await _flushed(events_on)
    parent = next(e for e in events if e["call_ref"] == routed.headers["x-treg-call-id"])
    assert parent["input_meta"] == {"domain_class": "company", "tld": "fr",
                                    "route_rules_version": "1", "route_rule": "find-fr"}
    child = next(e for e in events if e["call_ref"] == routed.headers["x-treg-call-id"] + ":r0")
    assert child["provider"] == "tomba" and "route_rule" not in (child.get("input_meta") or {})


# ---- storage -----------------------------------------------------------------------------------

@pytest.fixture(params=["model", "migration"])
async def rule_table(request):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from treg.infra import db
    from treg.models import RouteRuleVersion

    table = RouteRuleVersion.__table__
    migration = import_module("treg.alembic.versions.0096_route_rule_version")
    await db.reset_db()
    async with db._engine.connect() as connection:
        transaction = await connection.begin()
        try:
            if request.param == "migration":
                await connection.run_sync(table.drop)

                def upgrade(sync):
                    with Operations.context(MigrationContext.configure(sync)):
                        migration.upgrade()
                await connection.run_sync(upgrade)
            yield connection, table
        finally:
            await transaction.rollback()


async def test_only_an_approved_version_can_be_published(rule_table):
    from datetime import datetime

    import sqlalchemy as sa

    connection, table = rule_table
    now = datetime(2026, 1, 1)
    row = {"version": 1, "config": json.loads(envelope())["config"], "reason": "synthetic",
           "created_by": "agent", "created_at": now}
    await connection.execute(table.insert().values(**row))
    for bad in ({"publish_status": "pending"},                          # not approved
                {"review_status": "approved"},                         # no reviewer
                {"review_status": "approved", "reviewed_by": "op", "reviewed_at": now,
                 "publish_status": "published"}):                      # no publisher
        with pytest.raises(sa.exc.IntegrityError):
            async with connection.begin_nested():
                await connection.execute(table.update().where(table.c.version == 1).values(**bad))
    await connection.execute(table.update().where(table.c.version == 1).values(
        review_status="approved", reviewed_by="op", reviewed_at=now,
        publish_status="published", published_by="agent", published_at=now))
    got = (await connection.execute(sa.select(table.c.config, table.c.publish_status))).one()
    assert got.publish_status == "published" and got.config["rules"][0]["id"] == "find-fr"
