from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from conftest import funded_user
from treg.application.call import reserve as call_reserve
from treg.application.call import service as call_service
from treg.application.call.types import ReservationFailed
from treg.domain import money as ledger
from treg.infra import call_db_admission
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry

from test_hub import _script_manifest, _steps_manifest, hub_on  # noqa: F401
from test_marketplace_call import EP, EP_MICRO, _fake_relay, platform_on  # noqa: F401
from test_routing import ROUTED, enrichment_on  # noqa: F401


def _reject_reserves_after(monkeypatch, allowed):
    attempts = []

    @asynccontextmanager
    async def admit(operation):
        if operation == "reserve":
            attempts.append(operation)
            if len(attempts) > allowed:
                raise call_db_admission.CallDBAdmissionRejected(operation)
        yield

    monkeypatch.setattr(call_db_admission, "admit", admit)
    return attempts


async def test_reserve_entry_rejection_is_a_treg_overload(monkeypatch):
    attempts = _reject_reserves_after(monkeypatch, 0)
    with pytest.raises(ReservationFailed) as caught:
        async with call_reserve.reserve_admission(1):
            pytest.fail("a rejected reservation must not enter its database phase")
    assert attempts == ["reserve"]
    assert (caught.value.kind, caught.value.blame, caught.value.status_code,
            caught.value.retry_after_s) == ("db_admission", "treg", 503, 2)


async def test_reserve_does_not_relabel_an_exception_from_its_body(monkeypatch):
    _reject_reserves_after(monkeypatch, 1)
    failure = call_db_admission.CallDBAdmissionRejected("body")
    with pytest.raises(call_db_admission.CallDBAdmissionRejected) as caught:
        async with call_reserve.reserve_admission(1):
            raise failure
    assert caught.value is failure


async def test_routed_overload_stops_before_fallback(clients, enrichment_on, monkeypatch):
    attempts = _reject_reserves_after(monkeypatch, 0)

    async def no_upstream(*args, **kwargs):
        pytest.fail("no candidate may call upstream after the shared reserve refusal")

    monkeypatch.setattr(call_service, "relay", no_upstream)
    response = await clients.post(
        f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,findymail"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "db_admission"
    assert response.headers["Retry-After"] == "2"
    assert attempts == ["reserve"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


@pytest.mark.parametrize("kind", ["steps", "script"])
async def test_hub_overload_preserves_completed_charge_and_refunds_price(
    clients, hub_on, platform_on, monkeypatch, kind,
):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": [1]}'))
    if kind == "steps":
        manifest = _steps_manifest(
            uses=[EP], steps=[
                {"name": "first", "call": EP, "input": {"aweme_id": "$input.domain"}},
                {"name": "second", "call": EP, "input": {"aweme_id": "$first.data"},
                 "allow_fail": True},
                {"name": "third", "call": EP, "input": {"aweme_id": "$second.data"}},
            ], output={"rows": "$first.data"}, price_usd=0.01)
        inputs = {"domain": "example.com"}
        script = {}
    else:
        manifest = _script_manifest(uses=[EP], pricing={"max_price_usd": 0.01})
        inputs = {}
        script = {"script": (
            "export default async function run(ctx) {"
            f"const first = await ctx.call('{EP}', {{query: {{aweme_id: 'first'}}}});"
            f"await ctx.call('{EP}', {{query: {{aweme_id: 'second'}}}});"
            f"await ctx.call('{EP}', {{query: {{aweme_id: 'third'}}}});"
            "return {rows: first.json.data, count: 1};}")}
    published = await clients.post("/hub/tools", json={
        "manifest": manifest, "check": {"inputs": inputs, "fields": ["rows"]},
        "readme": "Local admission regression", **script})
    assert published.status_code == 201, published.text
    assert published.json()["status"] == "live", published.text
    buyer = await funded_user(clients, "admission-buyer@example.invalid")
    attempts = _reject_reserves_after(monkeypatch, 2)
    headers = {"X-Treg-Token": buyer["token"], "Idempotency-Key": "admission-hub"}
    response = await clients.post(f"/call/{published.json()['tool_id']}", json=inputs,
                                  headers=headers)
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == "2"
    detail = response.json()["detail"]
    assert detail["error"] == "db_admission"
    assert detail["charged_micro"] == EP_MICRO
    assert detail["price_micro"] == 0
    assert [step["status"] for step in detail["trace"]] == [200, 503]
    replay = await clients.post(f"/call/{published.json()['tool_id']}", json=inputs, headers=headers)
    assert replay.status_code == 503
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert replay.json()["detail"]["run_id"] == detail["run_id"]
    assert attempts == ["reserve"] * 3
    async with session_maker() as db:
        assert await ledger.balance_of(db, buyer["org_id"]) == 1_000_000 - EP_MICRO
        assert (await db.execute(select(Hold).where(Hold.org_id == buyer["org_id"]))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == f"{detail['run_id']}:price",
            LedgerEntry.kind == "release"))).scalars().all()
        assert len(entries) == 1
