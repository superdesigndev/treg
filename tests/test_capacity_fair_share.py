"""Fair share of a provider's concurrency on treg's own keys (domain/capacity/fair_share.py): a free
place is taken at once; when the provider is full, the next free place goes to the waiting team
with the fewest calls in progress; a call that waited too long leaves with `share_busy`, not
charged. Own keys are never gated."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.capacity.fair_share import FairShare, ShareBusy, fair_share
from treg.domain.capacity.view import view as capacity_view

from test_marketplace_call import EP, PLATFORM_KEYS, _balance, _fake_relay, platform_on  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_gate():
    fair_share.reset()
    yield
    fair_share.reset()


# ---- the gate -----------------------------------------------------------------------------------

async def _turns(n: int = 5) -> None:
    """Let the event loop run the woken waiters to completion."""
    for _ in range(n):
        await asyncio.sleep(0)


async def test_a_freed_place_goes_to_the_team_with_the_fewest_calls_in_progress():
    gate = FairShare()
    heavy = [await gate.acquire("p", 1, 3) for _ in range(3)]       # the heavy team fills the provider
    first_heavy = asyncio.create_task(gate.acquire("p", 1, 3))      # it queues first...
    await asyncio.sleep(0)
    light = asyncio.create_task(gate.acquire("p", 2, 3))            # ...a light team queues after it
    await asyncio.sleep(0)
    heavy[0].release()
    await _turns()
    assert light.done() and not first_heavy.done(), "the light team gets the freed place first"
    heavy[1].release()
    slot = await asyncio.wait_for(first_heavy, 1)
    assert slot.waited_ms >= 0
    for s in (heavy[2], light.result(), slot):
        s.release()
    assert gate._pools["p"].active == 0 and not gate._pools["p"].by_team


async def test_a_call_that_waits_too_long_leaves_the_line_and_never_takes_a_place():
    gate = FairShare()
    held = await gate.acquire("p", 1, 1)
    with pytest.raises(ShareBusy) as busy:
        await gate.acquire("p", 1, 1, max_wait_s=0.05)
    assert busy.value.active == 1
    assert not gate._pools["p"].line
    held.release()
    held.release()  # idempotent
    assert gate._pools["p"].active == 0
    assert (await gate.acquire("p", 1, 1)).waited_ms == 0, "a free place is taken at once"


async def test_a_cancelled_waiter_leaks_no_place():
    gate = FairShare()
    held = await gate.acquire("p", 1, 1)
    waiting = asyncio.create_task(gate.acquire("p", 2, 1))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    held.release()
    assert gate._pools["p"].active == 0 and not gate._pools["p"].line


async def test_free_places_are_never_left_empty_while_a_call_waits():
    gate = FairShare()
    held = [await gate.acquire("p", 1, 2) for _ in range(2)]
    waiters = [asyncio.create_task(gate.acquire("p", 2, 4)) for _ in range(2)]  # the limit was raised
    await _turns()
    assert all(w.done() for w in waiters), "a raised limit hands its new places to the line at once"
    for s in held + [w.result() for w in waiters]:
        s.release()


# ---- the call path ------------------------------------------------------------------------------

@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(capacity_view, "concurrency", lambda provider: 1 if provider == "tikhub" else 0)
    monkeypatch.setenv("TREG_FAIR_SHARE_WAIT_S", "0.2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_full_provider_refuses_with_share_busy_and_charges_nothing(
    clients: AsyncClient, platform_on, gated, monkeypatch,
):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    other_team = await fair_share.acquire("tikhub", -1, 1)   # another team holds the only place
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429, r.text
    assert "share_busy" in r.text and "not charged" in r.text
    assert await _balance(clients) == before
    other_team.release()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert fair_share._pools["tikhub"].active == 0, "the place is returned after the answer"


async def test_own_key_calls_are_never_gated(clients: AsyncClient, platform_on, gated, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    other_team = await fair_share.acquire("tikhub", -1, 1)
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    other_team.release()


async def test_a_provider_error_still_returns_the_place(clients: AsyncClient, platform_on, gated, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(0, raises=__import__("httpx").ConnectError("down")))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code >= 500
    assert fair_share._pools["tikhub"].active == 0
