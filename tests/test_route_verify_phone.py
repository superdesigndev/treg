"""`X-Treg-Route-Verify` on a phone find: HLR's live check (`hlrlookup.people.phone.verify`).

The check is a direct linked child `:v` built by the `people.phone.live` adapter; its hold closes
with the find's. A number not written internationally is never sent: HLR would read its first
digits as a country code."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.api import app
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import paths as P
from treg.infra.db import session_maker
from treg.models import CallRecord, Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401

FIND = "treg.people.phone.find"
HLR = "hlrlookup.people.phone.verify"
ASK = {"email": "patrick@stripe.com"}
ON = {"X-Treg-Route-Verify": "true"}


def _tomba(phone: str) -> tuple[int, dict]:
    return 200, {"data": {"email": "patrick@stripe.com", "e164_format": phone, "country_code": "US", "line_type": "MOBILE"}}


def _hlr(live_status: str, credits: float = 1, error: str = "NONE") -> tuple[int, dict]:
    return 200, {"results": [{"error": error, "credits_spent": credits, "live_status": live_status,
                              "detected_telephone_number": "14155550142", "telephone_number_type": "MOBILE",
                              "current_network_details": {"name": "Example Wireless"}, "is_ported": "NO"}]}


@pytest.fixture
def phone_on(monkeypatch, platform_on):  # noqa: F811
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA", "PLATFORM-TOMBA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP", "PLATFORM-HLR-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP_SECRET", "PLATFORM-HLR-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,hlrlookup")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


tools: list[tuple[str, str]] = []


@pytest.fixture
def quickenrich_on(monkeypatch, platform_on):  # noqa: F811
    monkeypatch.setenv("TREG_PLATFORM_KEY_QUICKENRICH", "PLATFORM-QUICKENRICH-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP", "PLATFORM-HLR-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP_SECRET", "PLATFORM-HLR-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "quickenrich,hlrlookup")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _quickenrich(phone, country) -> tuple[int, dict]:
    return 200, {"success": True, "message": "Phone found", "code": 200,
                 "data": {"employee_phone": phone, "employee_phone_type": "mobile", "country_code": country},
                 "meta": {"credits_used": 1}}


def _relay(answers: dict[str, list[tuple[int, dict]]], seen: list):
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        provider = next((p for p in ("hlrlookup", "quickenrich") if p in upstream_url), "tomba")
        tools.append((provider, tool.name))
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        seen.append((provider, json.loads(body) if body else None))
        status, doc = answers[provider].pop(0)
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)
    return relay


async def test_a_us_hit_is_checked_live_with_usa_status_and_charged_two_credits(clients: AsyncClient, phone_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [_tomba("+14155550142")], "hlrlookup": [_hlr("LIVE", 2)]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["phone"] == "+14155550142"
    hlr_body = seen[1][1]
    assert hlr_body["telephone_number"] == "14155550142" and hlr_body["usa_status"] == "YES"
    assert {k: hlr_body[k] for k in ("save_to_cache", "cache_days_global", "cache_days_private")} == {
        "save_to_cache": "PRIVATE", "cache_days_global": 0, "cache_days_private": 0}
    assert not {"get_landline_status", "get_ported_date", "get_imsi_hash"} & set(hlr_body)
    v = d["_treg"]["verification"]
    assert v["verdict"] == "live" and v["checked"] is True and v["served_by"] == HLR and v["cost_micro"] > 0
    assert "verified" not in json.dumps(v), "a phone is live or dead, never verified"
    assert "advice" not in d["_treg"]
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == before - await _balance(clients)
    assert total - v["cost_micro"] == sum(t["charged_micro"] for t in d["_treg"]["tried"])
    call_id = r.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        settled = {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}
    assert settled == {f"{call_id}:r0", f"{call_id}:v"}
    await audit.drain()
    async with session_maker() as db:
        row = (await db.execute(select(CallRecord).where(CallRecord.call_ref == f"{call_id}:v"))).scalar_one()
    assert row.endpoint_id == HLR and row.verdict == "live"


async def test_a_non_us_hit_sends_no_usa_status(clients: AsyncClient, phone_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [_tomba("+447540822872")], "hlrlookup": [_hlr("DEAD")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    assert seen[1][1]["telephone_number"] == "447540822872" and seen[1][1]["usa_status"] == "NO"
    assert r.json()["_treg"]["verification"]["verdict"] == "dead"
    assert r.json()["output"]["phone"] == "+447540822872", "a dead number is still returned"


@pytest.mark.parametrize("live_status,word,credits", [
    ("LIVE", "live", 1), ("DEAD", "dead", 1), ("ABSENT_SUBSCRIBER", "unknown", 1),
    ("NO_TELESERVICE_PROVISIONED", "unknown", 1), ("NOT_AVAILABLE_NETWORK_ONLY", "unknown", 1),
    ("INCONCLUSIVE", "unknown", 1), ("NO_COVERAGE", "unknown", 0), ("NOT_APPLICABLE", "unknown", 0),
])
async def test_every_hlr_live_status_maps_to_a_word(clients: AsyncClient, phone_on, monkeypatch, live_status, word, credits):
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [_tomba("+447540822872")], "hlrlookup": [_hlr(live_status, credits)]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    v = r.json()["_treg"]["verification"]
    assert v["verdict"] == word and v["checked"] is True
    assert (v["cost_micro"] == 0) is (credits == 0), "a free answer costs nothing"


async def test_an_hlr_error_is_no_check_and_costs_nothing(clients: AsyncClient, phone_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [_tomba("+447540822872")], "hlrlookup": [_hlr("", 0, error="INSUFFICIENT_CREDIT")]}, []))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.json()["_treg"]["verification"] == {
        "verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0, "reason": "checker_failed"}
    assert "advice" in r.json()["_treg"], "no check ran: the advice stays"
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == r.json()["_treg"]["tried"][0]["charged_micro"]


@pytest.mark.parametrize("phone", ["(415) 555-0142", "4155550142", "07790606023", "14155550142"])
async def test_a_number_not_written_internationally_is_never_sent(phone):
    async def never(child, client):
        raise AssertionError("no call may be made")
    parent = type("P", (), {})()
    parent.call_ref, parent.meta = "c1", None
    parent.input = type("I", (), {"raw_headers": (), "caller": None, "client_ip": "", "catalog_only": False})()
    check = catalog_store.load().contracts["people.phone.find"].check
    v, cost = await call_route._run_check(parent, check, {"phone": phone}, None, [], never, None)
    assert v == {"verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0,
                 "reason": "not_international"} and cost == 0


@pytest.mark.parametrize("raw,digits", [
    ("+14155550142", "14155550142"), ("+44 7790 606023", "447790606023"), ("+1 (415) 555-0142", "14155550142"),
    ("0044 7790 606023", "447790606023"), ("07790606023", None), ("4155550142", None), ("+12", None), (None, None),
])
def test_e164_digits_trusts_only_an_international_number(raw, digits):
    assert P.e164_digits(raw) == digits


@pytest.mark.parametrize("phone,country,out", [
    ("4155550142", "US", "+14155550142"), ("6135550142", "CA", "+16135550142"), (" 4155550142 ", "us", "+14155550142"),
    ("4155550142", "GB", "4155550142"), ("4155550142", None, "4155550142"), ("4155550142", "", "4155550142"),
    ("415555014", "US", "415555014"), ("14155550142", "US", "14155550142"), ("+14155550142", "US", "+14155550142"),
    ("+447540822872", "US", "+447540822872"), ("0155550142", "US", "0155550142"),
    ("415-555-0142", "US", "+14155550142"), ("(415) 555-0142", "US", "+14155550142"), ("415.555.0142", "CA", "+14155550142"),
    ("415-555-01AB", "US", "415-555-01AB"), ("(415) 555-01423", "US", "(415) 555-01423"), ("1-415-555-0142", "US", "1-415-555-0142"),
    ("+1 415 555 0142", "US", "+1 415 555 0142"), ("(015) 555-0142", "US", "(015) 555-0142"), (None, "US", None),
])
def test_with_country_code_adds_only_a_plan_it_fits(phone, country, out):
    assert P.with_country_code(phone, country) == out


@pytest.mark.parametrize("phone,country,out", [
    ("4155550142", "US", "+14155550142"), ("6135550142", "CA", "+16135550142"),
    ("4155550142", "MX", "4155550142"), ("4155550142", None, "4155550142"), ("415555014", "US", "415555014"),
    ("14155550142", "US", "14155550142"), ("+14155550142", "US", "+14155550142"), ("(415) 555-0142", "US", "+14155550142"),
])
def test_quickenrich_output_gets_plus_one_and_raw_stays(phone, country, out):
    adapter = catalog_store.load().adapters["quickenrich.people.phone.find"]
    doc = _quickenrich(phone, country)[1]
    assert adapter.from_upstream(doc)["phone"] == out
    assert doc["data"]["employee_phone"] == phone, "raw is what QuickEnrich sent"


async def test_a_quickenrich_us_hit_is_now_checked_live(clients: AsyncClient, quickenrich_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"quickenrich": [_quickenrich("4155550142", "US")], "hlrlookup": [_hlr("LIVE", 2)]}, seen))
    r = await clients.post(f"/call/{FIND}", json={"linkedin_url": "https://www.linkedin.com/in/example"}, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["phone"] == "+14155550142"
    assert d["raw"]["data"]["employee_phone"] == "4155550142", "raw is relayed as QuickEnrich sent it"
    assert seen[1][1]["telephone_number"] == "14155550142" and seen[1][1]["usa_status"] == "YES"
    assert d["_treg"]["verification"]["verdict"] == "live" and d["_treg"]["verification"]["checked"] is True


async def test_a_skipped_check_says_why_instead_of_asking_for_the_header(clients: AsyncClient, quickenrich_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({"quickenrich": [_quickenrich("4155550142", "GB")]}, []))
    r = await clients.post(f"/call/{FIND}", json={"linkedin_url": "https://www.linkedin.com/in/example"}, headers=ON)
    d = r.json()
    assert d["_treg"]["verification"]["reason"] == "not_international"
    advice = catalog_store.load().contracts["people.phone.find"].check.skip_advice
    assert d["_treg"]["advice"] == advice
    assert "X-Treg-Route-Verify" not in advice and "input_format" in advice


async def test_without_the_header_the_find_keeps_its_advice(clients: AsyncClient, quickenrich_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({"quickenrich": [_quickenrich("4155550142", "GB")]}, []))
    r = await clients.post(f"/call/{FIND}", json={"linkedin_url": "https://www.linkedin.com/in/example"})
    d = r.json()
    assert "verification" not in d["_treg"]
    assert d["_treg"]["advice"] == catalog_store.load().contracts["people.phone.find"].advice_unverified


def test_skip_advice_needs_a_when():
    from treg.domain.catalog.routing.contracts import _parse_check
    with pytest.raises(ValueError, match="skip_advice"):
        _parse_check("x", {"endpoint": "e", "field": "phone", "skip_advice": "tip"}, {"phone": {}})


def test_starts_with():
    assert P.starts_with("14155550142", "1") and not P.starts_with("447540822872", "1")
    assert not P.starts_with(None, "1")


def test_the_arena_phone_check_is_untouched():
    """Enrich Arena keeps its format check: HLR's live check is not one of its candidates."""
    from treg.domain import arena as rules
    cat = catalog_store.load()
    assert rules.VERIFICATION_TASKS["people.phone.find"] == ("people.phone.verify", "phone")
    assert HLR not in {e["id"] for e in cat.for_capability("people.phone.verify")}


async def test_the_teams_own_hlr_key_serves_the_check_unmetered(clients: AsyncClient, phone_on, monkeypatch):
    def probe(request):
        sent = json.loads(request.content)
        if "api_secret" in sent:
            return httpx.Response(200, json={"Status": "OK", "Credits": 10})
        return httpx.Response(400, json={"error": "BAD_REQUEST"})
    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        conn = (await clients.post("/connections/token", json={"provider": "hlrlookup", "token": "own-key"})).json()
        assert (await clients.post(f"/connections/{conn['id']}/extra-credential",
                                   json={"value": "own-secret"})).status_code == 200
    seen = []
    tools.clear()
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [_tomba("+447540822872")], "hlrlookup": [_hlr("LIVE")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    v = r.json()["_treg"]["verification"]
    assert v["verdict"] == "live" and v["cost_micro"] == 0, "a team's own key is never metered"
    assert tools[-1] == ("hlrlookup", "hlrlookup"), "the team's own HLR tool, not treg's key"
    assert int(r.headers["X-Treg-Cost-Micro"]) == r.json()["_treg"]["tried"][0]["charged_micro"]
