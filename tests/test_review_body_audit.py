"""The blind audit of the body pilot's changed codes: the judge sees two codings as A and B in an
order it cannot read back, the winner is mapped to reason or body, spend is capped and nothing is
written."""
import json

import pytest
from sqlmodel import select

from treg.application import review_body_audit
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallReview
from test_review_body_pilot import PEOPLE, _add, _archive, _codes, _org, _record, _review


def test_parse_pairs_takes_the_pilot_codes_and_refuses_unknown_ones():
    assert review_body_audit.parse_pairs("12:EMPTY:PARTIAL, 34:WRONG,56:CONTEXT:") == [
        (12, "EMPTY", "PARTIAL"), (34, "WRONG", None), (56, "CONTEXT", None)]
    with pytest.raises(ValueError):
        review_body_audit.parse_pairs("12:NOPE")


def test_the_order_is_blind_and_fixed_per_review():
    order = {i: review_body_audit._reason_first(i) for i in range(1, 200)}
    assert 60 < sum(order.values()) < 140
    assert all(review_body_audit._reason_first(i) == first for i, first in order.items())


def _judge(monkeypatch, pick):
    """Mock judge: `pick(prompt)` returns the verdict JSON; 1,000 tokens in and 100 out per call."""
    prompts = []

    async def judge(prompt, *, model, max_tokens):
        prompts.append(prompt)
        return pick(prompt), {"prompt_tokens": 1000, "completion_tokens": 100}
    monkeypatch.setattr(review_body_audit, "_judge", judge)
    monkeypatch.setattr(get_settings(), "ai_gateway_api_key", "test-key", raising=False)
    return prompts


def _coding(prompt, letter):
    return prompt.split(f"Coding {letter}: ")[1].split("\n")[0].split(" ")[0]


async def test_winners_map_back_to_reason_or_body_and_nothing_is_written(clients, monkeypatch):
    org = await _org(clients)
    rows = [_review(org, f"a-{i}", "WRONG") for i in range(4)] + [_record(org, f"a-{i}", f"k{i}") for i in range(4)]
    rows += [_review(org, "routed", "WRONG", routed=True), _record(org, "routed", tier="routed")]
    await _add(*rows)
    _archive(monkeypatch, {f"k{i}": PEOPLE for i in range(4)})
    # The judge always picks the coding whose main code is PARTIAL, the body one.
    prompts = _judge(monkeypatch, lambda p: {"verdict": "A" if _coding(p, "A") == "PARTIAL" else "B",
                                             "code": "PARTIAL"})
    before = await _codes()
    ids = {r.call_id: r.id for r in rows if hasattr(r, "usefulness")}
    pairs = [(ids[f"a-{i}"], "PARTIAL", None) for i in range(4)] + [(ids["routed"], "PARTIAL", None), (999999, "EMPTY", None)]

    out = await review_body_audit.run(pairs=pairs, model="m", max_usd=5, usd_per_mtok_in=4,
                                      usd_per_mtok_out=20, budget=500, max_tokens=100)
    assert out["asked"] == 4 and out["winners"] == {"body": 4} and out["by_change"] == {"WRONG->PARTIAL": {"body": 4}}
    assert out["routed"] == 1 and out["missing"] == 1 and out["usd"] == pytest.approx(4 * 0.006)
    assert {_coding(p, "A") for p in prompts} == {"WRONG", "PARTIAL"}  # both orders were shown
    assert "came back empty" not in json.dumps(out)
    assert await _codes() == before


async def test_spend_cap_stops_new_requests(clients, monkeypatch):
    org = await _org(clients)
    await _add(*[_review(org, f"c-{i}", "WRONG") for i in range(3)], *[_record(org, f"c-{i}", f"k{i}") for i in range(3)])
    _archive(monkeypatch, {f"k{i}": PEOPLE for i in range(3)})
    _judge(monkeypatch, lambda p: {"verdict": "both", "code": "WRONG"})
    async with session_maker() as db:
        ids = [r.id for r in (await db.execute(select(CallReview))).scalars()]

    out = await review_body_audit.run(pairs=[(i, "EMPTY", None) for i in ids], model="m", max_usd=0.006,
                                      usd_per_mtok_in=4, usd_per_mtok_out=20, budget=500, max_tokens=100)
    assert out["asked"] == 1 and out["stopped_by_cost"] is True and out["winners"] == {"both": 1}


async def test_an_unreadable_verdict_is_counted(clients, monkeypatch):
    org = await _org(clients)
    await _add(_review(org, "u", "WRONG"), _record(org, "u", "ku"))
    _archive(monkeypatch, {"ku": PEOPLE})
    _judge(monkeypatch, lambda p: {"verdict": "maybe"})
    async with session_maker() as db:
        review_id = (await db.execute(select(CallReview.id))).scalar_one()
    out = await review_body_audit.run(pairs=[(review_id, "EMPTY", None)], model="m", max_usd=5,
                                      usd_per_mtok_in=4, usd_per_mtok_out=20, budget=500, max_tokens=100)
    assert out["unreadable"] == 1 and out["winners"] == {}


async def test_off_without_a_gateway_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_gateway_api_key", "", raising=False)
    assert await review_body_audit.run(pairs=[], model="m", max_usd=5, usd_per_mtok_in=4, usd_per_mtok_out=20,
                                       budget=500, max_tokens=100) == {"skipped": "no_gateway_key"}
