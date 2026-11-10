"""A provider account's concurrency cap across separate processes, against disposable loopback Redis."""
import asyncio
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from treg.infra import kv

_WORKER = """
import asyncio, sys
from treg.domain.capacity.fair_share import FairShare, ShareBusy
from treg.infra import kv

async def main(account, cap, calls):
    gate, store = FairShare(), kv.store()
    probe = f"{account}:in-progress"
    peak, served = 0, 0

    async def one(team):
        nonlocal peak, served
        try:
            slot = await gate.acquire(account, team, cap, max_wait_s=5)
        except ShareBusy:
            return
        try:
            peak = max(peak, await store._client.incr(probe))
            await asyncio.sleep(0.05)
            served += 1
        finally:
            await store._client.decr(probe)
            slot.release()

    await asyncio.gather(*(one(n % 3) for n in range(calls)))
    await asyncio.sleep(0.1)
    await kv.close()
    print(peak, served)

asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
"""


@pytest.fixture
def redis_url():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    return url


async def test_separate_processes_never_exceed_the_account_cap(redis_url):
    account, cap, calls = f"treg-test-{uuid4().hex}", 4, 12
    env = {**os.environ, "TREG_KV_URL": redis_url}
    procs = [await asyncio.create_subprocess_exec(
        sys.executable, "-c", _WORKER, account, str(cap), str(calls), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for _ in range(3)]
    outs = await asyncio.gather(*(asyncio.wait_for(p.communicate(), 30) for p in procs))
    for p, (_, err) in zip(procs, outs):
        assert p.returncode == 0, err.decode()
    results = [tuple(map(int, out.split())) for out, _ in outs]
    assert max(peak for peak, _ in results) <= cap, "each process alone would allow `cap`"
    assert sum(served for _, served in results) == 3 * calls, "every call is served within its wait"
    store = kv.RedisStore(redis_url)
    try:
        assert await store._client.zcard(f"treg:provider-concurrency:v1:{account}") == 0
        await store._client.delete(f"{account}:in-progress")
    finally:
        await store.aclose()
