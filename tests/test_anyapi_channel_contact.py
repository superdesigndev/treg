"""AnyAPI's channel contact lookup answers 200 with the email, or 202 once a second source has the
lookup: already charged, with a keyless `collectUrl` the caller reads for the answer. Both are
finished calls for treg: the charge is the answer's `costUsd`, and the body reaches the caller as
AnyAPI sent it. With no caller ceiling treg sends AnyAPI no `max_cost_usd`, so the dearer second
source stays eligible above the listed price. Neither answer is ever archived: a 202 carries the
caller's own keyless collect token, which a cache hit would hand to another team."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import api as A
from treg import archive
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot

from test_marketplace_call import _balance, _dropleads_response, _entries

EP = "anyapi.youtube.channel_contact"

ANSWERED = {"output": {"email": "biz@example.com"}, "provider": "AnyAPI", "costUsd": 0.132}
HANDED_OFF = {
    "requestId": "req_123",
    "sku": "youtube.channel_contact",
    "status": "pending",
    "createdAt": "2026-11-11T00:00:00Z",
    "retryAfterSeconds": 5,
    "costUsd": 0.33,
    "collectUrl": "https://api.getanyapi.com/v1/requests/req_123/collect?token=abc",
    "note": "Collecting a pending request",
}


@pytest.fixture
def anyapi_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ANYAPI", "PLATFORM-ANYAPI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "anyapi")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("mode", ["shadow", "serve"])
@pytest.mark.parametrize(("status", "doc", "usd"), [(200, ANSWERED, 0.132), (202, HANDED_OFF, 0.33)])
async def test_both_answers_settle_at_reported_cost_pass_through_and_are_never_archived(
    clients: AsyncClient, anyapi_on, monkeypatch, mode, status, doc, usd,
):
    monkeypatch.setattr(get_settings(), "archive_mode", mode)
    before = await _balance(clients)
    sent: list[httpx.Request] = []

    def serve(request: httpx.Request):
        sent.append(request)
        return _dropleads_response(status, doc)

    original = A.app.state.http
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
            A.app.state.http = upstream
            response = await clients.post(f"/call/{EP}", json={"handle": "@mkbhd"})
    finally:
        A.app.state.http = original
    assert response.status_code == status, response.text
    assert [request.url.path for request in sent] == ["/v1/run/youtube.channel_contact"]
    assert "max_cost_usd" not in sent[0].url.params
    assert response.json() == doc
    charged = ledger.with_margin(round(usd * 1_000_000))
    assert response.headers["x-treg-cost-micro"] == str(charged)
    assert await _balance(clients) == before - charged
    kinds = [entry["kind"] for entry in await _entries(clients)]
    assert "settle" in kinds and "release" not in kinds

    # The archive may note that the question was asked, never keep or serve the answer: the same
    # question again goes to AnyAPI, not to the stored body.
    original = A.app.state.http
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
            A.app.state.http = upstream
            again = await clients.post(f"/call/{EP}", json={"handle": "@mkbhd"})
    finally:
        A.app.state.http = original
    assert again.status_code == status and len(sent) == 2
    await archive.drain()
    async with session_maker() as session:
        keys = (await session.execute(select(ArchiveKey))).scalars().all()
        snaps = (await session.execute(select(ArchiveSnapshot))).scalars().all()
    assert {key.policy for key in keys} <= {"forbidden"}
    assert all(snap.body is None and snap.body_storage is None for snap in snaps)
