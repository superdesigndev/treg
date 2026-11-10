"""treg's own provider account refusing a call for want of credit is not the caller's balance and
not theirs to fix. `/call/` relays the provider's answer untouched and says so in
`X-Treg-Capacity: exhausted`; MCP and the CLI turn that into one notice without the provider's
wallet details. A team's own key running dry, and treg's own balance refusal, stay what they were."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from treg import cli
from treg.application.call import service as call_service
from treg.capacity_notice import account_out_notice

from test_marketplace_call import EP, _fake_relay, platform_on  # noqa: F401

OUT = b'{"detail":"Insufficient balance","topup":"https://vendor.example/add_credit"}'
NOTICE = ("Sorry, this tool is temporarily unavailable on treg's side. We've been notified and "
          "will restore it shortly. You were not charged. You can try another tool meanwhile.")


def test_the_notice_says_not_charged_only_on_a_zero_charge():
    out = {"X-Treg-Capacity": "exhausted"}
    assert account_out_notice({**out, "X-Treg-Cost-Micro": "0"}) == NOTICE
    assert "not charged" not in account_out_notice(out)
    assert "not charged" not in account_out_notice({**out, "X-Treg-Cost-Micro": "1000"})
    assert account_out_notice({"X-Treg-Cost-Micro": "0"}) is None


async def test_an_out_of_credit_platform_answer_is_relayed_untouched_and_flagged(clients, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.content == OUT, "the body stays the provider's"
    assert r.headers["x-treg-capacity"] == "exhausted"
    assert r.headers["x-treg-cost-micro"] == "0"
    assert "x-treg-error" not in r.headers


async def test_an_ordinary_provider_error_is_not_flagged(clients, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(400, b'{"detail":"bad aweme_id"}'))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400 and "x-treg-capacity" not in r.headers


async def test_a_teams_own_key_running_dry_is_not_flagged(clients, platform_on, monkeypatch):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.content == OUT
    assert "x-treg-capacity" not in r.headers


async def _mcp_call(clients, monkeypatch) -> dict:
    from test_mcp import _call_tool, mcp_session

    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        return await _call_tool(c, "call", {"endpoint_id": EP, "params": {"aweme_id": "7"}}, token=token)


async def test_mcp_gives_the_notice_without_the_providers_body(clients, platform_on, monkeypatch):
    out = await _mcp_call(clients, monkeypatch)
    assert out["status"] == 402 and out["call_id"] and out["cost_usd"] == 0
    assert "body" not in out, "the provider's wallet details stay out"
    assert out["whose_error"] == "treg" and out["hint"] == NOTICE
    assert "vendor.example" not in json.dumps(out)


async def test_mcp_never_blames_the_teams_balance_for_its_own_keys_402(clients, platform_on, monkeypatch):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    out = await _mcp_call(clients, monkeypatch)
    assert out["status"] == 402 and out["whose_error"] == "provider"
    assert "prepaid balance" not in (out.get("hint") or "")


def _cli_call(monkeypatch, response: httpx.Response, *, json_mode: bool) -> None:
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *args, **kwargs):
            return response

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_JSON_OVERRIDE", json_mode)
    with pytest.raises(SystemExit):
        cli.cmd_call(SimpleNamespace(target=EP, path="", method="GET", query=[], data=None, file=None,
                                     content_type=None, header=[], upload=[]), {})


@pytest.mark.parametrize("json_mode", [False, True])
def test_treg_call_gives_the_notice(monkeypatch, capsys, json_mode):
    response = httpx.Response(402, content=OUT, headers={
        "X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": "abc", "X-Treg-Capacity": "exhausted"},
        request=httpx.Request("GET", f"https://treg.example/call/{EP}"))
    _cli_call(monkeypatch, response, json_mode=json_mode)
    out = capsys.readouterr()
    if json_mode:
        assert json.loads(out.out)["_treg"]["capacity"] == "exhausted"
    else:
        assert json.loads(out.out) == json.loads(OUT), "stdout stays the exact body"
        assert f"treg: {NOTICE}" in out.err and "charged $" not in out.err, "the charge is said once"


def test_treg_call_gives_balance_advice_only_for_treg_s_own_402(monkeypatch, capsys):
    body = {"detail": {"error": "credits_exhausted"}}  # a provider's FastAPI-shaped 402
    request = httpx.Request("GET", f"https://treg.example/call/{EP}")
    _cli_call(monkeypatch, httpx.Response(402, json=body, request=request), json_mode=False)
    assert "treg topup" not in capsys.readouterr().err
    ceiling = {"detail": {"error": "route_max_cost"}}
    _cli_call(monkeypatch, httpx.Response(402, json=ceiling, headers={"X-Treg-Error": "1"}, request=request),
              json_mode=False)
    assert "treg topup" not in capsys.readouterr().err, "a route ceiling is not the balance"
    replay = {"X-Treg-Error": "1", "X-Treg-Idempotent-Replay": "true"}
    _cli_call(monkeypatch, httpx.Response(402, json=body, headers=replay, request=request), json_mode=False)
    assert "treg topup" not in capsys.readouterr().err, "a replayed provider 402 is not the balance"
    broke = {"detail": {"error": "insufficient_balance"}}
    _cli_call(monkeypatch, httpx.Response(402, json=broke, headers={"X-Treg-Error": "1"}, request=request),
              json_mode=False)
    assert "treg topup" in capsys.readouterr().err
