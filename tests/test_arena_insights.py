"""Synthetic database evidence only; no production metrics or network calls."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from treg.application import arena_insights as service
from treg.domain import arena_insights as rules
from treg.domain.catalog import store
from treg.infra.db import session_maker
from treg.models import ArenaInsightState, ArenaObservation, ArchiveKey, ArchiveSnapshot, CallRecord
from treg.timeutil import utcnow_naive as now

EP = "tomba.people.email.find"


async def record(hash_, *, status=200, response=None, error=None, cached=False, tier="platform", ago=180, duration=100):
    async with session_maker() as db:
        row = CallRecord(user_email="synthetic@example.test", tool_name=EP, method="GET", path="/v1/email-finder",
            status_code=status, endpoint_id=EP, provider="tomba", credential_tier=tier, params_hash=hash_,
            error_request="?full_name=Example+Person&domain=example.test", error_response=error,
            hit=True, cached=cached, duration_ms=duration, created_at=now()-timedelta(seconds=ago))
        db.add(row)
        await db.flush()
        if response is not None:
            key = ArchiveKey(key_hash=f"synthetic-{row.id}", endpoint_id=EP,
                req_url="https://api.tomba.io/v1/email-finder?full_name=Example+Person&domain=example.test")
            db.add(key); await db.flush()
            db.add(ArchiveSnapshot(key_id=key.id, content_hash=f"body-{row.id}", body=json.dumps(response).encode()))
            row.archive_key_hash=key.key_hash; row.archive_content_hash=f"body-{row.id}"
            db.add(row)
        await db.commit()
        return row.id


async def test_database_stats_reclassify_deduplicate_and_exclude_errors(clients):
    await record("same", response={"data":{"email":"old@example.test"}}, ago=300, duration=100)
    await record("same", response={"data":{"email":None}}, ago=280)
    await record("same", status=429, error='{"error":"rate_limit"}', ago=260)
    await record("other", response={"data":{"email":"found@example.test"}}, duration=300)
    await record("no-result", status=404, error='{"error":"profile_not_found"}')
    await record("invalid", status=422, error='{"error":"params_invalid"}')
    await record("balance", status=402)
    await record("access", status=403)
    await record("no-body")
    await record("cached", response={"data":{"email":"cached@example.test"}}, cached=True)
    await record("own", response={"data":{"email":"own@example.test"}}, tier="credential")
    assert not await service.collect_batch(session_maker)
    snapshot=await service.public_snapshot(session_maker)
    assert snapshot["status"]=="ready"
    assert snapshot["since"] <= snapshot["observed_since"] <= snapshot["observed_until"] < snapshot["until"]
    row=next(r for r in snapshot["rows"] if r["endpoint"]==EP)
    assert row["hits"]==2 and row["misses"]==2
    assert row["unique_requests"]==3 and row["unique_rate"]==33.33
    assert row["median_hit_ms"]==200 and row["timed_hits"]==2
    assert row["excluded"]==4 and row["unresolved"]==1
    # Optional metric collector never trusts the audit hit flag or publishes evidence.
    assert "example.test" not in json.dumps(snapshot)
    assert "request_hash" not in json.dumps(snapshot)
    assert not await service.collect_batch(session_maker)
    assert (await service.public_snapshot(session_maker))["rows"]==snapshot["rows"]


async def test_initial_backlog_hidden_and_incremental_refresh(clients, monkeypatch):
    monkeypatch.setattr(service,"BATCH_SIZE",1)
    await record("first",response={"data":{"email":"found@example.test"}})
    assert await service.collect_batch(session_maker)
    assert (await service.public_snapshot(session_maker))["status"]=="warming"
    assert not await service.collect_batch(session_maker)
    initial=await service.public_snapshot(session_maker)
    assert initial["rows"][0]["unique_requests"]==1
    await record("later",response={"data":{"email":None}},ago=130)
    async with session_maker() as db:
        state=(await db.execute(select(ArenaInsightState))).scalar_one()
        state.updated_at=now()-timedelta(seconds=service.REFRESH_SECONDS+1)
        db.add(state);await db.commit()
    for _ in range(5):
        if not await service.collect_batch(session_maker):break
    assert (await service.public_snapshot(session_maker))["rows"][0]["unique_requests"]==2


async def test_catalog_rebuild_serves_previous_snapshot_until_new_one_is_complete(clients, monkeypatch):
    await record("first", response={"data": {"email": "found@example.test"}}, duration=100)
    await service.collect_batch(session_maker)
    initial = await service.public_snapshot(session_maker)
    cat, endpoints, _ = service._catalog()
    monkeypatch.setattr(service, "_catalog", lambda: (cat, endpoints, "next-catalog"))
    monkeypatch.setattr(service, "BATCH_SIZE", 1)

    # A fresh process can use the database snapshot before its worker starts.
    assert await service.public_snapshot(session_maker) == initial
    await record("second", response={"data": {"email": "other@example.test"}}, duration=300)
    for _ in range(2):
        assert await service.collect_batch(session_maker)
        assert await service.public_snapshot(session_maker) == initial

    assert not await service.collect_batch(session_maker)
    refreshed = await service.public_snapshot(session_maker)
    assert refreshed["status"] == "ready"
    assert refreshed["rows"][0]["unique_requests"] == 2
    assert refreshed["rows"][0]["median_hit_ms"] == 200
    async with session_maker() as db:
        state = await db.get(ArenaInsightState, "next-catalog")
        assert refreshed["updated_at"] == state.payload["updated_at"]


async def test_fallback_uses_latest_compatible_publication_even_during_refresh(clients):
    await record("first", response={"data": {"email": "found@example.test"}})
    await service.collect_batch(session_maker)
    initial = await service.public_snapshot(session_maker)
    _, _, version = service._catalog()
    async with session_maker() as db:
        current = await db.get(ArenaInsightState, version)
        original = dict(current.payload)
        current.payload = {}
        current.updated_at = None
        # Newest completed publication is retained even when its cursor is busy.
        db.add(ArenaInsightState(id="prior", scan_until=now(), payload=original))
        db.add(ArenaInsightState(id="older", scan_until=now(), updated_at=now(),
            payload={**original, "updated_at": "2000-01-01T00:00:00Z", "rows": []}))
        db.add(ArenaInsightState(id="incompatible", scan_until=now(), updated_at=now(),
            payload={**original, "version": 1, "updated_at": "2099-01-01T00:00:00Z"}))
        db.add(ArenaInsightState(id="unfinished", scan_until=now(),
            payload=service._empty(now())))
        await db.commit()
    assert await service.public_snapshot(session_maker) == initial

    # A completed current snapshot wins even if it legitimately has no rows.
    async with session_maker() as db:
        current = await db.get(ArenaInsightState, version)
        current.payload = {**original, "rows": []}
        await db.commit()
    assert (await service.public_snapshot(session_maker))["rows"] == []


async def test_missing_archive_is_revisited_and_old_window_removed(clients):
    ident=await record("late",ago=180)
    await record("expired",response={"data":{"email":"old@example.test"}},ago=31*86400)
    await service.collect_batch(session_maker)
    assert (await service.public_snapshot(session_maker))["rows"][0]["unique_requests"]==0
    async with session_maker() as db:
        row=await db.get(CallRecord,ident)
        key=ArchiveKey(key_hash="late-key",endpoint_id=EP,req_url="https://api.tomba.io/v1/email-finder?full_name=Example+Person&domain=example.test")
        db.add(key);await db.flush()
        db.add(ArchiveSnapshot(key_id=key.id,content_hash="late-body",body=b'{"data":{"email":"new@example.test"}}'))
        row.archive_key_hash=key.key_hash;row.archive_content_hash="late-body";db.add(row)
        state=(await db.execute(select(ArenaInsightState))).scalar_one();state.updated_at=now()-timedelta(seconds=121)
        db.add(state);await db.commit()
    await service.collect_batch(session_maker)
    row=(await service.public_snapshot(session_maker))["rows"][0]
    assert row["unique_requests"]==1 and row["unresolved"]==0


async def test_evidence_uses_exact_content_and_carrier_with_repeated_requests(clients):
    answer = {"data": {"email": "found@example.test"}}
    first = await record("first", response=answer)
    repeated = await record("repeated")
    missing = await record("missing")
    no_content = await record("no-content", status=404)
    async with session_maker() as db:
        original = await db.get(CallRecord, first)
        key = (await db.execute(select(ArchiveKey).where(
            ArchiveKey.key_hash == original.archive_key_hash))).scalar_one()
        carrier = (await db.execute(select(ArchiveSnapshot).where(
            ArchiveSnapshot.key_id == key.id))).scalar_one()
        db.add(ArchiveSnapshot(key_id=key.id, version=2, content_hash=carrier.content_hash,
            body_of=carrier.id, body=None))
        db.add(ArchiveSnapshot(key_id=key.id, version=3, content_hash="different-answer",
            body=b'{"data":{"email":null}}'))
        for ident, content_hash in ((repeated, carrier.content_hash), (missing, "absent"), (no_content, None)):
            row = await db.get(CallRecord, ident)
            row.archive_key_hash = key.key_hash
            row.archive_content_hash = content_hash
            db.add(row)
        await db.commit()
        records = (await db.execute(select(CallRecord).where(
            CallRecord.id.in_([first, repeated, missing, no_content])))).scalars().all()
        evidence = await service._evidence(db, records)
        assert evidence[first][2] == evidence[repeated][2] == answer
        assert evidence[missing][2] is None
        assert evidence[no_content][0]["domain"] == "example.test"
        assert evidence[no_content][2] is None
        # A batch containing request metadata but no response references is still valid.
        metadata_only = await service._evidence(db, [r for r in records if r.id == no_content])
        assert metadata_only == {no_content: evidence[no_content]}


@pytest.mark.parametrize("status",["valid","invalid"])
def test_verification_verdict_is_an_answer(status):
    cat=store.load();ep=cat.by_id["tomba.people.email.verify"]
    row=dict(kind="call",cached=False,credential_tier="platform",refused_by=None,error_request=None,
        error_response=None,status_code=200,path="/v1/email-verifier",method="GET",endpoint_id=ep["id"],params_hash="synthetic")
    assert rules.classify_record(row,ep,cat.adapters[ep["id"]],cat.contracts[ep["capability"]],
        ({"email":"person@example.test"},{},{"data":{"email":{"status":status,"score":99}}}))==("email","hit")


async def test_malformed_evidence_cannot_block_the_collection_cursor(clients, monkeypatch):
    await record("broken", response={"data":{"email":"found@example.test"}})
    classify = rules.classify_record
    monkeypatch.setattr(rules, "classify_record", lambda *args: (_ for _ in ()).throw(ValueError("malformed evidence")))
    assert not await service.collect_batch(session_maker)
    assert (await service.public_snapshot(session_maker))["status"] == "ready"
    async with session_maker() as db:
        fact = (await db.execute(select(ArenaObservation))).scalar_one()
        assert fact.category == "unresolved_evidence"
    monkeypatch.setattr(rules, "classify_record", classify)


def test_explicit_unknown_body_error_is_not_a_coverage_miss():
    assert rules.body_failure({"status":"error", "message":"unexpected provider failure"}) == "unresolved_body_error"
    assert rules.body_failure({"error":"unrecognized failure"}) == "unresolved_body_error"
