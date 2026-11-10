"""The provider's reported charge: settled from the answer, and, with `strip_reported_charge` on,
removed from what the caller receives unless the call sends `X-Treg-Keep-Reported-Charge`."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import api as A, archive, audit
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.reported_charge import path_of, without
from treg.infra.db import session_maker
from treg.models import ArchiveSnapshot

EP = "scrapecreators.x.v1-facebook-group"   # v2 row: settle.reported response.credits_charged
EP_CREDIT_MICRO = 1_880                     # one ScrapeCreators credit, raw
KEEP = "X-Treg-Keep-Reported-Charge"


@pytest.mark.parametrize("path,body,expected", [
    ("response.cost", b'{"data":[1],"cost":0.0025,"status":"ok"}', b'{"data":[1],"status":"ok"}'),
    ("response.cost", b'{"cost":0.0025,"data":[1]}', b'{"data":[1]}'),
    ("response.cost", b'{"data":[1],"cost":0.0025}', b'{"data":[1]}'),
    ("response.cost", b'{"cost":0.0025}', b'{}'),
    # Every other byte stays: indentation, separators, escapes and number spellings.
    ("response.cost", b'{\n  "name": "Caf\\u00e9",\n  "cost": 1.50,\n  "n": 1e3\n}',
     b'{\n  "name": "Caf\\u00e9",\n  "n": 1e3\n}'),
    ("response.cost", b'{ "a" : 1 ,\n "cost" : 2 }', b'{ "a" : 1 }'),
    ("response.billing.creditsCharged", b'{"billing":{"creditsCharged":2,"plan":"x"},"id":7}',
     b'{"billing":{"plan":"x"},"id":7}'),
    ("response.results.0.credits_spent", b'{"results":[{"credits_spent":1,"live":"YES"},{"credits_spent":1}]}',
     b'{"results":[{"live":"YES"},{"credits_spent":1}]}'),
    ("response.tasks.*.cost", b'[1]', b'[1]'),
    ("response.*.cost", b'[{"cost":1,"id":"a"},{"id":"b"},{"id":"c","cost":2}]',
     b'[{"id":"a"},{"id":"b"},{"id":"c"}]'),
    ("response.cost", b'[{"cost":1}]', b'[{"cost":1}]'),
    # A repeated key is removed every time it appears, so no earlier copy is left to read.
    ("response.cost", b'{"cost":1,"a":2,"cost":3}', b'{"a":2}'),
])
def test_without_removes_only_the_named_member(path, body, expected):
    assert without(path, body) == expected
    assert json.loads(without(path, body)) is not None


@pytest.mark.parametrize("path,body", [
    ("response.cost", b'{"data":[],"status":"ok"}'),        # the path selects nothing
    ("response.cost.total", b'{"cost":7}'),
    ("response.cost", b"<html>busy</html>"),                 # not JSON
    ("response.cost", b'{"cost":1'),                         # not one whole document
    ("response.cost", b'\xef\xbb\xbf{"cost":1}'),            # not UTF-8 JSON as json.loads(str) reads it
    ("response.cost", b'\xff\xfe{\x00}\x00'),
    ("response.cost", b""),                                  # a 204
    (None, b'{"cost":1}'),                                   # the row reads no charge from the answer
    ("response.costs.0", b'{"costs":[1]}'),                  # a list item is not a member
    ("response.cost[a=b]", b'{"cost":[{"a":"b"}]}'),
    ("response", b'1.5'),
    ("body.cost", b'{"cost":1}'),
    ("response.cost..x", b'{"cost":1}'),                     # not a path
])
def test_without_returns_the_same_bytes_when_there_is_nothing_to_remove(path, body):
    assert without(path, body) is body


def test_the_path_is_the_one_the_settlement_reads_in_either_spelling():
    by_id = catalog_store.load().by_id
    assert path_of(by_id[EP]) == "response.credits_charged"
    assert path_of(by_id["hlrlookup.people.phone.verify"]) == "response.results.0.credits_spent"
    assert path_of({"cost": {"reported_charge": {"path": "costUsd", "unit": "usd"}}}) == "response.costUsd"
    assert path_of({"bill": {}, "settle": {"reported": {"header": "X-Credits", "unit": "credit"}}}) is None
    assert path_of({"bill": {}, "settle": {"count": {"path": "response.data"}}}) is None
    assert path_of({"cost": {"type": "per_call", "value": 0.01}}) is None
    assert path_of(None) is None


@pytest.fixture
def platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_SCRAPECREATORS", "PLATFORM-SC-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP", "PLATFORM-HLR-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP_SECRET", "PLATFORM-HLR-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "scrapecreators,hlrlookup")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def strip_on(platform_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "strip_reported_charge", True)


def _vendor(monkeypatch, status: int, body: bytes) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, stream=httpx.ByteStream(body),
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(A.app.state, "http", AsyncClient(transport=httpx.MockTransport(upstream)))
    return seen


async def _balance(clients: AsyncClient) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


ANSWER = b'{"success":true,"credits_charged":2,"group":{"id":"366190054572553","name":"Caf\\u00e9"}}'
SERVED = b'{"success":true,"group":{"id":"366190054572553","name":"Caf\\u00e9"}}'


async def _call(clients: AsyncClient, headers: dict | None = None) -> httpx.Response:
    return await clients.get(f"/call/{EP}", params={"group_id": "366190054572553"}, headers=headers)


async def test_off_by_default_the_answer_is_the_providers(clients, platform_on, monkeypatch):
    assert get_settings().strip_reported_charge is False
    _vendor(monkeypatch, 200, ANSWER)
    before = await _balance(clients)
    response = await _call(clients)
    assert response.status_code == 200, response.text
    assert response.content == ANSWER
    assert before - await _balance(clients) == 2 * EP_CREDIT_MICRO


async def test_on_the_charge_is_removed_after_it_is_billed(clients, strip_on, monkeypatch):
    seen = _vendor(monkeypatch, 200, ANSWER)
    before = await _balance(clients)
    response = await _call(clients)
    assert response.status_code == 200, response.text
    assert response.content == SERVED
    assert response.headers["content-length"] == str(len(SERVED))
    assert before - await _balance(clients) == 2 * EP_CREDIT_MICRO
    assert response.headers["X-Treg-Cost-Micro"] == str(2 * EP_CREDIT_MICRO)
    assert KEEP.lower() not in {k.lower() for k in seen[0].headers}
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["cost_observed_micro"] == 2 * EP_CREDIT_MICRO


async def test_on_a_call_that_asks_keeps_the_charge(clients, strip_on, monkeypatch):
    seen = _vendor(monkeypatch, 200, ANSWER)
    before = await _balance(clients)
    response = await _call(clients, headers={KEEP: "true"})
    assert response.status_code == 200, response.text
    assert response.content == ANSWER
    assert before - await _balance(clients) == 2 * EP_CREDIT_MICRO
    assert KEEP.lower() not in {k.lower() for k in seen[0].headers}  # treg's header, never relayed


async def test_on_an_error_body_is_relayed_as_it_came(clients, strip_on, monkeypatch):
    body = b'{"success":false,"error":"not_found","credits_charged":0}'
    _vendor(monkeypatch, 404, body)
    response = await _call(clients)
    assert response.status_code == 404
    assert response.content == body


async def test_on_a_row_in_todays_spelling_loses_the_member_its_settle_reads(clients, strip_on, monkeypatch):
    answer = b'{"results":[{"error":"NONE","credits_spent":1,"live_status":"LIVE"}]}'
    _vendor(monkeypatch, 200, answer)
    before = await _balance(clients)
    response = await clients.post("/call/hlrlookup.people.phone.verify", json={
        "telephone_number": "447540822872", "save_to_cache": "PRIVATE",
        "cache_days_global": 0, "cache_days_private": 0})
    assert response.status_code == 200, response.text
    assert response.content == b'{"results":[{"error":"NONE","live_status":"LIVE"}]}'
    assert before - await _balance(clients) == 6_608   # one credit, read before it was removed


async def test_a_replay_hands_back_the_answer_the_first_call_received(clients, strip_on, monkeypatch):
    seen = _vendor(monkeypatch, 200, ANSWER)
    first = await _call(clients, headers={"Idempotency-Key": "k-1"})
    again = await _call(clients, headers={"Idempotency-Key": "k-1", KEEP: "true"})
    assert first.content == again.content == SERVED
    assert again.headers["X-Treg-Idempotent-Replay"] == "true"
    assert len(seen) == 1


@pytest.fixture
def serve(strip_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")


async def test_an_archive_hit_loses_the_charge_and_the_archive_keeps_it(clients, serve, monkeypatch):
    seen = _vendor(monkeypatch, 200, ANSWER)
    live = await _call(clients)
    assert live.status_code == 200 and live.content == SERVED
    await archive.drain()
    before = await _balance(clients)
    hit = await _call(clients)
    assert hit.status_code == 200, hit.text
    assert hit.headers["X-Treg-Cache"] == "hit"
    assert hit.content == SERVED
    assert hit.headers["content-length"] == str(len(SERVED))
    assert len(seen) == 1
    # The repeat settles from the stored answer's own two credits, at the repeat price.
    assert int(hit.headers["X-Treg-Cost-Micro"]) == 2 * EP_CREDIT_MICRO * 10 // 100
    assert before - await _balance(clients) == int(hit.headers["X-Treg-Cost-Micro"])
    kept = await _call(clients, headers={KEEP: "1"})
    assert kept.headers["X-Treg-Cache"] == "hit" and kept.content == ANSWER
    async with session_maker() as s:
        hashes = [snap.content_hash for snap in (await s.execute(select(ArchiveSnapshot))).scalars()]
    assert hashes == [archive.content_hash(ANSWER)]          # the provider's bytes, whole

    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    result = (await clients.get(f"/calls/{row['id']}/result")).json()
    assert result["stored"] is True
    assert result["response"]["body_text"] == SERVED.decode()
    asked = (await clients.get(f"/calls/{row['id']}/result", headers={KEEP: "true"})).json()
    assert asked["response"]["body_text"] == ANSWER.decode()
