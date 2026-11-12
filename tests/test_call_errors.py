"""The `call_error` line: one per failed call, its class from one table, no request or answer value."""

from __future__ import annotations

import gzip
import json
import logging

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A
from treg.application.call import call_errors
from treg.application.call.types import ResolutionFailed
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import session_maker
from treg.infra.org_concurrency import AdmissionRejected

EP = "tikhub.tiktok.video.comments"
SECRET_INPUT = "7461234567890123456"
SECRET_ANSWER = "person-ann-lee@example.com"


@pytest.fixture
def lines(caplog):
    caplog.set_level(logging.INFO, logger=call_errors.log.name)
    call_errors.log.addHandler(caplog.handler)
    try:
        yield lambda: [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
                       if r.name == call_errors.log.name and r.getMessage().startswith("call_error ")]
    finally:
        call_errors.log.removeHandler(caplog.handler)


@pytest.fixture
def platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _upstream(status: int, doc: dict):
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(doc).encode()),
                              headers={"content-type": "application/json"})
    return answer


async def _answering(status: int, doc: dict) -> None:
    await A.app.state.http.aclose()
    A.app.state.http = AsyncClient(transport=httpx.MockTransport(_upstream(status, doc)))


@pytest.mark.parametrize(("status", "kw", "expected"), [
    (400, {"refused_by": "request", "kind": "catalog_parameter_invalid"}, ("treg", "caller_input")),
    (400, {}, ("upstream", "caller_input")),
    (422, {}, ("upstream", "caller_input")),
    (402, {"refused_by": "balance", "kind": "insufficient_balance"}, ("treg", "caller_balance_or_cap")),
    (429, {"refused_by": "cap", "kind": "daily_cap_reached"}, ("treg", "caller_balance_or_cap")),
    (429, {"refused_by": "cap", "kind": "provider_rate_limited"}, ("treg", "treg_capacity")),
    (429, {"refused_by": "cap"}, ("treg", "caller_balance_or_cap")),
    (403, {"refused_by": "policy"}, ("treg", "caller_policy")),
    (403, {"refused_by": "policy", "kind": "tool_access_denied"}, ("treg", "caller_policy")),
    (403, {"refused_by": "policy", "kind": "org_endpoint_quota_exceeded"}, ("treg", "treg_policy")),
    (410, {"refused_by": "retired", "kind": "platform_account_retired"}, ("treg", "our_provider_account")),
    (410, {"refused_by": "retired", "kind": "catalog_retired"}, ("treg", "treg_policy")),
    (503, {"refused_by": "paused"}, ("treg", "treg_policy")),
    (503, {"kind": "provider_paused"}, ("treg", "treg_policy")),
    (429, {"refused_by": "capacity", "kind": "share_busy"}, ("treg", "treg_policy")),
    (409, {"refused_by": "request", "kind": "idempotency_in_progress"}, ("treg", "treg_capacity")),
    (429, {"refused_by": "cap", "kind": "org_concurrency_limit"}, ("treg", "treg_policy")),
    (429, {"refused_by": "cap", "kind": "org_traffic_paused"}, ("treg", "treg_policy")),
    (401, {"refused_by": "auth"}, ("treg", "caller_auth")),
    (400, {"refused_by": "request", "kind": "route_caller_fault"}, ("upstream", "caller_input")),
    (404, {"refused_by": "resolution", "kind": "route_not_found"}, ("upstream", "not_found")),
    (503, {"refused_by": "capacity", "kind": "provider_capacity"}, ("treg", "treg_capacity")),
    (402, {"tier": "credential"}, ("upstream", "caller_provider_account")),
    (401, {"tier": "tool"}, ("upstream", "caller_provider_account")),
    (403, {"tier": "platform"}, ("upstream", "our_provider_account")),
    (402, {"tier": "anonymous"}, ("upstream", "our_provider_account")),
    (400, {"tier": "platform", "signal": "balance"}, ("upstream", "our_provider_account")),
    (400, {"tier": "credential", "signal": "balance"}, ("upstream", "caller_provider_account")),
    (400, {"tier": "tool", "signal": "unrecorded"}, ("upstream", "caller_provider_account")),
    (400, {"tier": "platform", "signal": "unrecorded"}, ("upstream", "our_provider_account")),
    (402, {"tier": "tool", "billed_oauth": True}, ("upstream", "our_provider_account")),
    (400, {"tier": "tool", "billed_oauth": True, "signal": "quota"}, ("upstream", "our_provider_account")),
    (401, {"tier": "tool", "billed_oauth": True}, ("upstream", "caller_provider_account")),
    (429, {"tier": "platform", "signal": "burst"}, ("upstream", "rate_limit")),
    (403, {"tier": "platform", "signal": "edge_block"}, ("upstream", "upstream_error")),
    (404, {}, ("upstream", "not_found")),
    (429, {}, ("upstream", "rate_limit")),
    (502, {"kind": "read_timeout"}, ("upstream", "timeout")),
    (502, {"kind": "connect_failed"}, ("upstream", "upstream_error")),
    (503, {}, ("upstream", "upstream_error")),
    (418, {}, ("upstream", "other")),
    (502, {"kind": "injection_failed"}, ("treg", "treg_error")),
    (500, {"kind": "unexpected_exception"}, ("treg", "treg_error")),
    (422, {"refused_by": "request", "kind": "route_no_candidate"}, ("treg", "caller_input")),
    (503, {"kind": "route_no_candidate"}, ("treg", "treg_capacity")),
    (503, {"answered": False}, ("treg", "treg_error")),
    (409, {"kind": "credential_missing"}, ("treg", "caller_provider_account")),
])
def test_class_table(status, kw, expected):
    assert call_errors.classify(status, **{"tier": None, **kw}) == expected


def test_caller_side_is_the_classes_a_default_view_leaves_out():
    assert call_errors.CALLER_SIDE == {"caller_balance_or_cap", "caller_policy", "caller_provider_account"}


async def test_a_balance_refusal_logs_the_caller_side_line(clients: AsyncClient, platform_on, lines):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain", 1_000_000)
    r = await clients.get(f"/call/{EP}?aweme_id={SECRET_INPUT}")
    assert r.status_code == 402, r.text
    (line,) = lines()
    assert line == {
        "org_id": org_id, "call_ref": r.headers["X-Treg-Call-Id"], "step": None, "road": "catalog",
        "endpoint_id": EP, "provider": "tikhub", "tier": "platform", "status": 402, "by": "treg",
        "refused_by": "balance", "error_class": "caller_balance_or_cap", "caller_side": True,
        "kind": "insufficient_balance", "signal": None, "parameter": None, "rule": None,
    }


@pytest.mark.parametrize(("own_key", "expected", "caller_side"), [
    (True, "caller_provider_account", True),
    (False, "our_provider_account", False),
])
async def test_an_upstream_402_is_the_account_of_whoever_owns_the_key(
        clients: AsyncClient, platform_on, lines, own_key, expected, caller_side):
    if own_key:
        await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    await _answering(402, {"error": "out of credits", "echo": SECRET_ANSWER, "id": SECRET_INPUT})
    r = await clients.get(f"/call/{EP}?aweme_id={SECRET_INPUT}")
    assert r.status_code == 402, r.text
    (line,) = lines()
    assert (line["by"], line["error_class"], line["caller_side"]) == ("upstream", expected, caller_side)
    assert line["tier"] == ("credential" if own_key else "platform")


async def test_no_request_or_answer_value_is_logged(clients: AsyncClient, platform_on, lines, caplog):
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    await _answering(400, {"error": f"bad aweme_id {SECRET_INPUT}", "echo": SECRET_ANSWER})
    r = await clients.get(f"/call/{EP}?aweme_id={SECRET_INPUT}&count=5")
    assert r.status_code == 400, r.text
    (line,) = lines()
    assert line["error_class"] == "caller_input" and line["by"] == "upstream"
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == call_errors.log.name)
    for value in (SECRET_INPUT, SECRET_ANSWER, "OWN-KEY", "aweme_id", "bad"):
        assert value not in text


async def test_a_success_logs_nothing(clients: AsyncClient, lines):
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert lines() == []


async def test_an_unknown_endpoint_never_logs_the_name_it_was_called_by(clients: AsyncClient, lines):
    r = await clients.get("/call/tikhub.someone-secret-name.lookup")
    assert r.status_code == 404, r.text
    (line,) = lines()
    assert line["endpoint_id"] is None and line["by"] == "treg"
    assert line["error_class"] == "not_found"


async def test_a_missing_required_parameter_logs_its_name_and_rule(clients: AsyncClient, lines):
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    r = await clients.get(f"/call/{EP}?count=5")
    assert r.status_code == 400, r.text
    assert set(r.json()) == {"detail"} and isinstance(r.json()["detail"], str)
    (line,) = lines()
    assert (line["by"], line["error_class"], line["kind"]) == ("treg", "caller_input",
                                                               "catalog_parameter_invalid")
    assert (line["parameter"], line["rule"]) == ("queryParams.aweme_id", "required")


def test_a_refused_parameter_is_named_only_when_declared(lines):
    for parameter in ("queryParams.aweme_id", "queryParams.someone@example.com", "aweme_id",
                      "query.count", "someone@example.com", "queryParams.aweme_id.someone@example.com"):
        failure = ResolutionFailed("catalog_parameter_invalid", status_code=400, parameter=parameter,
                                   rule="type", detail=SECRET_INPUT)
        call_errors.emit(400, org_id=1, call_ref="x", road="catalog", endpoint_id=EP,
                         provider="tikhub", tier="platform", refused_by="request", failure=failure,
                         answered=False)
    assert [line["parameter"] for line in lines()] == [
        "queryParams.aweme_id", "undeclared", "aweme_id", "query.count", "undeclared", "undeclared"]


@pytest.mark.parametrize(("endpoint_id", "parameter", "logged"), [
    ("coingecko.coins.get", "pathParams.coin_id", "pathParams.coin_id"),
    ("coingecko.coins.get", "pathParams.coin_id.someone@example.com", "undeclared"),
    ("cloro.ai-search.chatgpt.shopping", "body.include.ads", "body.include.ads"),
    ("cloro.ai-search.chatgpt.shopping", "body.include.someone@example.com", "undeclared"),
    ("cloro.ai-search.chatgpt.shopping", "body.include.ads.someone@example.com", "undeclared"),
    ("cloro.ai-search.chatgpt.shopping", "body.include.0", "undeclared"),
    ("anyapi.google.serp.maps", "body.categoryFilterWords.0", "body.categoryFilterWords.0"),
    ("cloro.ai-search.chatgpt.shopping", "body.15551234567", "undeclared"),
    ("cloro.ai-search.chatgpt.shopping", "body.include.ads.15551234567", "undeclared"),
    ("coingecko.coins.get", "queryParams.15551234567", "undeclared"),
    ("cloro.ai-search.chatgpt.shopping", "body", "body"),
])
def test_every_part_of_a_logged_parameter_is_declared(lines, endpoint_id, parameter, logged):
    failure = ResolutionFailed("catalog_parameter_invalid", status_code=400, parameter=parameter,
                               rule="required", detail=SECRET_INPUT)
    call_errors.emit(400, org_id=1, call_ref="x", road="catalog", endpoint_id=endpoint_id,
                     provider="p", tier="platform", refused_by="request", failure=failure,
                     answered=False)
    (line,) = lines()
    assert line["parameter"] == logged


def test_a_parameter_in_the_refusal_detail_is_read_and_list_values_are_not(lines):
    for parameter in ("queryParams.count", ["body.a", "body.b"]):
        failure = ResolutionFailed("catalog_parameter_invalid", status_code=400, rule="platform",
                                   detail={"parameter": parameter})
        call_errors.emit(400, org_id=1, call_ref="x", road="catalog", endpoint_id=EP,
                         provider="tikhub", tier="platform", refused_by="request", failure=failure,
                         answered=False)
    named, listed = lines()
    assert (named["parameter"], named["rule"]) == ("queryParams.count", "platform")
    assert (listed["parameter"], listed["rule"]) == (None, "platform")


def test_operator_flow_control_is_not_a_spend_cap(lines):
    call_errors.emit(429, org_id=1, call_ref="x", road="unresolved", refused_by="cap",
                     failure=AdmissionRejected(retry_after_s=1, version=1), answered=False)
    (line,) = lines()
    assert (line["kind"], line["error_class"], line["caller_side"]) == (
        "org_concurrency_limit", "treg_policy", False)


async def test_a_bad_treg_token_is_its_own_class(clients: AsyncClient, lines):
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Token": "nope"})
    assert r.status_code == 401, r.text
    (line,) = lines()
    assert (line["error_class"], line["caller_side"], line["by"]) == ("caller_auth", False, "treg")


@pytest.mark.parametrize("encoding", ["", "gzip"])
def test_an_own_key_out_of_credit_answer_is_read_whatever_its_encoding(lines, encoding):
    answer = json.dumps({"error": "You don't have enough credits", "echo": SECRET_ANSWER}).encode()
    headers = [(b"content-type", b"application/json")]
    if encoding:
        answer = gzip.compress(answer)
        headers.append((b"content-encoding", encoding.encode()))
    call_errors.emit(400, org_id=1, call_ref="abc", road="catalog", endpoint_id=EP,
                     provider="tikhub", tier="credential", body=answer, headers=headers)
    (line,) = lines()
    assert (line["signal"], line["error_class"], line["caller_side"]) == (
        "unrecorded", "caller_provider_account", True)
    assert SECRET_ANSWER not in json.dumps(line)


def test_a_routed_child_carries_its_step(lines):
    call_errors.emit(429, org_id=1, call_ref="abc:r2", road="catalog", endpoint_id=EP,
                     provider="tikhub", tier="platform")
    (line,) = lines()
    assert line["step"] == "r2" and line["error_class"] == "rate_limit"


async def test_a_routed_refusal_logs_one_parent_line(clients: AsyncClient, lines):
    r = await clients.post("/call/treg.people.phone.find", json={"nickname": SECRET_INPUT})
    assert r.status_code >= 400, r.text
    (line,) = lines()
    assert (line["road"], line["tier"], line["provider"], line["by"]) == ("routed", "routed", "treg", "treg")
    assert line["endpoint_id"] == "treg.people.phone.find" and line["error_class"] == "caller_input"


@pytest.mark.parametrize(("parameter", "rule", "logged"), [
    ("body.set_cookies.15551234567", "undeclared", "undeclared"),
    ("body.set_cookies.15551234567", "repeat", "undeclared"),
    ("body.set_cookies.2", "type", "body.set_cookies.2"),
])
def test_a_dotted_key_the_caller_made_up_is_never_read_as_a_path(lines, parameter, rule, logged):
    failure = ResolutionFailed("catalog_parameter_invalid", status_code=400, parameter=parameter,
                               rule=rule, detail=SECRET_INPUT)
    call_errors.emit(400, org_id=1, call_ref="x", road="catalog", endpoint_id="litescrape.web.fetch.post",
                     provider="litescrape", tier="platform", refused_by="request", failure=failure,
                     answered=False)
    (line,) = lines()
    assert line["parameter"] == logged


async def test_a_routed_child_refused_before_its_audit_logs_its_own_line(
        clients: AsyncClient, monkeypatch, lines):
    from treg.application.call import service as call_service
    from treg.application.call.types import AuthorizationFailed
    monkeypatch.setenv("TREG_PLATFORM_KEY_TAVILY", "PLATFORM-TAVILY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily")
    get_settings.cache_clear()

    async def deny(**kw):
        raise AuthorizationFailed("policy_denied", status_code=403, detail="denied")

    monkeypatch.setattr(call_service, "authorize_call", deny)
    r = await clients.post("/call/treg.web.search", json={"q": "example query", "limit": 3},
                           headers={"X-Treg-Route-Prefer": "tavily"})
    assert r.status_code >= 400, r.text
    children = [line for line in lines() if line["step"]]
    assert children and children[0]["kind"] == "policy_denied"
    assert (children[0]["by"], children[0]["road"], children[0]["endpoint_id"]) == (
        "treg", "catalog", "tavily.web.search")
    assert [line["step"] for line in lines()].count(None) == 1
