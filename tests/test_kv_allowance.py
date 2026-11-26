"""A team's allowance counter in the shared store: one figure every process spends from."""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from treg.infra import kv

def _soon(ms: int = 60_000) -> int:
    return int(time.time() * 1000) + ms


async def _add(store, org_id, epoch, amount, **named):
    """An add by a caller that read the counter just before, as a refill does."""
    run = (await store.allowance_state(org_id))[4]
    return await store.allowance_add(org_id, epoch, amount, run=run, **named)


async def _state(store, org_id):
    return (await store.allowance_state(org_id))[:4]


real_redis = pytest.mark.skipif(
    not os.environ.get('TREG_TEST_KV_URL'), reason='set TREG_TEST_KV_URL to a scratch Redis')


@pytest.fixture
async def counter():
    store = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    org_id = time.time_ns() % 2_000_000_000
    try:
        yield store, org_id
    finally:
        await store._client.delete(kv._allowance_key(org_id))
        await store.aclose()


@real_redis
async def test_takes_stop_at_what_the_counter_holds(counter):
    store, org_id = counter
    assert await store.allowance_take(org_id, 1) == 0
    assert await _add(store, org_id, 7, 250, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 250
    assert [await store.allowance_take(org_id, 100) for _ in range(3)] == [7, 7, 0]
    assert await store.allowance_take(org_id, 50) == 7
    assert await store.allowance_take(org_id, 1) == 0
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000


@real_redis
async def test_a_take_near_the_deadline_or_the_bottom_asks_one_caller_to_refill(counter, monkeypatch):
    store, org_id = counter
    await _add(store, org_id, 7, 1_000, until_ms=_soon(10_000), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 10, ahead_ms=5_000, low_micro=100) == 7
    assert await store.allowance_take(org_id, 10, ahead_ms=20_000) == -7, "inside the lead"
    assert await store.allowance_take(org_id, 10, ahead_ms=20_000) == 7, "one caller is asked"
    assert (await store.allowance_state(org_id))[0] == 970, "each of them took its money"
    monkeypatch.setattr(kv, "_ALLOWANCE_ASK_AGAIN_MS", 0)
    assert await store.allowance_take(org_id, 10, low_micro=961) == -7, "below the low mark"
    assert await store.allowance_take(org_id, 10, low_micro=100) == 7
    assert await store.allowance_take(org_id, 10) == 7, "nobody is asked without a lead or a mark"


@real_redis
async def test_each_deadline_asks_for_its_own_refill_however_soon_after_the_last(counter):
    """A refill moves the deadline on. The window it opens can be shorter than the pause between
    two asks, and must not lapse because the last ask was a moment ago."""
    store, org_id = counter
    await _add(store, org_id, 7, 1_000, until_ms=_soon(10_000), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 10, ahead_ms=20_000) == -7
    assert await store.allowance_take(org_id, 10, ahead_ms=20_000) == 7
    await _add(store, org_id, 7, 20, until_ms=_soon(15_000), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 10, ahead_ms=20_000) == -7, "a new deadline"
    assert await store.allowance_take(org_id, 10, ahead_ms=20_000) == 7


@real_redis
async def test_the_take_that_crosses_the_low_mark_asks_whatever_the_pause(counter):
    """A call gave its money back and the counter is above the mark again. The refill that was
    asked for then added nothing, so the pause still runs; the next dip must not wait it out."""
    store, org_id = counter
    await _add(store, org_id, 7, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 800, low_micro=250) == -7
    assert await store.allowance_give_back(org_id, 7, 800) is True
    await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 800, low_micro=250) == -7, "crossed the mark again"
    assert await store.allowance_take(org_id, 10, low_micro=250) == 7, "staying low waits"


@real_redis
async def test_a_counter_low_again_after_a_refill_that_paid_asks_at_once(counter):
    """The pause is for a refill that failed. One that brought money worked; a counter low
    again right after it is being spent, and waiting would let it run dry."""
    store, org_id = counter
    await _add(store, org_id, 7, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 800, low_micro=250) == -7
    await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 10, low_micro=250) == 7, "nothing came: the pause holds"
    await _add(store, org_id, 7, 810, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 800, low_micro=250) == -7, "it paid, and is low again"


@real_redis
async def test_takes_stop_once_the_counter_is_no_longer_fresh_and_resume_on_the_next_add(counter):
    store, org_id = counter
    assert await _state(store, org_id) == (0, 0, 0, 0)
    # Redis judges the deadline by its own clock, which need not agree with this machine's.
    seconds, micros = await store._client.time()
    until = seconds * 1000 + micros // 1000 + 40
    await _add(store, org_id, 7, 100, until_ms=until, ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 10) == 7
    await asyncio.sleep(0.08)
    assert await store.allowance_take(org_id, 10) == 0
    assert (await store.allowance_state(org_id))[0] == 90
    assert await store.allowance_give_back(org_id, 7, 10) is True
    assert await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 100
    assert await store.allowance_take(org_id, 100) == 7


@real_redis
async def test_an_add_that_arrives_late_never_moves_the_deadline_back_or_reopens_it(counter):
    store, org_id = counter
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await _add(store, org_id, 7, 50, until_ms=_soon(-5_000), ttl_ms=60_000, seen_frozen=0) == 150
    assert await store.allowance_take(org_id, 10) == 7, "the later deadline stands"
    await store.allowance_retire(org_id, 7, 60_000)
    assert await _add(store, org_id, 8, 100, until_ms=_soon(-5_000), ttl_ms=60_000, seen_frozen=0) == 100
    assert await store.allowance_take(org_id, 10) == 0, "published after its own deadline"


@real_redis
async def test_take_up_to_takes_what_is_there_from_its_own_epoch_only(counter):
    store, org_id = counter
    await _add(store, org_id, 7, 400, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take_up_to(org_id, 8, 900) == 0
    assert await store.allowance_take_up_to(org_id, 7, 150) == 150
    assert await store.allowance_take_up_to(org_id, 7, 900) == 250
    assert await store.allowance_take_up_to(org_id, 7, 900) == 0
    assert (await store.allowance_state(org_id))[0] == 0


async def _pace(store, org_id):
    """(what the counter is down by, ms since the last add), read with the state."""
    state = await store.allowance_state_and_pace(org_id)
    assert state[:5] == await store.allowance_state(org_id), "the state it reads with it"
    return state[5:]


@real_redis
async def test_the_pace_is_what_the_counter_went_down_by_since_the_last_add(counter):
    store, org_id = counter
    assert await _pace(store, org_id) == (0, 0), "no counter, no reading"
    await _add(store, org_id, 7, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 300) == 7
    assert await store.allowance_give_back(org_id, 7, 100) is True
    await asyncio.sleep(0.05)
    spent, since_ms = await _pace(store, org_id)
    assert spent == 200 and 50 <= since_ms < 5_000, "net of what calls gave back"
    await _add(store, org_id, 7, 200, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    spent, since_ms = await _pace(store, org_id)
    assert spent == 0 and since_ms < 50, "each add starts a new reading"
    assert await store.allowance_take(org_id, 1_000) == 7
    await asyncio.sleep(0.05)
    assert await store.allowance_take(org_id, 1) == 0
    spent, later_ms = await _pace(store, org_id)
    assert spent == 1_000 and later_ms >= 50, "what was spent stays; how long ago grows"


@real_redis
async def test_money_nobody_spent_leaves_no_reading(counter):
    """A take-back corrects a counter above its hold, and a drain returns what a counter held
    to the balance: neither is spending, so the next refill reads nothing rather than a pace."""
    store, org_id = counter
    await _add(store, org_id, 7, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take_up_to(org_id, 7, 100) == 100
    assert await _pace(store, org_id) == (0, 0)
    await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 100) == 7
    assert (await _pace(store, org_id))[0] == 100
    assert await store.allowance_drop(org_id, 7) == 800
    assert await _pace(store, org_id) == (0, 0)
    await store.allowance_retire(org_id, 7, 60_000)
    assert await _pace(store, org_id) == (0, 0)


@real_redis
async def test_concurrent_takes_from_two_clients_never_overspend(counter):
    store, org_id = counter
    other = kv.RedisStore(os.environ['TREG_TEST_KV_URL'])
    try:
        await _add(store, org_id, 3, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
        taken = await asyncio.gather(*(
            client.allowance_take(org_id, 30) for _ in range(40) for client in (store, other)))
    finally:
        await other.aclose()
    assert sum(1 for epoch in taken if epoch == 3) == 33 and set(taken) == {0, 3}
    assert await store.allowance_drop(org_id, 3) == 10


@real_redis
async def test_a_give_back_reaches_only_the_counter_it_was_taken_from(counter):
    store, org_id = counter
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 100) == 7
    assert await store.allowance_give_back(org_id, 7, 30) is True
    assert await store.allowance_give_back(org_id, 8, 30) is False
    assert await _add(store, org_id, 7, 20, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 50
    assert await _add(store, org_id, 9, 500, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 500
    assert await store.allowance_give_back(org_id, 7, 30) is False
    assert await store.allowance_take(org_id, 500) == 9


@real_redis
async def test_dropping_empties_and_freezes_the_counter_and_keeps_its_epoch(counter):
    store, org_id = counter
    assert await store.allowance_drop(org_id, 7) == 0
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 40) == 7
    assert await store.allowance_drop(org_id, 6) == 0, "another hold's counter"
    assert await store.allowance_drop(org_id, 7) == 60
    assert await store.allowance_drop(org_id, 7) == 0
    assert await store.allowance_give_back(org_id, 7, 40) is True
    available, frozen, thawed, _, _ = await store.allowance_state(org_id)
    assert (available, frozen - thawed) == (40, 2)
    assert await store.allowance_take(org_id, 1) == 0, "given back, but not offered until thawed"
    assert await _add(
        store, org_id, 7, 10, until_ms=_soon(), ttl_ms=60_000, seen_frozen=frozen) == 50
    assert await store.allowance_take(org_id, 50) == 7


@real_redis
async def test_only_an_add_that_read_the_freeze_may_thaw_it(counter):
    """A refill reads the freeze count, runs its transaction, then adds. A freeze that lands in
    between is one its transaction did not see, so its add must not lift it."""
    store, org_id = counter
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    available, seen, thawed, _, _ = await store.allowance_state(org_id)
    assert available == 100 and seen == thawed > 0
    assert await store.allowance_freeze(org_id) == seen + 1
    assert await _add(
        store, org_id, 7, 50, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen) == 150
    assert await store.allowance_take(org_id, 10) == 0, "the earlier reading cannot thaw it"
    assert await store.allowance_freeze(org_id) == seen + 2
    assert await _add(
        store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen + 1) == 150
    assert await store.allowance_take(org_id, 10) == 0, "one of two freezes accounted for"
    await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen + 2)
    assert await store.allowance_take(org_id, 150) == 7


@real_redis
async def test_a_freeze_that_lands_before_the_first_add_holds_the_counter_it_builds(counter):
    """The first refill has committed and not published. A freeze that lands now finds no
    counter; the add, whose transaction did not see the freeze, must not open one over it."""
    store, org_id = counter
    assert await _state(store, org_id) == (0, 0, 0, 0)
    frozen = await store.allowance_freeze(org_id, ttl_ms=60_000)
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000
    assert await store.allowance_take(org_id, 10) == 0
    assert await store.allowance_give_back(org_id, 7, 10) is False
    assert await _add(
        store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 100
    assert await store.allowance_take(org_id, 10) == 0, "built frozen"
    assert await _state(store, org_id) == (100, frozen, frozen - 1, 7)
    await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=frozen)
    assert await store.allowance_take(org_id, 100) == 7


@real_redis
async def test_a_freeze_count_read_from_a_counter_since_lost_does_not_jam_the_rebuilt_one(counter):
    """A refill read three freezes; the store then lost the hash. Thawing the rebuilt counter to
    three would leave it frozen with nothing left to bring the two counts together."""
    store, org_id = counter
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    for _ in range(3):
        await store.allowance_freeze(org_id)
    _, seen, _, _, _ = await store.allowance_state(org_id)
    await store._client.delete(kv._allowance_key(org_id))
    assert await _add(
        store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen) == 100
    available, frozen, thawed, _, _ = await store.allowance_state(org_id)
    assert available == 100 and frozen == thawed
    assert await store.allowance_take(org_id, 100) == 7


@real_redis
@pytest.mark.parametrize("first, rebuilt", [(2**41, 2**40), (2**40, 2**41), (None, None)])
async def test_a_freeze_on_a_rebuilt_counter_is_not_thawed_by_a_count_read_from_the_lost_one(
        counter, monkeypatch, first, rebuilt):
    """A refill read five freezes. The store lost the hash, and an overrun after the refill's
    transaction froze the one built in its place. The refill's count is of another hash,
    whether it is larger or smaller than the new one's."""
    store, org_id = counter
    if first is not None:
        monkeypatch.setattr(kv, "_new_counts", lambda: first)
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    for _ in range(5):
        await store.allowance_freeze(org_id)
    _, seen, _, _, _ = await store.allowance_state(org_id)
    await store._client.delete(kv._allowance_key(org_id))
    if rebuilt is not None:
        monkeypatch.setattr(kv, "_new_counts", lambda: rebuilt)
    frozen = await store.allowance_freeze(org_id)
    assert await _add(
        store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen) == 100
    assert await store.allowance_take(org_id, 10) == 0, "the count of a lost hash thaws nothing"
    await _add(store, org_id, 7, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=frozen)
    assert await store.allowance_take(org_id, 100) == 7


@real_redis
async def test_a_counter_rebuilt_for_a_newer_hold_keeps_the_freezes_of_the_old_one(counter):
    store, org_id = counter
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    frozen = await store.allowance_freeze(org_id)
    await store.allowance_retire(org_id, 7, 60_000)
    await _add(store, org_id, 9, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 10) == 0
    await _add(store, org_id, 9, 0, until_ms=_soon(), ttl_ms=60_000, seen_frozen=frozen)
    assert await store.allowance_take(org_id, 10) == 9


@real_redis
async def test_a_writer_with_an_older_epoch_never_replaces_a_newer_counter(counter):
    store, org_id = counter
    await _add(store, org_id, 9, 500, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 'stale'
    assert await store.allowance_give_back(org_id, 7, 100) is False
    assert await store.allowance_retire(org_id, 7, 60_000) == 0
    assert await store.allowance_take(org_id, 500) == 9


@real_redis
async def test_retiring_a_counter_refuses_its_late_writers_and_admits_the_next_hold(counter):
    store, org_id = counter
    epoch = 1_800_000_000_000_007  # the size of a real one: past what a script prints exactly
    await _add(store, org_id, epoch, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 40) == epoch
    assert await store.allowance_retire(org_id, epoch, 60_000) == 60
    assert await _add(store, org_id, epoch, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 'stale'
    assert await store.allowance_give_back(org_id, epoch, 40) is False
    assert await store.allowance_take(org_id, 1) == 0
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000
    assert await _add(store, org_id, epoch + 1, 30, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 30
    assert await store.allowance_take(org_id, 30) == epoch + 1


@real_redis
async def test_retiring_with_no_counter_still_leaves_the_fence(counter):
    store, org_id = counter
    assert await store.allowance_retire(org_id, 7, 60_000) == 0
    assert await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 'stale'


async def _restart(store, org_id):
    """What a restart of the store leaves: the hash as it was written to disk, and a server
    whose run is not the one that built it."""
    await store._client.hset(kv._allowance_key(org_id), 'run', '0' * 40)


@real_redis
@pytest.mark.parametrize("touch, answer", [
    (lambda store, org_id: store.allowance_take(org_id, 100), 0),
    (lambda store, org_id: store.allowance_give_back(org_id, 7, 50), False),
    (lambda store, org_id: store.allowance_take_up_to(org_id, 7, 50), 0),
    (lambda store, org_id: store.allowance_drop(org_id, 7), 0),
    (lambda store, org_id: _pace(store, org_id), (0, 0)),
    (lambda store, org_id: store.allowance_state(org_id), None),
])
async def test_a_counter_built_before_the_store_restarted_offers_nothing(counter, touch, answer):
    """A store that restarts can come back without the last writes it acknowledged, the takes
    and freezes of that interval undone. What it shows of a counter is then no figure to spend
    from: nothing of it is left but its freezes, which a refill reads before it thaws them."""
    store, org_id = counter
    await _add(store, org_id, 7, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    assert await store.allowance_take(org_id, 100) == 7
    await store.allowance_freeze(org_id)
    frozen, thawed = await _counts(store, org_id)
    await _restart(store, org_id)
    if answer is not None:
        assert await touch(store, org_id) == answer
    assert await _state(store, org_id) == (0, frozen, thawed, 0)
    assert 0 < await store._client.pttl(kv._allowance_key(org_id)) <= 60_000


@real_redis
async def test_a_refill_after_a_restart_builds_the_counter_from_what_it_brings(counter):
    store, org_id = counter
    await _add(store, org_id, 7, 900, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    await _restart(store, org_id)
    assert await _add(
        store, org_id, 7, 250, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0) == 250
    assert [await store.allowance_take(org_id, 250), await store.allowance_take(org_id, 1)] == [7, 0]
    await _restart(store, org_id)
    assert await store.allowance_retire(org_id, 7, 60_000) == 0, "nothing of it is returned"
    assert (await store.allowance_state(org_id))[3] == 8


@real_redis
async def test_a_refill_that_read_the_counter_before_a_restart_publishes_nothing(counter):
    """A refill committed its money on what it read of the debts. A freeze sent after that
    reading says a debt arrived since; a restart takes the freeze with the counter, so the
    refill's money is not offered: the next refill reads the debts again."""
    store, org_id = counter
    await _add(store, org_id, 7, 100, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    _, seen, _, _, run = await store.allowance_state(org_id)
    await store.allowance_freeze(org_id)
    await _restart(store, org_id)
    assert await store.allowance_add(
        org_id, 7, 1_000, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen,
        run='0' * 40) == "stale"
    assert await store.allowance_take(org_id, 10) == 0
    assert (await _state(store, org_id))[0] == 0
    _, seen, _, _, run = await store.allowance_state(org_id)
    assert await store.allowance_add(
        org_id, 7, 500, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen, run=run) == 500
    assert await store.allowance_take(org_id, 10) == 7, "a refill that read the freeze thaws it"
    with pytest.raises(ValueError):
        await store.allowance_add(
            org_id, 7, 1, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0, run="not a run")


@real_redis
async def test_a_freeze_after_a_restart_holds_the_counter_built_next(counter):
    store, org_id = counter
    await _add(store, org_id, 7, 900, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    seen = (await store.allowance_state(org_id))[1]
    await _restart(store, org_id)
    await store.allowance_freeze(org_id)
    await _add(store, org_id, 7, 250, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen)
    assert await _state(store, org_id) == (250, *(await _counts(store, org_id)), 7)
    assert await store.allowance_take(org_id, 10) == 0


@real_redis
async def test_a_counter_that_names_no_run_is_one_of_an_earlier_run(counter):
    """A hash written by a build that did not name the run."""
    store, org_id = counter
    await _add(store, org_id, 7, 900, until_ms=_soon(), ttl_ms=60_000, seen_frozen=0)
    await store._client.hdel(kv._allowance_key(org_id), 'run')
    assert await store.allowance_take(org_id, 100) == 0


@real_redis
async def test_a_freeze_written_by_a_build_that_names_no_run_holds_the_counter_built_on_it(counter):
    """While builds are mixed, a process of the earlier one settles an overrun after a refill
    read the team's debts, and freezes a counter that is not there yet. The hash it leaves
    names no run. The refill's add must build on that freeze, not clear it away."""
    store, org_id = counter
    _, seen, _, _, run = await store.allowance_state(org_id)
    await store._client.hset(kv._allowance_key(org_id), mapping={'frozen': 5, 'thawed': 4})
    assert await store.allowance_add(
        org_id, 7, 250, until_ms=_soon(), ttl_ms=60_000, seen_frozen=seen, run=run) == 250
    assert await _state(store, org_id) == (250, 5, 4, 7)
    assert await store.allowance_take(org_id, 10) == 0


async def _counts(store, org_id):
    frozen, thawed = await store._client.hmget(kv._allowance_key(org_id), 'frozen', 'thawed')
    assert int(frozen) == int(thawed) + 1
    return int(frozen), int(thawed)


async def test_a_store_fault_reads_as_unavailable_and_takes_nothing(monkeypatch):
    faults = []
    monkeypatch.setattr(kv, '_note_fault', faults.append)

    async def broken(*args):
        raise ConnectionError('down')

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=broken)
    assert await store.allowance_take(5, 100) is None
    assert await store.allowance_give_back(5, 7, 100) is None
    assert await store.allowance_add(
        5, 7, 100, until_ms=_soon(), ttl_ms=1000, seen_frozen=0, run='r') is None
    assert await store.allowance_drop(5, 7) is None
    assert await store.allowance_retire(5, 7, 1000) is None
    assert await store.allowance_state(5) is None
    assert await store.allowance_state_and_pace(5) is None
    assert await store.allowance_take_up_to(5, 7, 100) is None
    assert await store.allowance_freeze(5) is None
    assert len(faults) == 9


async def test_arguments_that_are_not_plain_amounts_never_reach_the_script():
    calls = []

    async def record(*args):
        calls.append(args)
        return 1

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=record)
    for bad in (0, -1, 1.5, '1', True):
        with pytest.raises(ValueError):
            await store.allowance_take(5, bad)
    with pytest.raises(ValueError):
        await store.allowance_give_back(5, 7, -1)
    assert not calls
    assert await store.allowance_take(5, 100) == 1
    assert calls == [(kv._ALLOWANCE_TAKE, 1, 'treg:money-allowance:v1:5', 100, 0, 0,
                      kv._ALLOWANCE_ASK_AGAIN_MS)]


async def test_a_local_store_never_claims_to_be_the_shared_counter():
    store = kv.LocalStore()
    assert await store.allowance_take(5, 100) is None
    assert await store.allowance_give_back(5, 7, 100) is None
    assert await store.allowance_add(
        5, 7, 100, until_ms=_soon(), ttl_ms=1000, seen_frozen=0, run='r') is None
    assert await store.allowance_drop(5, 7) is None
    assert await store.allowance_retire(5, 7, 1000) is None
    assert await store.allowance_state(5) is None
    assert await store.allowance_state_and_pace(5) is None
    assert await store.allowance_take_up_to(5, 7, 100) is None
    assert await store.allowance_freeze(5) is None
