"""Per-team backpressure: the fold names the teams it is behind on, and their metered calls take
one of a few shared places, the rest a fast 429; a team's own key and other teams are never
touched, and the throttle lifts once the fold catches up."""
import asyncio
from datetime import timedelta

import pytest
from httpx import AsyncClient

from treg import audit
from treg.application import allowance_backpressure as producer
from treg.application import allowance_sweep
from treg.config import Settings, get_settings
from treg.infra import kv, provider_concurrency, team_backpressure as gate
from treg.timeutil import utcnow_naive
from test_marketplace_call import EP, _balance, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _assert_no_money


class Store(kv.LocalStore):
    """The local store with the shared places a real one has, and switches for its faults."""

    def __init__(self):
        super().__init__()
        self.slots: dict[str, set[str]] = {}
        self.reads = 0
        self.down = False

    async def get_texts(self, keys):
        self.reads += 1
        if self.down:
            raise ConnectionError("store down")
        return await super().get_texts(keys)

    async def acquire_slot(self, key, token, ttl_ms, limit):
        if self.down:
            return "unavailable"
        owners = self.slots.setdefault(key, set())
        if len(owners) >= limit:
            return "busy"
        owners.add(token)
        return "acquired"

    async def renew_slot(self, key, token, ttl_ms):
        return "renewed" if token in self.slots.get(key, ()) else "not_owned"

    async def release_slot(self, key, token):
        self.slots.get(key, set()).discard(token)
        return "released"


@pytest.fixture
def store(monkeypatch):
    settings = get_settings()
    shared = Store()
    monkeypatch.setattr(settings, "kv_url", "redis://backpressure-test")
    monkeypatch.setattr(kv, "store", lambda: shared)
    monkeypatch.setattr(settings, "money_backpressure_oldest_s", 60)
    monkeypatch.setattr(settings, "money_backpressure_max_in_flight", 2)
    monkeypatch.setattr(settings, "money_allowance_fold_shards", 1)
    gate.reset()
    producer.reset()
    yield shared
    gate.reset()
    producer.reset()


def _clock(monkeypatch, start=1000.0):
    now = [start]
    monkeypatch.setattr(gate.time, "monotonic", lambda: now[0])
    return now


def test_off_by_default():
    settings = Settings(_env_file=None, kv_url="redis://x")
    assert settings.money_backpressure_oldest_s is None
    assert settings.money_backpressure_lease_held_skips is None
    assert not gate.enabled(settings)


def test_the_oldest_wait_names_a_team(store):
    assert producer.choose([(1, 600), (2, 59), (3, 60)], starved_after_s=30) == {1, 3}


def test_lease_held_names_a_starved_team_only_after_enough_skips_in_a_row(store, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "money_backpressure_oldest_s", None)
    monkeypatch.setattr(settings, "money_backpressure_lease_held_skips", 3)
    for _ in range(3):
        producer.note_lease_held(7)
        producer.note_lease_held(8)
    # 8 has a queue the fold is not behind on: being skipped is not a backlog
    assert producer.choose([(7, 31), (8, 5)], starved_after_s=30) == {7}
    producer.note_folded(7)
    assert producer.choose([(7, 31)], starved_after_s=30) == frozenset()
    # a team with nothing queued any more starts its count again
    producer.note_lease_held(8)
    producer.choose([], starved_after_s=30)
    assert 8 not in producer._lease_held


async def test_a_share_publishes_its_list_and_an_empty_list_deletes_it(store):
    await producer.publish((0, 1), [(5, 120), (6, 1)], starved_after_s=30)
    assert await store.get_text(gate.share_key((0, 1))) == "5"
    await producer.publish((0, 1), [(5, 1)], starved_after_s=30)
    assert await store.get_text(gate.share_key((0, 1))) is None


async def test_an_unchanged_list_is_not_written_again_until_a_third_of_its_life(store, monkeypatch):
    writes = []
    real = store.set_text

    async def counted(key, value, ttl_s):
        writes.append((key, value, ttl_s))
        return await real(key, value, ttl_s)

    monkeypatch.setattr(store, "set_text", counted)
    now = [0.0]
    monkeypatch.setattr(producer.time, "monotonic", lambda: now[0])
    for _ in range(3):
        await producer.publish((0, 1), [(5, 120)], starved_after_s=30)
    now[0] = get_settings().money_backpressure_mark_ttl_s / 3 + 0.1
    await producer.publish((0, 1), [(5, 120)], starved_after_s=30)
    assert writes == [(gate.share_key((0, 1)), "5", 15)] * 2


async def test_switched_off_nothing_is_read_or_written(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "money_backpressure_oldest_s", None)
    monkeypatch.setattr(store, "set_text", pytest.fail)
    await producer.publish((0, 1), [(5, 999)], starved_after_s=30)
    assert await gate.admit(5) is None
    assert store.reads == 0


async def test_a_listed_team_gets_its_places_then_a_fast_refusal(store):
    await gate.publish((0, 1), [5], 15)
    first, second = await gate.admit(5), await gate.admit(5)
    assert isinstance(first, provider_concurrency.Lease) and isinstance(second, provider_concurrency.Lease)
    with pytest.raises(gate.Throttled) as refused:
        await gate.admit(5)
    assert refused.value.max_in_flight == 2 and refused.value.retry_after_s >= 1
    first.release()
    await asyncio.sleep(0)  # the release runs behind
    third = await gate.admit(5)
    assert third is not None
    # another team is never asked for a place
    assert await gate.admit(6) is None
    assert set(store.slots) == {f"{gate._SLOTS}:5"}
    second.release()
    third.release()


async def test_one_read_per_refresh_for_every_call_of_the_process(store, monkeypatch):
    now = _clock(monkeypatch)
    await gate.publish((0, 1), [5], 15)
    assert all(await asyncio.gather(*(gate.throttled(5) for _ in range(20))))
    assert store.reads == 1
    now[0] += get_settings().money_backpressure_refresh_s
    assert await gate.throttled(5)
    assert store.reads == 2


async def test_a_store_that_cannot_answer_throttles_nobody(store, monkeypatch):
    now = _clock(monkeypatch)
    await gate.publish((0, 1), [5], 15)
    store.down = True
    assert await gate.admit(5) is None          # the list cannot be read
    store.down = False
    now[0] += get_settings().money_backpressure_refresh_s
    assert await gate.throttled(5)
    store.down = True
    assert await gate.admit(5) is None          # listed, but no place can be taken: allowed


async def test_the_throttle_lifts_once_the_fold_catches_up_while_calls_continue(store, monkeypatch):
    """A degraded mode must end once its cause is gone: the pass that finds the queue drained
    deletes the list, and every process lets the team through within one refresh, however many
    of its calls are refused meanwhile (a refusal never renews the list)."""
    now = _clock(monkeypatch)
    await producer.publish((0, 1), [(5, 300)], starved_after_s=30)
    held = [await gate.admit(5), await gate.admit(5)]
    for _ in range(50):
        with pytest.raises(gate.Throttled):
            await gate.admit(5)
    await producer.publish((0, 1), [(5, 2)], starved_after_s=30)   # caught up
    now[0] += get_settings().money_backpressure_refresh_s
    assert await gate.admit(5) is None
    for lease in held:
        lease.release()


async def test_a_list_nobody_publishes_again_expires(store, monkeypatch):
    await gate.publish((0, 1), [5], 2)
    monkeypatch.setattr(kv.time, "monotonic", lambda: 10**9)  # the local store's clock
    assert await store.get_text(gate.share_key((0, 1))) is None


async def test_the_fold_pass_publishes_from_the_queue_it_read(store, monkeypatch):
    old = utcnow_naive() - timedelta(seconds=600)
    young = utcnow_naive() - timedelta(seconds=1)

    async def queued_teams(db, queued_before, *, limit, shard, after):
        return [] if after is not None else [(5, old), (6, young)]

    async def fold_team(*args, **kwargs):
        return 0

    monkeypatch.setattr(allowance_sweep.allowance, "queued_teams", queued_teams)
    monkeypatch.setattr(allowance_sweep, "_fold_team", fold_team)

    class Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    await allowance_sweep.fold_queued_once(Session, shard=(0, 1))
    assert await store.get_text(gate.share_key((0, 1))) == "5"


async def test_a_metered_call_of_a_throttled_team_is_refused_before_any_money(
        clients: AsyncClient, platform_on, store):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    await gate.publish((0, 1), [org_id], 15)
    held = [await gate.admit(org_id), await gate.admit(org_id)]
    before = await _balance(clients)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 429, response.text
    assert int(response.headers["Retry-After"]) >= 1
    detail = response.json()["detail"]
    assert detail["error"] == "team_backpressure"
    assert detail["max_in_flight"] == 2 and detail["retry_after_s"] >= 1
    assert await _balance(clients) == before
    await _assert_no_money()
    await audit.drain()
    for lease in held:
        lease.release()
    await asyncio.sleep(0)
    # with a place free the same call runs, and gives its place back when it ends
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    await asyncio.sleep(0)
    assert store.slots[f"{gate._SLOTS}:{org_id}"] == set()


async def test_the_teams_own_key_is_never_throttled(clients: AsyncClient, platform_on, store):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    await gate.publish((0, 1), [org_id], 15)
    held = [await gate.admit(org_id), await gate.admit(org_id)]
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    assert response.json()["auth"] == "Bearer OWN-KEY"
    for lease in held:
        lease.release()
