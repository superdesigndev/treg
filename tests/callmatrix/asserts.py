"""Shared four-book assertions for every call matrix case."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import Any, Mapping

from httpx import AsyncClient, Response

from treg import audit

from test_marketplace_call import _balance, _entries, _telemetry

from .provider import FakeProvider


@dataclass(frozen=True)
class Snapshot:
    balance_micro: int
    entry_ids: frozenset[str]
    hit_count: int


@dataclass(frozen=True)
class Expect:
    status: int
    body: bytes | None = None
    cost_micro: int | None = None
    treg_error: bool = False
    balance_delta: int = 0
    ledger_kinds: tuple[str, ...] = ()
    ledger_reason: str | None = None
    audit: Mapping[str, Any] = field(default_factory=dict)
    upstream_hits: int = 1


async def snapshot(clients: AsyncClient, provider: FakeProvider) -> Snapshot:
    entries = await _entries(clients)
    return Snapshot(
        balance_micro=await _balance(clients),
        entry_ids=frozenset(entry["id"] for entry in entries),
        hit_count=len(provider.hits),
    )


async def assert_outcome(
    clients: AsyncClient,
    provider: FakeProvider,
    response: Response,
    before: Snapshot,
    expect: Expect,
) -> dict:
    """Check the HTTP response, money journal, audit row, and provider hit book."""
    # Diagnostic guard for the CI-only stall: if the audit queue cannot drain, dump every pending
    # task's coroutine stack and the pool state instead of hanging until the runner kills the job.
    try:
        await asyncio.wait_for(audit.drain(), timeout=90)
    except asyncio.TimeoutError:
        import traceback
        from treg.infra.db import _engine
        lines = [f"audit.drain stalled; pending={len(audit._pending)}"]
        try:
            lines.append(f"pool: {_engine.pool.status()}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"pool status unavailable: {exc}")
        for task in list(audit._pending):
            lines.append(f"--- pending task {task!r}")
            for frame in task.get_stack():
                lines.extend(traceback.format_stack(frame, limit=3))
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                lines.append(f"--- live task {task!r}")
                for frame in task.get_stack():
                    lines.extend(traceback.format_stack(frame, limit=2))
        raise AssertionError("\n".join(lines))

    assert response.status_code == expect.status, response.text
    if expect.body is not None:
        assert response.content == expect.body
    call_id = response.headers.get("X-Treg-Call-Id")
    assert (response.headers.get("X-Treg-Error") == "1") is expect.treg_error
    if expect.cost_micro is None:
        assert "X-Treg-Cost-Micro" not in response.headers
    else:
        assert response.headers.get("X-Treg-Cost-Micro") == str(expect.cost_micro)

    balance = await _balance(clients)
    assert balance - before.balance_micro == expect.balance_delta
    balance_view = (await clients.get("/orgs")).json()[0]
    detail = await clients.get(f"/orgs/{balance_view['org_id']}/balance")
    assert detail.status_code == 200, detail.text
    assert detail.json()["holds"] == [], "a completed call must not leave an open hold"

    entries = await _entries(clients)
    fresh = [entry for entry in entries if entry["id"] not in before.entry_ids]
    assert tuple(entry["kind"] for entry in fresh) == expect.ledger_kinds
    if expect.ledger_reason is not None:
        release = next(entry for entry in fresh if entry["kind"] == "release")
        assert release["meta"]["reason"] == expect.ledger_reason, release["meta"]

    row = await _telemetry(clients)
    if call_id is not None:
        assert row["call_ref"] == call_id
    assert row["status_code"] == expect.status
    for key, value in expect.audit.items():
        assert row[key] == value, f"audit {key}: {row[key]!r} != {value!r}"

    assert len(provider.hits) - before.hit_count == expect.upstream_hits
    assert call_id, "every /call response must carry X-Treg-Call-Id"
    return row
