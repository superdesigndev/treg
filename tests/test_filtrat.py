"""Filtrat: email verification (optionally resolving catch-all domains) and email finding, all
synchronous, each answer reporting its own charge in credits_used. Prices, answers and the miss
shape observed live 2026-10-29; see src/treg/catalog/filtrat.yaml."""
from __future__ import annotations

import json

import pytest

from treg import oauth_providers as P
from treg.application.call import settle as call_settle
from treg.domain.capacity import signatures as S
from treg.domain.catalog import store as cs
from treg.domain.money import settlement

ENDPOINTS = ("filtrat.people.email.verify", "filtrat.people.email.verify.resolve_catchall",
             "filtrat.people.email.find", "filtrat.people.email.find.resolve_catchall")


def _example(name: str) -> dict:
    return json.loads((cs.CATALOG_DIR / cs.EXAMPLES_DIRNAME / f"{name}.json").read_text())


def test_every_filtrat_route_is_platform_priced_and_settles_on_credits_used():
    cat = cs.load()
    eps = {e["id"]: e for e in cat.endpoints if e["provider"] == "filtrat"}
    assert set(eps) == set(ENDPOINTS)
    for eid, ep in eps.items():
        assert cat.platform_eligible(ep), eid
        assert cat.cost_view(ep["cost"], "filtrat")["usd"] > 0, eid
        assert ep["cost"]["settle"] == "usage" and ep["cost"]["usage"] == {"path": "credits_used", "unit": "credit"}, eid


@pytest.mark.parametrize("eid, resolve", [(e, e.endswith("resolve_catchall")) for e in ENDPOINTS])
def test_each_tool_pins_catch_all_resolution_on_the_shared_key(eid, resolve):
    """The price differs with resolve_catchall, so each tool fixes it on treg's own key."""
    ep = cs.load().by_id[eid]
    assert ep["platform_request"] == {"body.resolve_catchall": resolve}


@pytest.mark.parametrize("name, eid, micro", [
    ("filtrat.people.email.verify", "filtrat.people.email.verify", 250),                         # 1 credit
    ("filtrat.people.email.verify.resolve_catchall", "filtrat.people.email.verify.resolve_catchall", 1_250),  # 5
    ("filtrat.people.email.find", "filtrat.people.email.find", 5_000),                           # 20
    ("filtrat.people.email.find.resolve_catchall", "filtrat.people.email.find.resolve_catchall", 5_000),  # 20: found without resolution
    ("filtrat.people.email.find.miss", "filtrat.people.email.find", 0),                          # a miss is free
])
def test_an_answer_settles_at_its_credits_used(name, eid, micro):
    """1 credit = $0.00025 (fx.yaml), so 250 micro-USD a credit."""
    ep = cs.load().by_id[eid]
    basis = settlement.derive_basis(ep["cost"], request={"body": ep["test_request"]["body"]}, input_schema=ep["input"],
                                    unit_micro=250, terminal=False, usage_unit_micro=250)
    assert basis["amount"]["kind"] == "usage"
    assert settlement.settle(basis, {"terminal": call_settle._usage_document(json.dumps(_example(name)).encode())}) == micro


def test_the_adapters_read_the_live_answers():
    cat = cs.load()
    verify = cat.adapters["filtrat.people.email.verify"]
    out = verify.from_upstream(_example("filtrat.people.email.verify"))
    assert out["valid"] is True and out["status"] == "verified"
    assert verify.verdict_word(verify.verdict_raw(_example("filtrat.people.email.verify"))) == "valid"
    assert verify.is_miss({"status": "unknown", "credits_used": 0.0})
    assert verify.verdict_word("catchall") == "catch_all"

    find = cat.adapters["filtrat.people.email.find"]
    hit = find.from_upstream(_example("filtrat.people.email.find"))
    assert hit["email"] == "michael.buriak@tallium.com" and hit["verified"] is True
    assert not find.is_miss(_example("filtrat.people.email.find"))
    assert find.is_miss(_example("filtrat.people.email.find.miss"))       # 200, found_email null
    unverified = {**_example("filtrat.people.email.find"), "catchall_unverified": True}
    assert find.from_upstream(unverified)["verified"] is False


def test_out_of_credits_is_a_balance_signal():
    body = b'{"error":{"code":"INSUFFICIENT_CREDITS","message":"Not enough credits for this request.","detail":{"required":20.0}}}'
    signal = S.classify("filtrat", 402, {}, body)
    assert signal is not None and signal.kind == "balance"


def test_the_key_rides_as_a_bearer_header_and_the_probe_is_free():
    p = P.REGISTRY["filtrat"]
    assert (p.token_header, p.token_format, p.auth_kind) == ("Authorization", "Bearer {secret}", "key")
    assert p.base_url == "https://filtrat.io/v1" and p.probe_path == "/credits/balance"
