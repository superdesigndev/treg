"""Synthetic database evidence only; no production metrics or network calls."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from treg import archive_bodies
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


async def test_naive_datetime_insert_does_not_raise_value_error(clients):
    """Regression test: SQLModel 0.0.45 rejects naive datetime binds on INSERT.

    The arena insights collector INSERTs into ArenaInsightState with scan_until=now()-60s.
    Before the NaiveUTC annotation fix and sqlmodel pin, this raised:
        ValueError: Datetime values must have timezone information.
    """
    backlog = await service.collect_batch(session_maker)
    assert backlog is False  # no records, but INSERT succeeded


async def test_evidence_pointers_name_their_reader_and_carry_the_media_type(clients, monkeypatch):
    call_id = await record("media", response={"data": {"email": "found@example.test"}})
    async with session_maker() as db:
        snapshot = (await db.execute(select(ArchiveSnapshot))).scalars().one()
        snapshot.media_type = "application/json"
        db.add(snapshot)
        await db.commit()
        row = await db.get(CallRecord, call_id)
    readers = []
    options = archive_bodies.read_options
    monkeypatch.setattr(archive_bodies, "read_options", lambda path: readers.append(path) or options(path))
    async with session_maker() as db:
        keymap, pointers, media = await service._evidence_pointers(db, [row], path="replay")
    pair = (keymap[row.archive_key_hash].id, row.archive_content_hash)
    assert set(pointers) == {pair} and media == {pair: "application/json"}
    assert readers == ["replay"]


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


async def test_rules_change_serves_previous_snapshot_until_new_one_is_complete(clients, monkeypatch):
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
        state=(await db.execute(select(ArenaInsightState))).scalar_one();state.updated_at=now()-timedelta(seconds=service.REFRESH_SECONDS+1)
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
    evidence = await service._evidence(records)
    assert evidence[first][2] == evidence[repeated][2] == answer
    assert evidence[missing][2] is None
    assert evidence[no_content][0]["domain"] == "example.test"
    assert evidence[no_content][2] is None
    # A batch containing request metadata but no response references is still valid.
    metadata_only = await service._evidence([r for r in records if r.id == no_content])
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


@pytest.mark.parametrize('read_mode', ['db', 'r2-first'])
async def test_corrupt_compressed_body_does_not_block_other_arena_evidence(clients, monkeypatch, read_mode):
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), 'archive_body_read_result', read_mode)
    await record('broken', response={'data': {'email': 'broken@example.test'}})
    async with session_maker() as db:
        snap = (await db.execute(select(ArchiveSnapshot))).scalar_one()
        snap.body, snap.enc = b'not a zlib stream', 'zlib'
        db.add(snap)
        await db.commit()
    await record('healthy', response={'data': {'email': 'found@example.test'}})
    assert not await service.collect_batch(session_maker)
    snapshot = await service.public_snapshot(session_maker)
    assert snapshot['status'] == 'ready'
    assert snapshot['rows'][0]['hits'] == 1 and snapshot['rows'][0]['unresolved'] == 1


def test_explicit_unknown_body_error_is_not_a_coverage_miss():
    assert rules.body_failure({"status":"error", "message":"unexpected provider failure"}) == "unresolved_body_error"
    assert rules.body_failure({"error":"unrecognized failure"}) == "unresolved_body_error"


async def test_bounceban_finished_answers_count_and_verifying_is_unresolved(clients):
    """BounceBan's row is async only for the rare `verifying` answer (`terminal_on_submission`), so
    it stays on the leaderboard: a finished first answer is judged as usual, a `verifying` one is no
    answer yet."""
    bb = "bounceban.people.email.verify"
    assert bb in service._catalog()[1]

    async def bounceban(hash_, response):
        async with session_maker() as db:
            row = CallRecord(user_email="synthetic@example.test", tool_name=bb, method="GET",
                path="/v1/verify/single", status_code=200, endpoint_id=bb, provider="bounceban",
                credential_tier="platform", params_hash=hash_, error_request="?email=dev%40bounceban.com",
                cached=False, duration_ms=100, created_at=now() - timedelta(seconds=180))
            db.add(row); await db.flush()
            key = ArchiveKey(key_hash=f"synthetic-{row.id}", endpoint_id=bb,
                req_url="https://api.bounceban.com/v1/verify/single?email=dev%40bounceban.com")
            db.add(key); await db.flush()
            db.add(ArchiveSnapshot(key_id=key.id, content_hash=f"body-{row.id}", body=json.dumps(response).encode()))
            row.archive_key_hash = key.key_hash; row.archive_content_hash = f"body-{row.id}"
            db.add(row); await db.commit()

    await bounceban("found", {"id": "a", "status": "success", "result": "deliverable"})
    await bounceban("no-verdict", {"id": "b", "status": "success", "result": None})
    await bounceban("verifying", {"id": "c", "status": "verifying", "try_again_at": 1})
    assert not await service.collect_batch(session_maker)
    snapshot = await service.public_snapshot(session_maker)
    row = next(r for r in snapshot["rows"] if r["endpoint"] == bb)
    assert row["hits"] == 1 and row["misses"] == 1 and row["unresolved"] == 1
    async with session_maker() as db:
        categories = (await db.execute(select(ArenaObservation.category).where(
            ArenaObservation.endpoint == bb))).scalars().all()
    assert sorted(categories) == ["hit", "miss", "unresolved_pending"]


def test_async_rows_without_terminal_on_submission_stay_out_of_insights():
    endpoints = service._catalog()[1]
    assert "wiza.people.email.find" not in endpoints


def test_cursor_version_survives_catalog_changes(monkeypatch):
    cat, endpoints, version = service._catalog()
    monkeypatch.setattr(store, "load", lambda: cat)
    monkeypatch.setitem(cat.by_id, EP, {**endpoints[EP], "path": "/moved"})
    monkeypatch.delitem(cat.by_id, next(eid for eid in endpoints if eid != EP))
    assert service._catalog()[2] == version
    monkeypatch.setattr(rules, "RULES_VERSION", rules.RULES_VERSION + "-next")
    assert service._catalog()[2] != version


async def test_aggregate_counts_only_the_current_endpoint_set(clients, monkeypatch):
    await record("first", response={"data": {"email": "found@example.test"}})
    cat, endpoints, version = service._catalog()
    async with session_maker() as db:
        db.add(ArenaObservation(id=10**9, version=version, endpoint="gone.people.email.find", task="people.email.find",
            input="name_domain", request_hash="h", category="hit", created_at=now() - timedelta(minutes=5)))
        await db.commit()
    assert not await service.collect_batch(session_maker)
    rows = (await service.public_snapshot(session_maker))["rows"]
    assert [r["endpoint"] for r in rows] == [EP]


async def test_prune_runs_during_a_backlog_and_clears_superseded_versions(clients, monkeypatch):
    monkeypatch.setattr(service, "BATCH_SIZE", 1)
    await record("first", response={"data": {"email": "found@example.test"}})
    await record("second", response={"data": {"email": "other@example.test"}})
    version = service._catalog()[2]
    old = dict(task="people.email.find", input="name_domain", request_hash="h", category="hit")
    async with session_maker() as db:
        db.add(ArenaObservation(id=10**9, version=version, endpoint=EP, created_at=now() - timedelta(days=40), **old))
        db.add(ArenaObservation(id=10**9 + 1, version="superseded", endpoint=EP, created_at=now(), **old))
        db.add(ArenaInsightState(id="superseded", scan_until=now(), updated_at=now(),
            payload={**service._empty(now()), "status": "ready", "updated_at": "2000-01-01T00:00:00Z"}))
        await db.commit()

    assert (await service.drain(session_maker, max_seconds=0))["backlog"]
    async with session_maker() as db:
        ids = set((await db.execute(select(ArenaObservation.id))).scalars())
        assert not ids & {10**9, 10**9 + 1}
        # Still the fallback while the current version has not published.
        assert await db.get(ArenaInsightState, "superseded")

    assert not (await service.drain(session_maker))["backlog"]
    await service.prune(session_maker)
    async with session_maker() as db:
        assert await db.get(ArenaInsightState, "superseded") is None
    assert (await service.public_snapshot(session_maker))["rows"][0]["unique_requests"] == 2


async def test_rollback_after_prune_rebuilds_before_publishing(clients, monkeypatch):
    await record("first", response={"data": {"email": "found@example.test"}})
    cat, endpoints, version = service._catalog()
    monkeypatch.setattr(service, "_catalog", lambda: (cat, endpoints, "rolled-back"))
    assert not await service.collect_batch(session_maker)
    published = await service.public_snapshot(session_maker)
    assert published["rows"][0]["unique_requests"] == 1

    monkeypatch.setattr(service, "_catalog", lambda: (cat, endpoints, version))
    await service.prune(session_maker)
    async with session_maker() as db:
        old = await db.get(ArenaInsightState, "rolled-back")
        assert old.cursor == 0 and old.updated_at is None and old.payload["rows"] == published["rows"]

    monkeypatch.setattr(service, "_catalog", lambda: (cat, endpoints, "rolled-back"))
    assert not await service.collect_batch(session_maker)
    assert (await service.public_snapshot(session_maker))["rows"][0]["unique_requests"] == 1


def test_the_rewind_asks_for_an_expression_postgresql_cannot_answer_by_walking_the_primary_key():
    """A bare `min(id)` becomes an ascending primary-key walk from the oldest audit row, which
    filters the whole table before it reaches the last ten minutes; `min(id + 0)` reads the
    endpoint/created_at index instead."""
    from sqlalchemy.dialects import postgresql

    sql = str(service.rewind_query(["a.b"], now()).compile(dialect=postgresql.dialect()))
    assert "min(callrecord.id + 0)" in sql
    assert "callrecord.endpoint_id IN" in sql and "callrecord.created_at >=" in sql


def test_the_batch_read_asks_each_arena_endpoint_on_its_own_index():
    """`endpoint_id IN (...) AND id > cursor ORDER BY id LIMIT n` is planned as a primary-key
    walk from the cursor that reads every endpoint's rows; one ordered probe per endpoint reads
    `ix_callrecord_endpoint_id_id` entries of the Arena's rows only."""
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    window = (CallRecord.created_at >= now(), CallRecord.created_at < now())
    ids = str(service.batch_ids_query(["a.b", "c.d"], 7, 99, window).compile(dialect=dialect))
    assert "JOIN LATERAL" in ids and "VALUES" in ids and " IN " not in ids
    assert "callrecord.endpoint_id = arena_endpoint.endpoint_id" in ids
    assert "callrecord.id <=" in ids and "callrecord.created_at >=" in ids


async def _bulk(rows):
    """(endpoint_id, created_at) pairs, inserted in order, so ids follow the list."""
    async with session_maker() as db:
        for endpoint_id, created_at in rows:
            db.add(CallRecord(user_email="synthetic@example.test", tool_name=endpoint_id, method="GET",
                path="/v1/x", status_code=200, endpoint_id=endpoint_id, provider="tomba",
                credential_tier="platform", params_hash="h", created_at=created_at))
        await db.commit()


async def _observed() -> int:
    async with session_maker() as db:
        return len((await db.execute(select(ArenaObservation.id))).all())


async def test_rows_older_than_the_window_do_not_take_a_batchs_places(clients):
    """A cold start over a long history: only rows inside the window count toward a batch, so
    one row in it after a thousand older ones is published in the first batch."""
    old = now() - timedelta(days=service.WINDOW_DAYS + 1)
    await _bulk([(EP, old)] * 1000 + [(EP, now() - timedelta(seconds=180))])
    assert not await service.collect_batch(session_maker), "published in one batch"
    assert await _observed() == 1


async def test_a_row_written_before_the_cutoff_is_read_whatever_its_id(clients):
    """Ids follow insertion, not `created_at`: a row of the cycle can have a larger id than one
    written after the cutoff. Both are committed before the batch, so the cycle counts it."""
    # collect_batch cuts the cycle off 60 s before now.
    await _bulk([(EP, now() - timedelta(seconds=58)), (EP, now() - timedelta(seconds=62))])
    assert not await service.collect_batch(session_maker)
    assert await _observed() == 1


async def test_a_cycle_cut_off_long_ago_publishes_without_reading_past_it(clients):
    """The cycle's cutoff fell behind while a busy endpoint kept writing. The one row of the
    cycle is published in one batch, not after the rows written since."""
    _, _, version = service._catalog()
    until = now() - timedelta(hours=1)
    async with session_maker() as db:
        db.add(ArenaInsightState(id=version, cursor=0, scan_until=until, updated_at=None, payload={}))
        await db.commit()
    other = "hunter.people.email.find"
    await _bulk([(EP, until - timedelta(seconds=30))]
                + [(other, until + timedelta(seconds=5 + index)) for index in range(1000)])
    assert not await service.collect_batch(session_maker)
    assert await _observed() == 1


async def test_an_arena_row_committed_after_a_later_id_is_read_by_the_next_cycle(clients):
    """An Arena row takes its id, and a row of another endpoint takes the next id and commits
    first. The cycle sees only the second, finds its span empty of the window and publishes;
    the first commits afterwards. The next cycle must still read it."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("needs two concurrent write transactions")
    async with session_maker() as arena_write:
        arena_write.add(CallRecord(user_email="synthetic@example.test", tool_name=EP, method="GET",
            path="/v1/x", status_code=200, endpoint_id=EP, provider="tomba", credential_tier="platform",
            params_hash="late", created_at=now() - timedelta(seconds=180)))
        await arena_write.flush()
        await _bulk([("not.an.arena.endpoint", now() - timedelta(seconds=170))])
        assert not await service.collect_batch(session_maker)
        assert await _observed() == 0
        await arena_write.commit()
    async with session_maker() as db:
        state = (await db.execute(select(ArenaInsightState))).scalar_one()
        state.updated_at = now() - timedelta(seconds=service.REFRESH_SECONDS + 1)
        await db.commit()
    assert not await service.collect_batch(session_maker)
    assert await _observed() == 1
