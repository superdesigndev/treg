"""Harmar catalog: per-media-second pricing, reserve at the 60-minute cap, settle on seconds_charged."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlmodel import select

from treg.application import asynctasks as task_app
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, Org
from treg.timeutil import utcnow_naive


def _response(status: int, document: object) -> UpstreamResponse:
    body = json.dumps(document).encode()

    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)


@pytest.fixture
def harmar_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_HARMAR", "test-platform-harmar")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "harmar")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("endpoint_id", ["harmar.speech.transcribe", "harmar.speech.captions.render"])
async def test_harmar_paid_rows_are_platform_priced_per_media_second(clients, endpoint_id):
    response = await clients.get(f"/catalog/endpoints/{endpoint_id}")
    assert response.status_code == 200
    endpoint = response.json()["endpoint"]
    assert endpoint["platform_eligible"] is True
    # $54.99 / 30,000 s (Starter, the dearest pack); the advertised figure is one minute of media.
    assert endpoint["cost"]["usd"] == pytest.approx(60 * 0.001833)


async def test_harmar_balance_is_never_platform_eligible(clients):
    response = await clients.get("/catalog/endpoints/harmar.account.balance")
    assert response.status_code == 200
    assert response.json()["endpoint"]["platform_eligible"] is False


@pytest.mark.parametrize(("terminal_status", "seconds", "task_status", "settled_micro"), [
    ("completed", 10, "settled", 18_330),
    ("failed", None, "released", 0),
])
async def test_harmar_transcribe_settles_on_terminal_seconds_charged(
    clients, monkeypatch, harmar_platform, terminal_status, seconds, task_status, settled_micro,
):
    async def submit(*_args, **_kwargs):
        return _response(202, {"id": "harmar-job-1", "status": "processing",
                               "duration_seconds": 9.4, "seconds_charged": 10})

    # The reserve is the 60-minute ceiling ($6.60); the signup promo does not cover it.
    async with session_maker() as db:
        for org in (await db.execute(select(Org))).scalars():
            await money.grant(db, org.id, amount_micro=10_000_000, kind="promotional", once=False)
        await db.commit()

    monkeypatch.setattr(call_service, "relay", submit)
    response = await clients.post("/call/harmar.speech.transcribe", json={
        "media_url": "https://cdn.harmar.ai/api-samples/hy-10s.mp4",
    })
    assert response.status_code in (200, 202), response.text

    async with session_maker() as db:
        row = (await db.execute(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_id == "harmar-job-1"))).scalar_one()
        assert row.reserved_micro == 3600 * 1_833
        assert row.settlement_basis["amount"] == {
            "kind": "usage", "path": "seconds_charged", "unit": "media_second", "unit_micro": 1_833,
        }
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        call_id = row.call_id
        await db.commit()

    async def terminal(_row, _client):
        document = {"id": "harmar-job-1", "status": terminal_status, "duration_seconds": 9.4}
        if seconds is not None:
            document |= {"seconds_charged": seconds, "text": "Բարև"}
        else:
            document |= {"error": "processing_failed"}
        return 200, json.dumps(document).encode()

    monkeypatch.setattr(task_app, "_poll", terminal)
    result = await task_app.settle_due()
    assert result.settled == (task_status == "settled")
    assert result.released == (task_status == "released")
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        assert row.status == task_status
        assert row.settled_micro == settled_micro
