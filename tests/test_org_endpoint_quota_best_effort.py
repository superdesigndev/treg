"""Temporary Redis limits fail open without rewriting unknown state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import funded_user
from redis.exceptions import ConnectionError as RedisConnectionError

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra import org_endpoint_quota

from test_marketplace_call import EP, EP_MICRO, _balance, _fake_relay, platform_on  # noqa: F401
from test_capacity_smoothing import _relay_script
from test_org_endpoint_quota_redis import put_rule, state, store  # noqa: F401


def _use_store(monkeypatch, store):
    clock = [100.0]
    monkeypatch.setattr(org_endpoint_quota, "monotonic", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    monkeypatch.setattr(org_endpoint_quota, "_store", store)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"answer":"synthetic"}'))
    monkeypatch.setattr(call_service, "_quota_warning_at", None)
    return clock


def test_unavailable_warning_is_sanitized_and_limited_to_once_per_minute(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(call_service, "_quota_warning_at", None)
    monkeypatch.setattr(call_service, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    for now in (0.0, 0.0, 59.9, 60.0, 60.1):
        clock[0] = now
        call_service._warn_quota_unavailable()
    messages = [record.getMessage() for record in caplog.records
                if record.name == "treg.org_endpoint_quota"]
    assert messages == [
        "Temporary endpoint quota state unavailable; allowing logical call",
    ] * 2


@pytest.mark.parametrize("loss", ["namespace", "rule", "count", "expiry"])
async def test_lost_or_unknown_rule_allows_without_reinitializing(
    clients, monkeypatch, platform_on, store, loss,
):
    _use_store(monkeypatch, store)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    await put_rule(store, org=org_id, endpoint=EP, limit=0)
    url = f"/call/{EP}?aweme_id=7"
    assert (await clients.get(url)).status_code == 429
    field = f"rule:{org_id}:{EP}"
    if loss == "namespace":
        await store._client.delete(org_endpoint_quota.KEY)
    elif loss == "rule":
        await store._client.hdel(org_endpoint_quota.KEY, field)
    elif loss == "count":
        await store._client.hset(org_endpoint_quota.KEY, field,
                                 '{"version":1,"limit":0,"enabled":true}')
    else:
        await store._client.expire(org_endpoint_quota.KEY, 60)
    before_rule = await store._client.hgetall(org_endpoint_quota.KEY)
    before_balance = await _balance(clients)
    response = await clients.get(url)
    assert response.status_code == 200, response.text
    assert before_balance - await _balance(clients) == EP_MICRO
    assert await store._client.hgetall(org_endpoint_quota.KEY) == before_rule


async def test_outage_allows_limited_and_unconfigured_orgs_then_recovers(
    clients, monkeypatch, platform_on, store, caplog,
):
    clock = _use_store(monkeypatch, store)
    events = []
    monkeypatch.setattr(call_service.analytics, "capture", lambda distinct, event, properties=None, **kw:
                        events.append(properties) if event == "org_endpoint_quota_fail_open" else None)
    monkeypatch.setattr(call_service.analytics, "enabled", lambda: True)
    monkeypatch.setattr(call_service.analytics, "_ensure_flusher", lambda: None)
    monkeypatch.setattr(call_service.analytics, "_quota_window", None)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    other = await funded_user(clients, "synthetic-quota-unrestricted@example.com")
    await put_rule(store, org=org_id, endpoint=EP, limit=0)
    url = f"/call/{EP}?aweme_id=7"
    assert (await clients.get(url)).status_code == 429
    original_eval = store._client.eval
    unavailable = AsyncMock(side_effect=RedisConnectionError("synthetic-secret-url"))
    monkeypatch.setattr(store._client, "eval", unavailable)
    assert (await clients.get(url)).status_code == 200
    assert (await clients.get(url, headers={"X-Treg-Token": other["token"]})).status_code == 200
    assert unavailable.await_count == 1
    assert events == []
    messages = [record.getMessage() for record in caplog.records
                if record.name == "treg.org_endpoint_quota"]
    assert len(messages) == 1 and "allowing" in messages[0]
    assert "synthetic-secret-url" not in caplog.text
    monkeypatch.setattr(store._client, "eval", original_eval)
    assert (await clients.get(url)).status_code == 200
    assert (await state(store, org=org_id, endpoint=EP))["used"] == 0
    clock[0] += 5
    assert (await clients.get(url)).status_code == 429
    assert (await clients.get(url, headers={"X-Treg-Token": other["token"]})).status_code == 200
    assert (await state(store, org=org_id, endpoint=EP))["used"] == 0
    call_service.analytics._emit_quota_summary(force=True)
    assert events == [{
        "admissions": 3, "target_count": 3,
        "target_examples": [
            {"org_id": org_id, "endpoint_id": EP},
            {"org_id": other["org_id"], "endpoint_id": EP},
        ],
        "targets_truncated": False,
    }]


async def test_uncertain_batch_is_admitted_once_without_extra_commit_or_retry(monkeypatch):
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    parent, child = (42, "example.route"), (42, "example.read")
    context = SimpleNamespace(quota_pending=(parent,), quota_admissions=set())
    db = SimpleNamespace(in_transaction=lambda: False, commit=AsyncMock())
    request = SimpleNamespace(context=context, caller=SimpleNamespace(org_id=42), db=db,
                              state=SimpleNamespace())
    uncertain = AsyncMock(return_value=("unavailable", parent))
    monkeypatch.setattr(org_endpoint_quota, "admit_many", uncertain)
    await call_service._admit_org_endpoint(request, child[1])
    await call_service._admit_org_endpoint(request, child[1])
    assert context.quota_admissions == {parent, child}
    uncertain.assert_awaited_once_with((parent, child))
    db.commit.assert_not_awaited()


async def test_lost_increment_response_allows_once_through_retry_and_replay(
    clients, monkeypatch, platform_on, store,
):
    clock = _use_store(monkeypatch, store)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    await put_rule(store, org=org_id, endpoint=EP, limit=1)
    original_eval = store._client.eval

    async def uncertain(*args, **kwargs):
        await original_eval(*args, **kwargs)
        raise RedisConnectionError("synthetic response lost after increment")

    evaluation = AsyncMock(side_effect=uncertain)
    monkeypatch.setattr(store._client, "eval", evaluation)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([
        (429, ((b"retry-after", b"0"),), b'{"detail":"slow"}'),
        (200, (), b'{"ok":true}'),
        (200, (), b'{"ok":true}'),
    ], seen))
    url = f"/call/{EP}?aweme_id=7"
    headers = {"Idempotency-Key": "synthetic-uncertain-quota"}
    assert (await clients.get(url, headers=headers)).status_code == 200
    replay = await clients.get(url, headers=headers)
    assert replay.status_code == 200 and replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert len(seen) == 2 and evaluation.await_count == 1
    assert (await state(store, org=org_id, endpoint=EP))["used"] == 1
    monkeypatch.setattr(store._client, "eval", original_eval)
    assert (await clients.get(url)).status_code == 200
    assert (await state(store, org=org_id, endpoint=EP))["used"] == 1
    clock[0] += 5
    assert (await clients.get(url)).status_code == 429
    assert len(seen) == 3
