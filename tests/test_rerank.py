import json
from datetime import timedelta

import pytest

from tests.test_catalog_find import _fake_v2, _find, _on
from tests.test_endpoint_stats import _now, _record
from treg.api import app
from treg.application import catalog_stats
from treg.config import Settings
from treg.domain.catalog import store as catalog_store
from treg.infra import judge as judge_infra
from treg.infra.db import session_maker

JOB = "extract the text of a web page"


async def _job_rows(clients, monkeypatch, **settings) -> list[dict]:
    _on(monkeypatch, find_engine="v2", **settings)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({"web.extract": 0.92}, plat=("web", 0.9)))
    await _find(clients, JOB)
    await app.state.endpoint_observation_reader.wait_for_idle()
    _, events = await _find(clients, JOB)
    return [row for row in events[-1]["rows"] if row["capability"] == "web.extract"]


def _callable(rows: list[dict]) -> list[str]:
    return [row["provider"] for row in rows if row["platform_eligible"]]


def test_the_tiers_setting_refuses_a_malformed_value():
    assert Settings(ranking_tiers=json.dumps({"web.extract": {"good": ["a"], "bad": ["b"]}})).ranking_tier_map == {
        "web.extract": {"a": "good", "b": "bad"}}
    for bad in ("[", "[]", '{"web.extract": ["a"]}', '{"web.extract": {"mid": ["a"]}}',
                '{"web.extract": {"good": ["A"]}}', '{"web.extract": {"good": ["a"], "bad": ["a"]}}',
                '{"Web Extract": {"good": ["a"]}}', '{"web.extract": {"good": "a"}}'):
        with pytest.raises(ValueError):
            Settings(ranking_tiers=bad)


async def test_a_row_only_a_teams_own_key_can_call_follows_every_callable_row(clients, monkeypatch):
    flags = [row["platform_eligible"] for row in await _job_rows(clients, monkeypatch)]
    assert False in flags and flags == sorted(flags, reverse=True)


async def test_a_good_vendor_leads_its_job_and_a_bad_one_trails_it(clients, monkeypatch):
    before = _callable(await _job_rows(clients, monkeypatch))
    good, bad = before[-1], before[0]
    tiers = json.dumps({"web.extract": {"good": [good], "bad": [bad]}})
    after = _callable(await _job_rows(clients, monkeypatch, ranking_tiers=tiers))
    assert (after[0], after[-1]) == (good, bad)


async def test_tiers_for_another_capability_leave_the_job_in_its_measured_order(clients, monkeypatch):
    before = _callable(await _job_rows(clients, monkeypatch))
    tiers = json.dumps({"web.search": {"good": [before[-1]], "bad": [before[0]]}})
    assert _callable(await _job_rows(clients, monkeypatch, ranking_tiers=tiers)) == before


async def test_a_long_record_outranks_a_cheaper_vendor_with_a_few_early_successes(clients, monkeypatch):
    cat = catalog_store.load()
    eligible = [ep for ep in cat.for_capability("web.extract")
                if ep.get("kind") != "routed" and cat.platform_eligible(ep)]
    by_price = sorted(eligible, key=lambda ep: cat.cost_view(ep.get("cost"), ep.get("provider"))["usd"])
    few, long = by_price[0]["id"], by_price[-1]["id"]
    for _ in range(5):
        await _record(few, 200)
    for _ in range(200):
        await _record(long, 200)
    await catalog_stats.refresh(session_maker, now=_now() + timedelta(minutes=2))
    ids = [row["id"] for row in await _job_rows(clients, monkeypatch)]
    assert ids.index(long) < ids.index(few)
