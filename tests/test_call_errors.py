"""The `call_error` line: one per failed call, its class from one table, no request or answer value."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

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
CALLER_INPUT = "7461234567890123456"
PROVIDER_ANSWER = "person-ann-lee@example.com"


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
    (429, {"refused_by": "cap", "kind": "provider_rate_limited"}, ("treg", "treg_capacity")),
    (429, {"refused_by": "cap"}, ("treg", "caller_balance_or_cap")),
    (400, {"refused_by": "request", "kind": "route_caller_fault"}, ("upstream", "caller_input")),
    (402, {"tier": "credential"}, ("upstream", "caller_provider_account")),
    (401, {"tier": "tool"}, ("upstream", "caller_provider_account")),
    (403, {"tier": "platform"}, ("upstream", "our_provider_account")),
    (402, {"tier": "anonymous"}, ("upstream", "our_provider_account")),
    (400, {"tier": "platform", "signal": "balance"}, ("upstream", "our_provider_account")),
    (400, {"tier": "credential", "signal": "balance"}, ("upstream", "caller_provider_account")),
    (400, {"tier": "tool", "signal": "unrecorded"}, ("upstream", "caller_provider_account")),
    (402, {"tier": "tool", "billed_oauth": True}, ("upstream", "our_provider_account")),
    (401, {"tier": "tool", "billed_oauth": True}, ("upstream", "caller_provider_account")),
    (429, {"tier": "platform", "signal": "burst"}, ("upstream", "rate_limit")),
    (403, {"tier": "platform", "signal": "edge_block"}, ("upstream", "upstream_error")),
    (404, {}, ("upstream", "not_found")),
    (502, {"kind": "read_timeout"}, ("upstream", "timeout")),
    (503, {}, ("upstream", "upstream_error")),
    (418, {}, ("upstream", "other")),
    (422, {"refused_by": "request", "kind": "route_no_candidate"}, ("treg", "caller_input")),
    (503, {"kind": "route_no_candidate"}, ("treg", "treg_capacity")),
    (503, {"answered": False}, ("treg", "treg_error")),
])
def test_class_table(status, kw, expected):
    assert call_errors.classify(status, **{"tier": None, **kw}) == expected


def test_the_sql_view_leaves_out_the_same_caller_side_classes():
    sql = (Path(__file__).parents[1] / "scripts/call_events_actionable_errors.sql").read_text()
    lists = re.findall(r"error_class (?:NOT )?IN \(([^)]*)\)", sql)
    assert [set(re.findall(r"'(\w+)'", found)) for found in lists] == [call_errors.CALLER_SIDE] * 2


async def test_a_balance_refusal_logs_the_caller_side_line(clients: AsyncClient, platform_on,
                                                           call_error_lines):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain", 1_000_000)
    r = await clients.get(f"/call/{EP}?aweme_id={CALLER_INPUT}")
    assert r.status_code == 402, r.text
    (line,) = call_error_lines()
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
        clients: AsyncClient, platform_on, call_error_lines, own_key, expected, caller_side):
    if own_key:
        await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    await _answering(402, {"error": "out of credits", "echo": PROVIDER_ANSWER, "id": CALLER_INPUT})
    r = await clients.get(f"/call/{EP}?aweme_id={CALLER_INPUT}")
    assert r.status_code == 402, r.text
    (line,) = call_error_lines()
    assert (line["by"], line["error_class"], line["caller_side"]) == ("upstream", expected, caller_side)
    assert line["tier"] == ("credential" if own_key else "platform")


async def test_no_request_or_answer_value_is_logged(clients: AsyncClient, platform_on,
                                                    call_error_lines, caplog):
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    await _answering(400, {"error": f"bad aweme_id {CALLER_INPUT}", "echo": PROVIDER_ANSWER})
    r = await clients.get(f"/call/{EP}?aweme_id={CALLER_INPUT}&count=5")
    assert r.status_code == 400, r.text
    (line,) = call_error_lines()
    assert line["error_class"] == "caller_input" and line["by"] == "upstream"
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == call_errors.log.name)
    for value in (CALLER_INPUT, PROVIDER_ANSWER, "OWN-KEY", "aweme_id", "bad"):
        assert value not in text


async def test_a_success_logs_nothing(clients: AsyncClient, call_error_lines):
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert call_error_lines() == []


async def test_an_unknown_endpoint_never_logs_the_name_it_was_called_by(clients: AsyncClient,
                                                                         call_error_lines):
    r = await clients.get("/call/tikhub.someone-secret-name.lookup")
    assert r.status_code == 404, r.text
    (line,) = call_error_lines()
    assert line["endpoint_id"] is None and line["by"] == "treg"
    assert line["error_class"] == "not_found"


async def test_a_missing_required_parameter_logs_its_name_and_rule(clients: AsyncClient,
                                                                    call_error_lines):
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    r = await clients.get(f"/call/{EP}?count=5")
    assert r.status_code == 400, r.text
    assert set(r.json()) == {"detail"} and isinstance(r.json()["detail"], str)
    (line,) = call_error_lines()
    assert (line["by"], line["error_class"], line["kind"]) == ("treg", "caller_input",
                                                               "catalog_parameter_invalid")
    assert (line["parameter"], line["rule"]) == ("queryParams.aweme_id", "required")


@pytest.mark.parametrize(("parameter", "rule", "logged"), [
    ("queryParams.aweme_id", "type", "queryParams.aweme_id"),
    ("headers.X-Api-Version", "required", "headers.X-Api-Version"),
    ("body", "required", "body"),
    ("body.include.ads", "required", "body.include.ads"),
    ("body.model", "undeclared", "body.model"),
    ("body.set_cookies.2", "type", "body.set_cookies.2"),
    ("body.urls.12345", "type", "undeclared"),
    ("body.15551234567", "undeclared", "undeclared"),
    ("body.set_cookies.15551234567", "undeclared", "undeclared"),
    ("queryParams.someone@example.com", "type", "undeclared"),
    ("queryParams.aweme_id.someone@example.com", "type", "undeclared"),
    ("someone@example.com", "type", "undeclared"),
    ("body.token_0123456789abcdef0123456789abcdef0123456789ab", "undeclared", "undeclared"),
])
def test_a_refused_parameter_is_named_only_when_every_part_looks_like_a_name(
        call_error_lines, parameter, rule, logged):
    failure = ResolutionFailed("catalog_parameter_invalid", status_code=400, parameter=parameter,
                               rule=rule, detail=CALLER_INPUT)
    call_errors.emit(400, org_id=1, call_ref="x", road="catalog", endpoint_id=EP,
                     provider="p", tier="platform", refused_by="request", failure=failure,
                     answered=False)
    (line,) = call_error_lines()
    assert line["parameter"] == logged


def test_a_parameter_in_the_refusal_detail_is_read_and_list_values_are_not(call_error_lines):
    for parameter in ("queryParams.count", ["body.a", "body.b"]):
        failure = ResolutionFailed("catalog_parameter_invalid", status_code=400, rule="platform",
                                   detail={"parameter": parameter})
        call_errors.emit(400, org_id=1, call_ref="x", road="catalog", endpoint_id=EP,
                         provider="tikhub", tier="platform", refused_by="request", failure=failure,
                         answered=False)
    named, listed = call_error_lines()
    assert (named["parameter"], named["rule"]) == ("queryParams.count", "platform")
    assert (listed["parameter"], listed["rule"]) == (None, "platform")


def test_operator_flow_control_is_not_a_spend_cap(call_error_lines):
    call_errors.emit(429, org_id=1, call_ref="x", road="unresolved", refused_by="cap",
                     failure=AdmissionRejected(retry_after_s=1, version=1), answered=False)
    (line,) = call_error_lines()
    assert (line["kind"], line["error_class"], line["caller_side"]) == (
        "org_concurrency_limit", "treg_policy", False)


async def test_a_bad_treg_token_is_a_caller_side_auth_error(clients: AsyncClient, call_error_lines):
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Token": "nope"})
    assert r.status_code == 401, r.text
    (line,) = call_error_lines()
    assert (line["error_class"], line["caller_side"], line["by"]) == ("caller_auth", True, "treg")


@pytest.mark.parametrize("encoding", ["", "gzip"])
def test_an_own_key_out_of_credit_answer_is_read_whatever_its_encoding(call_error_lines, encoding):
    answer = json.dumps({"error": "You don't have enough credits", "echo": PROVIDER_ANSWER}).encode()
    headers = [(b"content-type", b"application/json")]
    if encoding:
        answer = gzip.compress(answer)
        headers.append((b"content-encoding", encoding.encode()))
    call_errors.emit(400, org_id=1, call_ref="abc", road="catalog", endpoint_id=EP,
                     provider="tikhub", tier="credential", body=answer, headers=headers)
    (line,) = call_error_lines()
    assert (line["signal"], line["error_class"], line["caller_side"]) == (
        "unrecorded", "caller_provider_account", True)
    assert PROVIDER_ANSWER not in json.dumps(line)


def test_an_own_tool_out_of_credit_answer_is_read_without_a_provider(call_error_lines):
    answer = json.dumps({"error": "You don't have enough credits", "echo": PROVIDER_ANSWER}).encode()
    call_errors.emit(400, org_id=1, call_ref="abc", road="own_tool", provider=None, tier="tool",
                     body=answer, headers=[(b"content-type", b"application/json")])
    (line,) = call_error_lines()
    assert (line["signal"], line["error_class"], line["caller_side"]) == (
        "unrecorded", "caller_provider_account", True)
    assert PROVIDER_ANSWER not in json.dumps(line)


@pytest.mark.parametrize(("base_url", "signal", "expected", "caller_side"), [
    ("https://api.hunter.io/v2", "quota", "caller_provider_account", True),
    ("https://api.example.com/v2", "unknown", "rate_limit", False),
])
async def test_an_own_tool_answer_is_read_with_the_codes_of_its_hosts_provider(
        clients: AsyncClient, call_error_lines, base_url, signal, expected, caller_side):
    sid = (await clients.post("/secrets", json={"name": "k", "value": "OWN-KEY"})).json()["id"]
    r = await clients.post("/tools", json={"name": "mine", "base_url": base_url, "secret_id": sid})
    assert r.status_code < 300, r.text
    await _answering(429, {"errors": [{"details": "Too many calls per billing period"}],
                           "echo": PROVIDER_ANSWER})
    r = await clients.get("/call/mine/account")
    assert r.status_code == 429, r.text
    (line,) = call_error_lines()
    assert (line["road"], line["provider"], line["tier"]) == ("own_tool", None, "tool")
    assert (line["signal"], line["error_class"], line["caller_side"]) == (signal, expected, caller_side)


@pytest.mark.parametrize(("target", "kind", "expected", "caller_side"), [
    (f"{EP}?aweme_id=7", "platform_key_missing", "our_provider_account", False),
    ("anymailfinder.account", "credential_missing", "caller_provider_account", True),
])
async def test_a_missing_credential_is_the_account_of_whoever_should_hold_the_key(
        clients: AsyncClient, monkeypatch, call_error_lines, target, kind, expected, caller_side):
    refusals = []
    emit = call_errors.emit

    def keep(*args, failure=None, **kw):
        refusals.append(failure)
        emit(*args, failure=failure, **kw)

    monkeypatch.setattr(call_errors, "emit", keep)
    r = await clients.get(f"/call/{target}")
    assert r.status_code == 404, r.text
    assert "platform_key_missing" not in r.text
    (line,) = call_error_lines()
    assert (line["kind"], line["by"], line["error_class"], line["caller_side"]) == (
        kind, "treg", expected, caller_side)
    assert [failure.kind for failure in refusals] == ["credential_missing"]


def test_a_routed_child_carries_its_step(call_error_lines):
    call_errors.emit(429, org_id=1, call_ref="abc:r2", road="catalog", endpoint_id=EP,
                     provider="tikhub", tier="platform")
    (line,) = call_error_lines()
    assert line["step"] == "r2" and line["error_class"] == "rate_limit"


async def test_a_routed_refusal_logs_one_parent_line(clients: AsyncClient, call_error_lines):
    r = await clients.post("/call/treg.people.phone.find", json={"nickname": CALLER_INPUT})
    assert r.status_code >= 400, r.text
    (line,) = call_error_lines()
    assert (line["road"], line["tier"], line["provider"], line["by"]) == ("routed", "routed", "treg", "treg")
    assert line["endpoint_id"] == "treg.people.phone.find" and line["error_class"] == "caller_input"


async def test_a_routed_child_refused_before_its_audit_logs_its_own_line(
        clients: AsyncClient, monkeypatch, call_error_lines):
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
    children = [line for line in call_error_lines() if line["step"]]
    assert children and children[0]["kind"] == "policy_denied"
    assert (children[0]["by"], children[0]["road"], children[0]["endpoint_id"]) == (
        "treg", "catalog", "tavily.web.search")
    assert [line["step"] for line in call_error_lines()].count(None) == 1


async def test_a_pin_mismatch_is_the_callers_input_not_a_policy_refusal(clients: AsyncClient,
                                                                        call_error_lines):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    agent = await clients.post(f"/orgs/{org_id}/agents", json={
        "name": "pinned", "role": "member", "pinned_tags": {"customer": "a"}})
    assert agent.status_code == 200, agent.text
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={
        "X-Treg-Token": agent.json()["token"], "X-Treg-Meta": "customer=b"})
    assert r.status_code == 403, r.text
    (line,) = call_error_lines()
    assert (line["kind"], line["error_class"], line["caller_side"]) == (
        "metadata_pin_mismatch", "caller_input", False)


def _crashing_child(monkeypatch, raised: Exception):
    """`execute_call` with its body raising `raised`, and a context for a call ref."""
    from types import SimpleNamespace
    from treg.application.call import service as call_service

    async def crash(request, client):
        raise raised

    monkeypatch.setattr(call_service, "_execute_call", crash)

    def context(ref, with_caller=True):
        body = SimpleNamespace(read=None, stream=None)
        call_input = SimpleNamespace(method="GET", raw_rest="", raw_query="", raw_headers=(),
                                     query_items=(), body=body, client_ip="",
                                     caller=SimpleNamespace(org_id=7) if with_caller else None)
        return SimpleNamespace(input=call_input, call_ref=ref, marketplace=None, idempotency=None,
                               audited=False, cost_micro=None)

    return call_service.execute_call, context


@pytest.mark.parametrize(("raised", "status", "kind", "cls"), [
    (RuntimeError("boom"), 500, "unexpected_exception", "treg_error"),
    (__import__("sqlalchemy.exc").exc.TimeoutError("pool"), 503, "db_pool", "treg_capacity"),
])
async def test_a_child_that_crashes_before_its_audit_logs_its_line(
        monkeypatch, call_error_lines, raised, status, kind, cls):
    execute_call, context = _crashing_child(monkeypatch, raised)
    for ref in ("root", "root:v"):
        with pytest.raises(type(raised)):
            await execute_call(context(ref), None)
    (line,) = call_error_lines()
    assert (line["step"], line["status"], line["kind"], line["error_class"], line["by"]) == (
        "v", status, kind, cls, "treg")


async def test_a_child_whose_line_cannot_be_built_still_raises_its_own_failure(monkeypatch):
    execute_call, context = _crashing_child(monkeypatch, RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await execute_call(context("root:v", with_caller=False), None)
