"""The per-call verdict word: declared per contract, mapped per adapter, stored on `CallRecord`."""

from __future__ import annotations

import json
import logging

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.application.call import service as call_service
from treg.domain.catalog import results
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import contracts as C
from treg.infra.db import session_maker
from treg.models import CallRecord

from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _relay_by_provider, enrichment_on  # noqa: F401


def _example(endpoint_id: str):
    path = catalog_store.example_path(endpoint_id)
    return json.loads(path.read_text()) if path else None


def _recording(cat) -> dict[str, tuple[C.Adapter, C.Contract]]:
    out = {}
    for eid, adapter in cat.adapters.items():
        contract = cat.contracts.get((cat.by_id.get(eid) or {}).get("capability") or "")
        if contract is not None and contract.verdict_words:
            out[eid] = (adapter, contract)
    return out


# ---- the declarations ------------------------------------------------------------------------

def test_every_verifier_example_maps_to_an_allowed_word():
    """A provider word the adapter does not map is stored as no verdict at all. A verifier's own
    example answer must therefore land on a word, so a new provider joins with its words mapped."""
    cat = catalog_store.load()
    verifiers = {eid: pair for eid, pair in _recording(cat).items()
                 if pair[1].capability == "people.email.verify"}
    assert verifiers, "people.email.verify records no verdict"
    unmapped = {}
    for eid, (adapter, contract) in verifiers.items():
        raw = adapter.verdict_raw(_example(eid))
        if adapter.verdict_word(raw) not in contract.verdict_words:
            unmapped[eid] = raw
    assert unmapped == {}, unmapped


def test_every_finder_example_with_a_claim_maps_to_an_allowed_word():
    cat = catalog_store.load()
    unmapped = {}
    for eid, (adapter, contract) in _recording(cat).items():
        raw = adapter.verdict_raw(_example(eid))
        if raw is not None and adapter.verdict_word(raw) not in contract.verdict_words:
            unmapped[eid] = raw
    assert unmapped == {}, unmapped


def test_adapter_verdict_maps_only_name_allowed_words():
    cat = catalog_store.load()
    recording = _recording(cat)
    stray = {}
    for eid, adapter in cat.adapters.items():
        if not (adapter.verdict or adapter.verdicts):
            continue
        if eid not in recording:
            stray[eid] = "declares verdict words but its contract records none"
            continue
        words = recording[eid][1].verdict_words
        bad = sorted(set(adapter.verdicts.values()) - set(words))
        if bad:
            stray[eid] = bad
    assert stray == {}, stray


def test_contract_verdict_must_map_into_its_words():
    with pytest.raises(ValueError, match="verdict"):
        C.parse_contracts({"contracts": {"x.verify": {
            "identity": [{"email": "str"}], "output": {"status": {"type": "str"}},
            "verdict": {"from": "status", "words": ["valid"], "map": {"ok": "fine"}}}}})


# ---- reading one answer ----------------------------------------------------------------------

def _body(doc) -> bytes:
    return json.dumps(doc).encode()


@pytest.mark.parametrize("endpoint_id,doc,word", [
    ("millionverifier.people.email.verify", {"quality": "good", "result": "ok"}, "valid"),
    ("millionverifier.people.email.verify", {"quality": "bad", "result": "unverified"}, "unknown"),
    ("zerobounce.people.email.verify", {"status": "catch-all"}, "catch_all"),
    # ZeroBounce `unknown` is a routing miss, and still a verdict worth counting
    ("zerobounce.people.email.verify", {"status": "unknown"}, "unknown"),
    ("contactout.people.email.verify", {"status_code": 200, "data": {"status": "accept_all"}}, "catch_all"),
    ("scrubby.people.email.verify", {"result": "Risky"}, "risky"),
    ("bounceban.people.email.verify", {"result": "risky", "is_accept_all": True}, "catch_all"),
    ("bounceban.people.email.verify", {"result": "risky", "is_accept_all": False}, "risky"),
    ("bounceban.people.email.verify", {"result": "deliverable", "is_accept_all": True}, "valid"),
    ("bounceban.people.email.verify", {"status": "verifying", "result": None}, None),
    ("limadata.people.email.verify", {"result": "Risky", "is_catch_all": True}, "catch_all"),
    ("limadata.people.email.verify", {"result": "Invalid", "is_catch_all": True}, "invalid"),
    ("hunter.people.email.find", {"data": {"email": "a@x.io", "verification": {"status": "valid"}}}, "verified"),
    ("hunter.people.email.find", {"data": {"email": "a@x.io", "verification": {"status": "accept_all"}}}, "unverified"),
    # a find that found nothing has no claim to report
    ("hunter.people.email.find", {"data": {"email": None}}, None),
    # a finder whose answer carries no `verified` output stores no word
    ("leadsforge.people.email.find", {"status": "succeeded", "email": "a@x.io"}, None),
])
def test_verdict_reads_the_answer(endpoint_id, doc, word):
    assert results.verdict(endpoint_id, 200, _body(doc)) == word


def test_no_verdict_for_a_failed_call_or_an_unreadable_body():
    assert results.verdict("millionverifier.people.email.verify", 500, _body({"result": "ok", "quality": "good"})) is None
    assert results.verdict("millionverifier.people.email.verify", 200, b"not json") is None
    assert results.verdict("firecrawl.web.scrape", 200, _body({"success": True})) is None


def test_an_unmapped_provider_word_is_no_verdict_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="treg.catalog"):
        word = results.verdict("millionverifier.people.email.verify", 200,
                               _body({"quality": "bad", "result": "brand_new_word"}))
    assert word is None
    assert "brand_new_word" in caplog.text and "millionverifier.people.email.verify" in caplog.text


# ---- stored with the call ----------------------------------------------------------------------

async def test_a_call_stores_its_verdict(clients: AsyncClient, enrichment_on, monkeypatch):
    """The routed child and a direct call both store the word on their own CallRecord."""
    await audit.drain()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "a@sample.example", "score": 96,
                                   "verification": {"status": "accept_all"}}}),
                   (200, {"data": {"email": {"status": "accept_all", "result": "risky",
                                             "score": 50, "accept_all": True}}})]}, []))
    found = await clients.post(f"/call/{ROUTED}", json={"full_name": "A Person", "domain": "sample.example"})
    assert found.status_code == 200, found.text
    checked = await clients.get("/call/tomba.people.email.verify", params={"email": "a@sample.example"})
    assert checked.status_code == 200, checked.text
    await audit.drain()
    async with session_maker() as db:
        rows = {r.endpoint_id: r for r in (await db.execute(select(CallRecord).where(
            CallRecord.endpoint_id.in_(["tomba.people.email.find", "tomba.people.email.verify"])))).scalars()}
    assert rows["tomba.people.email.find"].hit is True
    assert rows["tomba.people.email.find"].verdict == "unverified"
    assert rows["tomba.people.email.verify"].verdict == "catch_all"
