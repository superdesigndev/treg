"""Shared fair-share accounting, with availability-first local fallback.

One coordinator per process/account batches waiter checks and active lease renewals. Uncertain
ownership never stops a relay, never reacquires a token, and is cleaned up with that token only.
Local shadow counts include shared calls so fallback cannot forget this process's active work.
Global exclusion is deliberately not promised during fallback, mixed modes, or lease loss.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from ...infra import provider_store
from .fair_share import ShareBusy

_cleanup_tasks: set[asyncio.Task] = set()


def cleanup(awaitable) -> asyncio.Task:
    """Keep bounded owner cleanup alive even if its caller is cancelled a second time."""
    task = asyncio.create_task(awaitable)
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    return task


class SharedSlot:
    def __init__(self, pool, token, team, since, valid_until):
        self.pool, self.token, self.team = pool, token, team
        self.provider = pool.name
        self.waited_ms = int((time.monotonic() - since) * 1000)
        self.valid_until = valid_until
        self.owned = True
        self._closing = None
        pool.gate._pool(pool.name, pool.cap).take(team)
        pool.slots[token] = self
        pool.start()

    def release(self):
        if self._closing is None:
            self.pool.slots.pop(self.token, None)
            self.pool.gate._release(self.provider, self.team)
            self.pool.stop_if_idle()
            self._closing = cleanup(self.pool.store.abandon([self.provider], self.token, self.pool.cap))

    async def aclose(self):
        self.release()
        await asyncio.shield(self._closing)


class _Pool:
    def __init__(self, gate, store, name, cap):
        self.gate, self.store, self.name, self.cap = gate, store, name, cap
        self.slots: dict[str, SharedSlot] = {}
        self.waiters: dict[str, tuple[int, float, float, asyncio.Future]] = {}
        self.task = None
        self.wake = asyncio.Event()

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def stop_if_idle(self):
        if not self.slots and not self.waiters:
            if self.task is not None and self.task is not asyncio.current_task():
                self.task.cancel()
            if self.gate._shared.get(self.name) is self:
                self.gate._shared.pop(self.name, None)

    async def run(self):
        try:
            while self.slots or self.waiters:
                owned = [s for s in self.slots.values() if s.owned]
                if not owned and not self.waiters:
                    return
                delay = provider_store.POLL_S if self.waiters else provider_store.RENEW_S
                if owned:
                    delay = min(delay, max(0, min(s.valid_until for s in owned)
                                           - provider_store.LEASE_MS / 1000 + provider_store.RENEW_S
                                           - time.monotonic()))
                try:
                    await asyncio.wait_for(self.wake.wait(), delay)
                except TimeoutError:
                    pass
                self.wake.clear()
                started = time.monotonic()
                owned = [s for s in self.slots.values() if s.owned]
                due = any(started >= s.valid_until - provider_store.LEASE_MS / 1000 + provider_store.RENEW_S
                          for s in owned)
                # Renew the whole local cohort when its first lease is due. Per-token due tests
                # turn staggered admissions into one heartbeat per call rather than per pool.
                renew = [s.token for s in owned] if due else []
                lost = 0
                for token in tuple(renew):
                    slot = self.slots[token]
                    if started >= slot.valid_until:
                        slot.owned = False
                        renew.remove(token)
                        lost += 1
                if lost:
                    logging.getLogger("treg.capacity").warning(
                        "provider admission lease expired; %d relays continue with local accounting", lost)
                waiting = list(self.waiters)
                if not renew and not waiting:
                    continue
                result = await self.store.update(self.name, self.cap, renew=renew, waiting=waiting,
                                                 lease_ms=provider_store.LEASE_MS)
                timely = time.monotonic() < started + provider_store.LEASE_MS / 1000
                lost = 0
                for token in renew:
                    slot = self.slots.get(token)
                    if slot is None:
                        continue  # Released during I/O; renewal never recreates it.
                    if result is None or result.get(token, (None,))[0] != "acquired" or not timely:
                        slot.owned = False
                        lost += 1
                    else:
                        slot.valid_until = started + provider_store.LEASE_MS / 1000
                if lost:
                    logging.getLogger("treg.capacity").warning(
                        "provider admission ownership uncertain; %d relays continue with local accounting", lost)
                for token in waiting:
                    waiter = self.waiters.get(token)
                    if waiter is None:
                        continue
                    team, since, deadline, future = waiter
                    if future.done():
                        continue
                    status, active = result.get(token, ("missing", 0)) if result is not None else (None, 0)
                    if status == "waiting":
                        continue
                    self.waiters.pop(token, None)
                    if status == "acquired" and timely:
                        future.set_result(SharedSlot(self, token, team, since,
                                                     started + provider_store.LEASE_MS / 1000))
                    elif status is None or not timely or (status == "missing" and time.monotonic() < deadline):
                        future.set_result(None)  # Availability-first fallback, with remaining budget.
                    else:
                        future.set_exception(ShareBusy(self.name, team, active, time.monotonic() - since))
        except asyncio.CancelledError:
            raise
        except Exception:  # A coordinator fault must not strand waiters or abort an active relay.
            for slot in self.slots.values():
                slot.owned = False
            for _, _, _, future in self.waiters.values():
                if not future.done():
                    future.set_result(None)
        finally:
            self.stop_if_idle()


async def acquire(gate, store, pools, team, cap, max_wait_s):
    since = time.monotonic()
    if since < store.unavailable_until:
        # No command was sent, so there is no ambiguous owner to clean up. In an outage this
        # prevents each fallback call from defeating the store's short circuit breaker.
        return await gate._acquire_local(min(pools, key=gate.queued), team, cap, max_wait_s=max_wait_s)
    token = f"{team}:{uuid.uuid4().hex}"
    pool = None
    future = None
    handed = False
    selected = pools[0]
    try:
        result = await store.claim(pools, token, cap, max(1, int(max_wait_s * 1000)),
                                   lease_ms=provider_store.LEASE_MS)
        if result is not None:
            index, status, active = result
            selected = pools[index]
            pool = gate._shared.get(selected)
            if pool is None or pool.store is not store:
                pool = gate._shared[selected] = _Pool(gate, store, selected, cap)
            pool.cap = cap
            if status == "acquired" and time.monotonic() < since + provider_store.LEASE_MS / 1000:
                slot = SharedSlot(pool, token, team, since, since + provider_store.LEASE_MS / 1000)
                slot.waited_ms = 0  # An uncontended store round trip is not a fair-share wait.
                handed = True
                return slot
            remaining = max(0, since + max_wait_s - time.monotonic())
            if status == "full":
                # Bounded queue overload uses the existing busy response, after the normal wait.
                await asyncio.sleep(remaining)
                raise ShareBusy(selected, team, active, time.monotonic() - since)
            if status == "waiting":
                future = asyncio.get_running_loop().create_future()
                was_waiting = bool(pool.waiters)
                pool.waiters[token] = (team, since, since + max_wait_s, future)
                pool.start()
                if not was_waiting:
                    pool.wake.set()
                try:
                    slot = await asyncio.wait_for(asyncio.shield(future), remaining)
                except TimeoutError:
                    # A grant and timeout can coincide; retain a confirmed grant, as locally.
                    if future.done() and not future.cancelled():
                        slot = future.result()
                    else:
                        raise ShareBusy(selected, team, active, time.monotonic() - since) from None
                if slot is not None:
                    handed = True
                    return slot
    finally:
        if not handed:
            if pool is not None:
                pool.waiters.pop(token, None)
                pool.stop_if_idle()
            if future is not None:
                if future.done() and not future.cancelled() and future.exception() is None:
                    if (slot := future.result()) is not None:
                        await slot.aclose()
                else:
                    future.cancel()
            await asyncio.shield(cleanup(store.abandon(pools, token, cap)))
    # No new remote claim for this call after an ambiguous reply. Local fallback also sees
    # shared calls already active in this process. Recovery affects new calls only.
    selected = min(pools, key=gate.queued)
    return await gate._acquire_local(selected, team, cap,
                                     max_wait_s=max(0, since + max_wait_s - time.monotonic()))


async def close(gate):
    pools = list(gate._shared.values())
    tasks = [pool.task for pool in pools if pool.task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for pool in pools:
        for slot in list(pool.slots.values()):
            await slot.aclose()
        for token, (_, _, _, future) in list(pool.waiters.items()):
            if not future.done():
                future.cancel()
            await pool.store.abandon([pool.name], token, pool.cap)
        pool.waiters.clear()
    gate._shared.clear()
    if _cleanup_tasks:
        await asyncio.gather(*tuple(_cleanup_tasks), return_exceptions=True)
