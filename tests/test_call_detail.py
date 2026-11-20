"""Call-reference reads preserve their contract without loading failure evidence."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from treg.infra.db import _engine, session_maker
from treg.models import AsyncTaskRecord, CallRecord, LedgerEntry, Org

REF = "a" * 32
CREATED_AT = datetime(2020, 1, 2, 3, 4, 5)


@pytest.fixture
async def org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _call(org_id: int, *, status_code: int = 200, refused_by: str | None = None) -> dict:
    view: dict = {
        "call_ref": REF, "user_email": "synthetic@example.invalid", "tool_name": "catalog",
        "method": "POST", "path": "/synthetic", "status_code": status_code, "kind": "call",
        "client": "codex", "api_key_id": 42, "api_key_name": "Synthetic key",
        "api_key_prefix": "trg_synthetic", "endpoint_id": "synthetic.submit", "provider": "synthetic",
        "credential_tier": "platform", "cost_estimated_micro": 2500,
        "cost_observed_micro": 1750 if status_code == 200 else None,
        "cost_charged_micro": 1750 if status_code == 200 else 0,
        "duration_ms": 123, "response_bytes": 456, "refused_by": refused_by,
        "budget_dim": "customer", "budget_val": "a", "tags": {"customer": "a"},
    }
    async with session_maker() as db:
        row = CallRecord(
            org_id=org_id, **view, created_at=CREATED_AT,
            error_request="synthetic request " * 4096 if status_code >= 400 else None,
            error_response="synthetic failure " * 512 if status_code >= 400 else None,
        )
        db.add(row)
        await db.commit()
        return {"id": row.id, **view, "created_at": CREATED_AT.isoformat()}


async def _ledger(org_id: int, finish: str = "settle") -> list[dict]:
    entries: list[dict] = [
        {"kind": "reserve", "amount_micro": -2500, "endpoint_id": "synthetic.submit",
         "created_at": CREATED_AT},
        {"kind": finish, "amount_micro": -1750 if finish == "settle" else 2500,
         "endpoint_id": "synthetic.submit", "created_at": CREATED_AT + timedelta(seconds=1)},
    ]
    async with session_maker() as db:
        # Insert out of order to exercise the response's chronological ordering.
        for entry in reversed(entries):
            db.add(LedgerEntry(id=uuid4().hex, org_id=org_id, call_id=REF,
                               meta={"tags": {"customer": "a"}}, **entry))
        await db.commit()
    return [{**entry, "created_at": entry["created_at"].isoformat()} for entry in entries]


async def _get_call(clients: AsyncClient, **kwargs):
    reads = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        if sql.startswith("select ") and " from callrecord " in sql:
            reads.append(sql)

    event.listen(_engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await clients.get(f"/calls/{REF}", **kwargs)
    finally:
        event.remove(_engine.sync_engine, "before_cursor_execute", capture)
    assert len(reads) == 1, "call details must not lazy-load another audit query"
    assert "error_request" not in reads[0]
    assert "error_response" not in reads[0]
    return response


@pytest.mark.parametrize("status_code,refused_by", [(200, None), (502, None), (429, "cap")])
async def test_call_detail_sql_and_response(clients, org_id, status_code, refused_by):
    view = await _call(org_id, status_code=status_code, refused_by=refused_by)
    entries = await _ledger(org_id, "settle" if status_code == 200 else "release")

    response = await _get_call(clients)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "call": view, "async_task": None, "ledger": entries,
        "charged_micro": 1750 if status_code == 200 else 0,
    }


async def test_call_detail_isolates_matching_references_by_org(clients, org_id):
    async with session_maker() as db:
        foreign = Org(name="Foreign", slug="foreign")
        db.add(foreign)
        await db.commit()
        foreign_id = foreign.id
    assert foreign_id is not None
    await _call(foreign_id, status_code=502)
    await _ledger(foreign_id)
    view = await _call(org_id)

    response = await _get_call(clients)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "call": view, "async_task": None, "ledger": [], "charged_micro": 0,
    }


@pytest.mark.parametrize("foreign_record", [False, True])
async def test_missing_or_foreign_call_is_404(clients, org_id, foreign_record):
    if foreign_record:
        async with session_maker() as db:
            foreign = Org(name="Foreign", slug="foreign")
            db.add(foreign)
            await db.commit()
            foreign_id = foreign.id
        assert foreign_id is not None
        await _call(foreign_id, status_code=502)
        await _ledger(foreign_id)

    response = await _get_call(clients)

    assert response.status_code == 404
    assert response.json() == {"detail": "no call with that id"}


@pytest.mark.parametrize("finish", ["settle", "release"])
async def test_ledger_only_call_still_returns_200(clients, org_id, finish):
    entries = await _ledger(org_id, finish)

    response = await _get_call(clients)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "call": None, "async_task": None, "ledger": entries,
        "charged_micro": 1750 if finish == "settle" else 0,
    }


@pytest.mark.parametrize("pin,expected_status", [("a", 200), ("b", 404)])
async def test_call_detail_keeps_pinned_scope(clients, org_id, pin, expected_status):
    view = await _call(org_id, status_code=502)
    agent = await clients.post(f"/orgs/{org_id}/agents", json={
        "name": "reader", "role": "viewer", "pinned_tags": {"customer": pin},
    })
    assert agent.status_code == 200, agent.text

    response = await _get_call(clients, headers={"X-Treg-Token": agent.json()["token"]})

    assert response.status_code == expected_status, response.text
    assert response.json() == (
        {"call": view, "async_task": None, "ledger": [], "charged_micro": 0}
        if expected_status == 200 else {"detail": "no call with that id"}
    )


@pytest.mark.parametrize("status,settled", [("pending", None), ("settled", 1750), ("released", 0)])
async def test_call_detail_keeps_async_view(clients, org_id, status, settled):
    view = await _call(org_id)
    completed_at = CREATED_AT + timedelta(seconds=1) if status != "pending" else None
    error = "synthetic failure" if status == "released" else ""
    async with session_maker() as db:
        db.add(AsyncTaskRecord(
            call_id=REF, org_id=org_id, provider="synthetic", endpoint_id="synthetic.submit",
            task_id="synthetic-task", reserved_micro=2500, status=status, settled_micro=settled,
            created_at=CREATED_AT, next_check_at=CREATED_AT, completed_at=completed_at,
            error=error, tags={"customer": "a"},
        ))
        await db.commit()

    response = await _get_call(clients)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "call": {**view, "cost_charged_micro": settled}, "ledger": [], "charged_micro": 0,
        "async_task": {
            "status": status, "task_id": "synthetic-task", "reserved_micro": 2500,
            "settled_micro": settled, "created_at": CREATED_AT.isoformat(),
            "completed_at": completed_at.isoformat() if completed_at else None,
            "error": error or None, "result_url": None, "fetch_command": None, "ttl_note": None,
        },
    }
