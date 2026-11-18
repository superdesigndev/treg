"""A provider that stops answering must not take every upstream connection from the others: a pool
wait is bounded and answers the typed 503 `upstream_busy` (not charged, hold released), and a
per-provider in-flight count logs (mode `log`) or refuses (mode `enforce`) past its cap."""

from __future__ import annotations

import logging

import httpx
import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra.upstream.inflight import InFlight, UpstreamBusy, inflight

from test_marketplace_call import EP, _balance, _fake_relay, platform_on  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_counts():
    inflight.reset()
    yield
    inflight.reset()
    get_settings.cache_clear()


def test_the_shared_client_waits_briefly_for_a_connection_and_reads_for_the_call_timeout():
    s = get_settings()
    assert s.upstream_pool_timeout_s <= 5 < s.call_timeout_s
    assert s.upstream_max_connections >= 1000


def test_log_mode_counts_and_logs_but_never_refuses(caplog):
    gate = InFlight()
    with caplog.at_level(logging.WARNING, logger="treg.upstream_inflight"):
        slots = [gate.acquire("p", 2, "log") for _ in range(4)]
    assert gate.active("p") == 4
    assert len([r for r in caplog.records if "over its in-flight cap" in r.message]) == 1
    for slot in slots:
        slot.release()
        slot.release()
    assert gate.active("p") == 0


def test_enforce_mode_refuses_past_the_cap_and_a_release_frees_a_place():
    gate = InFlight()
    first, second = gate.acquire("p", 2, "enforce"), gate.acquire("p", 2, "enforce")
    with pytest.raises(UpstreamBusy):
        gate.acquire("p", 2, "enforce")
    assert gate.acquire("other", 2, "enforce") is not None, "another provider is unaffected"
    first.release()
    assert gate.acquire("p", 2, "enforce") is not None
    second.release()


def test_off_and_zero_cap_count_nothing():
    gate = InFlight()
    assert gate.acquire("p", 5, "off") is None and gate.acquire("p", 0, "enforce") is None
    assert gate.active("p") == 0


async def test_a_pool_wait_timeout_is_a_typed_503_and_charges_nothing(
    clients: AsyncClient, platform_on, monkeypatch,
):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, raises=httpx.PoolTimeout("pool")))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503, r.text
    assert "upstream_busy" in r.text and "not charged" in r.text
    assert r.headers["retry-after"] == "2"
    assert await _balance(clients) == before


async def test_enforce_refuses_a_provider_at_its_cap_uncharged_and_returns_the_place(
    clients: AsyncClient, platform_on, monkeypatch,
):
    monkeypatch.setenv("TREG_UPSTREAM_INFLIGHT_MODE", "enforce")
    monkeypatch.setenv("TREG_UPSTREAM_INFLIGHT_CAP", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    held = inflight.acquire("tikhub", 1, "enforce")
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503 and "upstream_busy" in r.text, r.text
    assert await _balance(clients) == before
    held.release()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert inflight.active("tikhub") == 0, "the place is returned after the answer"
