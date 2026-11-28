"""An own key out of credit (proxy-model.md "An own key out of credit"): after a streak of
out-of-credit answers on a team's own key, treg answers that endpoint's calls itself for a short
hold, then sends again; the provider's own answers are never altered."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import audit
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra import own_key_credit
from treg.infra.db import session_maker
from treg.models import CallRecord

EP = "tikhub.tiktok.video.comments"
OWN_KEY = "org.own-Q7-secret"
OUT = b'{"detail":"Insufficient balance","key":"org.own-Q7-secret"}'


class Provider:
    """The upstream: answers `status`/`body`, counts what reached it."""

    def __init__(self, status: int, body: bytes):
        self.status, self.body, self.calls = status, body, 0

    async def relay(self, request, upstream_url, tool, secrets, client, drop_params=None,
                    force_identity=False, **_):
        self.calls += 1
        body = self.body

        async def _stream():
            yield body

        async def _close():
            return None

        return UpstreamResponse(self.status, ((b"content-type", b"application/json"),),
                                _stream(), _close)


@pytest.fixture(autouse=True)
def _fresh():
    own_key_credit._streaks.clear()
    own_key_credit._suspects.clear()
    yield
    own_key_credit._streaks.clear()
    own_key_credit._suspects.clear()
    get_settings.cache_clear()


def _hold(monkeypatch, *, seconds: int = 30, streak: int = 3) -> None:
    monkeypatch.setenv("TREG_OWN_KEY_CREDIT_HOLD_S", str(seconds))
    monkeypatch.setenv("TREG_OWN_KEY_CREDIT_STREAK", str(streak))
    get_settings.cache_clear()


async def _own_key(clients: AsyncClient, value: str = OWN_KEY) -> None:
    r = await clients.post("/secrets", json={"name": "tikhub", "value": value})
    assert r.status_code in (200, 201), r.text


async def _rows() -> list[CallRecord]:
    await audit.drain()
    async with session_maker() as db:
        return (await db.execute(select(CallRecord).order_by(CallRecord.id))).scalars().all()


async def test_off_by_default_every_call_reaches_the_provider(clients: AsyncClient, monkeypatch):
    await _own_key(clients)
    provider = Provider(402, OUT)
    monkeypatch.setattr(call_service, "relay", provider.relay)
    for _ in range(15):
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 402
    assert provider.calls == 15


async def test_a_streak_holds_the_next_calls_without_sending_them(clients: AsyncClient, monkeypatch):
    _hold(monkeypatch)
    await _own_key(clients)
    provider = Provider(402, OUT)
    monkeypatch.setattr(call_service, "relay", provider.relay)
    relayed = [await clients.get(f"/call/{EP}?aweme_id=7") for _ in range(3)]
    # The provider's own answers are relayed as they came, credential masked as always.
    assert all(r.status_code == 402 and b"Insufficient balance" in r.content for r in relayed)
    assert all(r.headers.get("x-treg-error") is None for r in relayed)

    held = await clients.get(f"/call/{EP}?aweme_id=7")
    assert provider.calls == 3
    assert held.status_code == 402
    assert held.headers["x-treg-error"] == "1"
    assert 1 <= int(held.headers["retry-after"]) <= 30
    detail = held.json()["detail"]
    assert detail["error"] == "own_key_out_of_credit"
    assert detail["provider"] == "tikhub" and detail["endpoint_id"] == EP
    assert detail["provider_last_answer"]["status"] == 402
    assert "Insufficient balance" in detail["provider_last_answer"]["body"]
    assert OWN_KEY not in held.text
    rows = await _rows()
    assert [r.refused_by for r in rows] == [None, None, None, "own_credit"]
    assert rows[-1].credential_tier == "credential" and rows[-1].upstream_ms is None


async def test_continuous_traffic_never_extends_the_hold_and_it_ends(clients: AsyncClient,
                                                                   monkeypatch):
    """The degraded mode's exit: held calls never reach the provider, so they cannot renew the
    mark; at its end the next call is sent, and a success clears the way for every call after."""
    _hold(monkeypatch, seconds=30, streak=2)
    await _own_key(clients)
    provider = Provider(402, OUT)
    monkeypatch.setattr(call_service, "relay", provider.relay)
    for _ in range(2):
        await clients.get(f"/call/{EP}?aweme_id=7")
    clock = [own_key_credit.time.time()]
    monkeypatch.setattr(own_key_credit.time, "time", lambda: clock[0])
    for _ in range(40):  # steady traffic through the whole hold
        clock[0] += 0.5
        r = await clients.get(f"/call/{EP}?aweme_id=7")
        assert r.status_code == 402 and r.json()["detail"]["error"] == "own_key_out_of_credit"
    assert provider.calls == 2

    clock[0] += 11  # past the hold's end, whatever the traffic did meanwhile
    provider.status, provider.body = 200, b'{"comments":[]}'
    for _ in range(5):
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert provider.calls == 7


async def test_a_still_empty_account_is_held_again_after_one_probe(clients: AsyncClient,
                                                                  monkeypatch):
    _hold(monkeypatch, seconds=10, streak=2)
    await _own_key(clients)
    provider = Provider(402, OUT)
    monkeypatch.setattr(call_service, "relay", provider.relay)
    for _ in range(2):
        await clients.get(f"/call/{EP}?aweme_id=7")
    clock = [own_key_credit.time.time() + 11]
    monkeypatch.setattr(own_key_credit.time, "time", lambda: clock[0])
    probe = await clients.get(f"/call/{EP}?aweme_id=7")
    assert probe.headers.get("x-treg-error") is None and provider.calls == 3
    again = await clients.get(f"/call/{EP}?aweme_id=7")
    assert again.json()["detail"]["error"] == "own_key_out_of_credit" and provider.calls == 3


async def test_a_rate_limit_is_never_held(clients: AsyncClient, monkeypatch):
    _hold(monkeypatch)
    await _own_key(clients)
    provider = Provider(429, b'{"detail":"Too many requests"}')
    monkeypatch.setattr(call_service, "relay", provider.relay)
    for _ in range(10):
        r = await clients.get(f"/call/{EP}?aweme_id=7")
        assert r.status_code == 429 and r.headers.get("x-treg-error") is None
    assert provider.calls == 10


async def test_another_answer_ends_the_streak(clients: AsyncClient, monkeypatch):
    _hold(monkeypatch, streak=3)
    await _own_key(clients)
    provider = Provider(402, OUT)
    monkeypatch.setattr(call_service, "relay", provider.relay)
    for status in (402, 402, 500, 402, 402):
        provider.status = status
        await clients.get(f"/call/{EP}?aweme_id=7")
    assert provider.calls == 5
    provider.status = 402
    await clients.get(f"/call/{EP}?aweme_id=7")
    held = await clients.get(f"/call/{EP}?aweme_id=7")
    assert held.json()["detail"]["error"] == "own_key_out_of_credit" and provider.calls == 6


async def test_a_newly_connected_key_is_not_held(clients: AsyncClient, monkeypatch):
    _hold(monkeypatch, streak=2)
    await _own_key(clients)
    provider = Provider(402, OUT)
    monkeypatch.setattr(call_service, "relay", provider.relay)
    for _ in range(2):
        await clients.get(f"/call/{EP}?aweme_id=7")
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).headers["x-treg-error"] == "1"
    await _own_key(clients, value="org.new-key-R8")
    provider.status, provider.body = 200, b'{"comments":[]}'
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert provider.calls == 3
