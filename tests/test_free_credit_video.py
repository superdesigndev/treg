"""A platform declaring `purchased_credit_only` (video generation) is paid from purchased credit
only: free credit never reserves or settles it, and every other call keeps its order."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import CreditBlock, Hold, LedgerEntry

from test_asynctasks import _response

VIDEO = "/call/openrouter.video-gen.wan-3-0.from_text"
VIDEO_BODY = {"model": "alibaba/wan-3.0", "prompt": "A paper boat.", "duration": 2, "resolution": "480p"}
JUDGE = "/call/openrouter.ai-judge.decide"


@pytest.fixture
def openrouter_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_OPENROUTER", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "openrouter")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def video_purchased_only(monkeypatch):
    monkeypatch.setitem(catalog_store.load().platforms["video-gen"], "purchased_credit_only", True)


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _blocks(org_id: int) -> dict[str, int]:
    async with session_maker() as db:
        rows = (await db.execute(select(CreditBlock).where(CreditBlock.org_id == org_id))).scalars().all()
    out: dict[str, int] = {}
    for b in rows:
        out[b.kind] = out.get(b.kind, 0) + b.remaining_micro
    return out


async def _money_entries(org_id: int) -> list[str]:
    async with session_maker() as db:
        return [e.kind for e in (await db.execute(
            select(LedgerEntry).where(LedgerEntry.org_id == org_id))).scalars().all()]


async def _invariant(org_id: int) -> None:
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        blocks = sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id))
        holds = sum(h.amount_micro for h in await ledger.open_holds_of(db, org_id))
    assert balance == blocks - holds


async def test_free_only_team_is_refused_video_with_a_402_and_nothing_charged(
    clients: AsyncClient, monkeypatch, openrouter_platform, video_purchased_only,
):
    async def must_not_relay(*args, **kwargs):
        raise AssertionError("a refused video call reached the provider")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    org_id = await _org_id(clients)
    blocks_before, entries_before = await _blocks(org_id), await _money_entries(org_id)
    assert blocks_before.get("promotional", 0) >= 1_000_000 and "purchased" not in blocks_before

    r = await clients.post(VIDEO, json=VIDEO_BODY)
    assert r.status_code == 402, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "free_credit_excludes_video"
    assert detail["message"].startswith("Free credit does not cover video generation.")
    assert "Add funds" in detail["message"]
    assert detail["purchased_balance_micro"] == 0
    assert await _blocks(org_id) == blocks_before
    assert await _money_entries(org_id) == entries_before
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
    await _invariant(org_id)


async def test_purchased_credit_pays_video_and_free_credit_stays_untouched(
    clients: AsyncClient, monkeypatch, openrouter_platform, video_purchased_only,
):
    async def fake_relay(*args, **kwargs):
        return _response(201, {"id": "paid-video"})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    org_id = await _org_id(clients)
    async with session_maker() as db:
        await ledger.topup(db, org_id, 5_000_000, "pi_video")
        await db.commit()
    promo = (await _blocks(org_id))["promotional"]

    r = await clients.post(VIDEO, json=VIDEO_BODY)
    assert r.status_code == 201, r.text
    async with session_maker() as db:
        hold = (await db.execute(select(Hold).where(Hold.org_id == org_id))).scalar_one()
        assert hold.purchased_only is True
        consumed = await ledger.settle(db, hold.id)
    assert consumed == hold.amount_micro > 0
    assert await _blocks(org_id) == {"promotional": promo, "purchased": 5_000_000 - consumed}
    await _invariant(org_id)


async def test_purchased_only_reserve_counts_open_holds_and_never_settles_from_free_credit(clients):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        await ledger.topup(db, org_id, 1_000, "pi_small")
        await db.commit()
        promo = (await _blocks(org_id))["promotional"]
        first = await ledger.reserve(db, org_id, "video", 600, purchased_only=True)
        charged = (await db.get(Hold, first)).amount_micro
        assert charged <= 1_000 < 2 * charged, "the test needs room for exactly one hold"
        with pytest.raises(ledger.PurchasedCreditShort) as refused:
            await ledger.reserve(db, org_id, "video", 600, purchased_only=True)
        assert refused.value.purchased_micro == 1_000 - charged
        # Settling above the hold draws purchased blocks only; the overrun is a shortfall.
        consumed = await ledger.settle(db, first, actual_micro=10_000)
    assert consumed == 1_000
    assert await _blocks(org_id) == {"promotional": promo, "purchased": 0}
    await _invariant(org_id)


async def test_non_video_calls_still_spend_free_credit_first(
    clients: AsyncClient, monkeypatch, openrouter_platform,
):
    async def fake_relay(*args, **kwargs):
        return _response(200, {"answers": {}, "usage": {"cost": 0.0002}})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    org_id = await _org_id(clients)
    async with session_maker() as db:
        await ledger.topup(db, org_id, 5_000_000, "pi_other")
        await db.commit()
    promo = (await _blocks(org_id))["promotional"]

    r = await clients.post(JUDGE, json={"model": "typesafe/jev-1.13", "state": "x", "questions": {}})
    assert r.status_code == 200, r.text
    after = await _blocks(org_id)
    assert after["purchased"] == 5_000_000 and after["promotional"] < promo
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
    await _invariant(org_id)
