"""Hub run caps across separate processes, against disposable loopback Redis."""
import asyncio
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

_WORKER = """
import asyncio, sys
from treg.application.hub import limits
from treg.infra import kv

async def _until(store, key, cohort):
    for _ in range(1500):
        if int(await store._client.get(key) or 0) >= cohort:
            return
        await asyncio.sleep(0.01)
    raise RuntimeError(f"{key} stuck at {await store._client.get(key)}")

async def main(team, tool, runs, cohort):
    store = kv.store()
    ready, decided = f"{tool}:ready", f"{tool}:decided"
    probe = f"{tool}:in-progress"
    peak, served, refused = 0, 0, 0

    async def one():
        nonlocal peak, served, refused
        await store._client.incr(ready)
        await _until(store, ready, cohort)
        try:
            async with limits.slot(team, tool):
                peak = max(peak, await store._client.incr(probe))
                await store._client.incr(decided)
                await _until(store, decided, cohort)
                await store._client.decr(probe)
                served += 1
        except limits.HubBusy:
            await store._client.incr(decided)
            refused += 1

    await asyncio.gather(*(one() for _ in range(runs)))
    await asyncio.sleep(0.1)
    await kv.close()
    print(peak, served, refused)

asyncio.run(main(int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])))
"""


@pytest.fixture
def redis_url():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    return url


async def _run(redis_url: str, *, teams: list[int], tool: str, runs: int, tool_cap: int):
    env = {**os.environ, "TREG_KV_URL": redis_url, "TREG_HUB_TOOL_MAX_RUNS": str(tool_cap)}
    cohort = len(teams) * runs
    procs = [await asyncio.create_subprocess_exec(
        sys.executable, "-c", _WORKER, str(team), tool, str(runs), str(cohort), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for team in teams]
    outs = await asyncio.gather(*(asyncio.wait_for(p.communicate(), 30) for p in procs))
    rows = []
    for proc, (out, err) in zip(procs, outs):
        assert proc.returncode == 0, err.decode()[-2000:]
        rows.append([int(x) for x in out.split()])
    return rows


async def test_one_team_in_several_processes_never_passes_the_team_cap(redis_url):
    from treg.application.hub import limits
    team = int(uuid4().int % 1_000_000) + 1_000_000
    rows = await _run(redis_url, teams=[team] * 3, tool=f"t-{uuid4().hex}", runs=4, tool_cap=0)
    served = sum(r[1] for r in rows)
    assert served == limits.MAX_RUNS_PER_TEAM
    assert sum(r[2] for r in rows) == 12 - limits.MAX_RUNS_PER_TEAM


async def test_one_tool_in_several_processes_never_passes_the_tool_cap(redis_url):
    base = int(uuid4().int % 1_000_000) + 2_000_000
    rows = await _run(redis_url, teams=[base, base + 1, base + 2], tool=f"t-{uuid4().hex}", runs=3,
                      tool_cap=5)
    assert max(r[0] for r in rows) <= 5
    assert sum(r[1] for r in rows) == 5
