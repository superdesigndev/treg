"""R2 double writes and reads through /call and /calls, with no real object network."""
import asyncio
from collections import Counter
import hashlib

import pytest
from sqlalchemy import event, select

from treg import archive, archive_bodies, audit, bootstrap
from treg.application.call import service
from treg.config import get_settings
from treg.infra import db
from treg.models import ArchiveKey, ArchiveSnapshot
from tests.fake_object_store import MemoryObjectStore
from tests.test_marketplace_call import _fake_relay

EP = 'tikhub.tiktok.video.comments'
RAW = b'{"data":{"comments":[{"text":"hello"}]}}'
URL = '/call/' + EP + '?aweme_id=7'


@pytest.fixture
async def r2(monkeypatch):
    monkeypatch.setenv('TREG_PLATFORM_KEY_TIKHUB', 'test-platform-key')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'tikhub')
    get_settings.cache_clear()
    s = get_settings()
    for name, value in {
        'archive_mode': 'serve', 'archive_body_write': 'both',
        'archive_serve_endpoints': EP, 'archive_serve_percent': 100,
        'archive_object_store_endpoint': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com', 'archive_object_store_bucket': 'treg-dev',
        'archive_object_store_access_key_id': 'test-access', 'archive_object_store_secret_access_key': 'test-secret',
    }.items():
        monkeypatch.setattr(s, name, value)
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    # Enforce the invariant at the actual I/O seam, including request dependency sessions and
    # the background pool. Other requests may use DB concurrently; this task must own none.
    held = Counter()
    def checkout(connection, record, proxy):
        owner = asyncio.current_task()
        record.info['r2_test_owner'] = owner
        held[owner] += 1
    def checkin(connection, record):
        held[record.info.pop('r2_test_owner', None)] -= 1
    engines = {engine.sync_engine for engine in db._engines}
    for engine in engines:
        event.listen(engine, 'checkout', checkout)
        event.listen(engine, 'checkin', checkin)
    def check_io():
        assert held[asyncio.current_task()] == 0, 'object I/O holds a DB connection'
    store.check_io = check_io
    monkeypatch.setattr(service, 'relay', _fake_relay(200, RAW))
    yield store
    await archive.drain()
    bootstrap.configure_archive_object_store(None)
    for engine in engines:
        event.remove(engine, 'checkout', checkout)
        event.remove(engine, 'checkin', checkin)
    get_settings.cache_clear()


async def snapshots():
    async with db.session_maker() as s:
        return (await s.execute(select(ArchiveSnapshot).order_by(ArchiveSnapshot.id))).scalars().all()


async def test_upload_precedes_pointer_and_call_does_not_wait(clients, r2, monkeypatch):
    # Warm catalog/auth initialization before measuring only the non-blocking archive behavior.
    with monkeypatch.context() as warm:
        warm.setattr(get_settings(), 'archive_mode', 'off')
        assert (await clients.get(URL)).status_code == 200
    events = []
    monkeypatch.setattr(service.analytics, 'capture', lambda who, name, props, **kw: events.append((name, props)))
    r2.gate = asyncio.Event()
    response = await asyncio.wait_for(clients.get(URL), 2)
    assert response.status_code == 200 and response.content == RAW
    await asyncio.wait_for(r2.entered.wait(), 2)
    assert await snapshots() == []
    assert len([p for name, p in events if name == 'tool_called']) == 1
    assert not [p for name, p in events if name == 'archive_body_stored']
    r2.gate.set()
    await archive.drain()
    rows = await snapshots()
    assert len(rows) == 1 and rows[0].body_storage == 'both'
    assert r2.objects[rows[0].content_hash] == RAW
    assert archive._unpack(rows[0].body, rows[0].enc) == RAW
    props = [p for name, p in events if name == 'archive_body_stored']
    assert len(props) == 1 and props[0]['upload_status'] == 'uploaded'
    assert props[0]['upload_ms'] >= 0
    assert props[0]['dropped'] is False


@pytest.mark.parametrize('mode,expected_rows', [('both', 1), ('r2', 1)])
async def test_upload_failure_never_publishes_r2_pointer(clients, r2, monkeypatch, mode, expected_rows):
    monkeypatch.setattr(get_settings(), 'archive_body_write', mode)
    r2.fail_puts = 100
    events = []
    monkeypatch.setattr(service.analytics, 'capture', lambda who, name, props, **kw: events.append((name, props)))
    response = await clients.get(URL)
    assert response.content == RAW and response.status_code == 200
    await archive.drain()
    rows = await snapshots()
    assert len(rows) == expected_rows and all(row.body_storage == ('db' if mode == 'both' else None) for row in rows)
    props = [p for name, p in events if name == 'archive_body_stored']
    assert len(props) == 1
    assert props[0]['drop_reason'] == 'store_error'
    assert props[0]['upload_status'] == 'failed'
    assert props[0]['dropped'] is (mode == 'r2')


async def test_r2_queue_has_independent_concurrency_and_sheds_observably(clients, r2, monkeypatch):
    monkeypatch.setattr(get_settings(), 'archive_r2_max_pending', 3)
    r2.gate = asyncio.Event()
    events = []
    dropped = asyncio.Event()
    def capture(who, name, props, **kw):
        events.append((name, props))
        if props.get('drop_reason') == 'upload_queue_full':
            dropped.set()
    monkeypatch.setattr(service.analytics, 'capture', capture)
    try:
        for i in range(4):
            await clients.get(URL + '&count=' + str(i))
        # Wait for the DB fallback's completion event, not an arbitrary scheduling delay.
        await asyncio.wait_for(dropped.wait(), 5)
        assert r2.put_calls == 3  # more uploads than the two DB write slots
        assert all(row.body_storage == 'db' for row in await snapshots())
    finally:
        r2.gate.set()
    await archive.drain()
    assert len(await snapshots()) == 4 and len(r2.objects) == 1
    assert len([p for name, p in events if name == 'tool_called']) == 4


@pytest.mark.parametrize('path', ['lookup', 'result', 'terminal'])
@pytest.mark.parametrize('fallback', [False, 'error', 'missing', 'corrupt'])
async def test_read_switches_and_fallback_without_db_connection(clients, r2, monkeypatch, path, fallback):
    monkeypatch.setattr(get_settings(), 'archive_body_read_' + path, 'r2-first')
    if path == 'terminal':
        await archive.store_terminal_response('terminal-test', 'tikhub', EP, 200, RAW)
    else:
        await clients.get(URL)
        await archive.drain()
        await audit.drain()
    r2.fail_gets = fallback == 'error'
    if fallback == 'missing':
        r2.objects.clear()
    elif fallback == 'corrupt':
        r2.objects = {key: b'corrupt' for key in r2.objects}
    if not fallback:
        # Remove only the DB test copy, proving this path really obtains bytes from R2.
        async with db.session_maker() as s:
            for row in (await s.execute(select(ArchiveSnapshot))).scalars():
                row.body = None
                s.add(row)
            await s.commit()
    if path == 'lookup':
        response = await clients.get(URL)
        assert response.headers.get('x-treg-cache') == 'hit' and response.content == RAW
    elif path == 'result':
        call = (await clients.get('/calls')).json()[0]
        response = await clients.get(f"/calls/{call['id']}/result")
        assert response.status_code == 200
        assert response.json()['response']['body_text'] == RAW.decode()
    else:
        assert await archive.load_terminal_responses([('terminal-test', EP)]) == {'terminal-test': RAW}
    assert r2.get_calls > 0


async def test_defaults_read_db_and_r2_only_has_no_db_carrier(clients, r2, monkeypatch):
    await clients.get(URL)
    await archive.drain()
    await clients.get(URL)
    assert r2.get_calls == 0
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'r2')
    for _ in range(2):
        await clients.get(URL, headers={'Cache-Control': 'no-cache'})
        await archive.drain()
    rows = await snapshots()
    assert all(row.body is None and row.body_of is None and row.body_storage == 'r2' for row in rows[1:])
    monkeypatch.setattr(get_settings(), 'archive_body_read_lookup', 'r2-first')
    assert (await clients.get(URL)).content == RAW


async def test_terminal_retries_synchronously_bypassing_full_queue(clients, r2, monkeypatch):
    monkeypatch.setattr(get_settings(), 'archive_r2_max_pending_bytes', 1)
    r2.fail_puts = 2
    await archive.store_terminal_response('terminal-test', 'tikhub', EP, 200, RAW)
    assert r2.put_calls == 3
    assert (await snapshots())[0].body_storage == 'both'
    assert not archive_bodies._pending


async def test_pruner_strips_both_db_bytes_but_preserves_r2(clients, r2, monkeypatch):
    from datetime import timedelta
    for i in range(4):
        monkeypatch.setattr(service, 'relay', _fake_relay(200, RAW + b' ' * i))
        await clients.get(URL, headers={'Cache-Control': 'no-cache'})
        await archive.drain()
    async with db.session_maker() as s:
        for row in (await s.execute(select(ArchiveSnapshot))).scalars():
            row.fetched_at -= timedelta(days=40)
            s.add(row)
        key = (await s.execute(select(ArchiveKey))).scalar_one()
        key.last_requested_at -= timedelta(days=40)
        key.ttl_s = archive.TTL_NEVER
        s.add(key)
        await s.commit()
    assert await archive.prune_once() == 3
    rows = await snapshots()
    assert all(row.body is None and row.body_storage == "r2" for row in rows[:-1])
    assert rows[-1].body is not None and rows[-1].body_storage == "both"
    for row in rows:
        assert await r2.get(row.content_hash) is not None


@pytest.mark.parametrize('setting', ['archive_body_write', 'archive_body_read_lookup',
                                    'archive_body_read_result', 'archive_body_read_terminal'])
async def test_startup_fails_before_db_when_r2_configuration_missing(monkeypatch, setting):
    from fastapi import FastAPI
    s = get_settings()
    monkeypatch.setattr(s, 'archive_mode', 'serve')
    monkeypatch.setattr(s, setting, 'both' if setting == 'archive_body_write' else 'r2-first')
    monkeypatch.setattr(s, 'archive_object_store_endpoint', '')
    async def no_db():
        pytest.fail('startup should reject missing R2 configuration before DB I/O')
    monkeypatch.setattr(bootstrap, 'verify_db', no_db)
    with pytest.raises(RuntimeError, match='Archive R2'):
        async with bootstrap._lifespan('control')(FastAPI()):
            pass


def test_db_defaults_need_no_r2_configuration():
    from treg.config import Settings
    s = Settings(_env_file=None)
    assert s.archive_body_write == 'db'
    assert all(getattr(s, 'archive_body_read_' + p) == 'db' for p in ('lookup', 'result', 'terminal'))


async def test_checksum_mismatch_is_not_published(clients, r2, monkeypatch):
    from treg.infra.object_store import ObjectInfo
    async def wrong(body, **kw):
        return ObjectInfo('0' * 64, len(body))
    monkeypatch.setattr(r2, 'put', wrong)
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'r2')
    await clients.get(URL)
    await archive.drain()
    assert (await snapshots())[0].body_storage is None


@pytest.mark.parametrize('path', ['/calls', '/calls/{ref}'])
async def test_activity_terminal_reads_release_request_session(clients, r2, monkeypatch, path):
    from treg.models import AsyncTaskRecord, CallRecord
    from treg.timeutil import utcnow_naive
    monkeypatch.setattr(get_settings(), 'archive_body_read_terminal', 'r2-first')
    await clients.get(URL)
    await archive.drain()
    await audit.drain()
    async with db.session_maker() as s:
        call = (await s.execute(select(CallRecord))).scalar_one()
        ref = call.call_ref
        s.add(AsyncTaskRecord(call_id=ref, org_id=call.org_id, endpoint_id=EP, provider='tikhub',
                              reserved_micro=0, settled_micro=0, status='settled',
                              next_check_at=utcnow_naive(), completed_at=utcnow_naive()))
        await s.commit()
    await archive.store_terminal_response(ref, 'tikhub', EP, 200, RAW)
    response = await clients.get(path.format(ref=ref))
    assert response.status_code == 200
    assert r2.get_calls > 0


async def test_upload_timeout_and_byte_shedding_are_observable(clients, r2, monkeypatch):
    events = []
    monkeypatch.setattr(service.analytics, 'capture', lambda who, name, props, **kw: events.append((name, props)))
    monkeypatch.setattr(get_settings(), 'archive_r2_timeout_s', 0.02)
    r2.gate = asyncio.Event()
    await clients.get(URL)
    await archive.drain()
    assert any(p.get('drop_reason') == 'timeout' for _, p in events)
    monkeypatch.setattr(get_settings(), 'archive_r2_max_pending_bytes', 1)
    await clients.get(URL, headers={'Cache-Control': 'no-cache'})
    await archive.drain()
    assert any(p.get('drop_reason') == 'upload_bytes_full' for _, p in events)


async def test_obstore_client_uses_one_request_and_checks_hash_and_size():
    from treg.infra.object_store import R2ObjectStore, ObjectInfo
    from tests.fake_object_store import MemoryObstoreSDK
    sdk = MemoryObstoreSDK()
    store = R2ObjectStore(sdk, 1000)
    digest = hashlib.sha256(RAW).hexdigest()
    assert await store.put(RAW) == ObjectInfo(digest, len(RAW))
    assert sdk.path == digest and 'sha256' not in sdk.attributes
    assert sdk.calls == ['put']
    assert await store.head(digest) == ObjectInfo(digest, len(RAW))
    assert sdk.calls == ['put', 'head']
    assert await store.get(digest) == RAW
    assert sdk.calls == ['put', 'head', 'get']
    small = R2ObjectStore(sdk, len(RAW) - 1)
    with pytest.raises(ValueError, match='too large'):
        await small.put(RAW)
    with pytest.raises(ValueError, match='too_large'):
        await small.get(digest)
    with pytest.raises(ValueError, match='content hash'):
        await store.get('../caller-controlled')
    sdk.body = b'corrupt'
    with pytest.raises(ValueError, match='hash_mismatch'):
        await store.get(digest)


async def test_dev_smoke_skips_missing_credentials(monkeypatch, capsys):
    import runpy
    for name in ('ENDPOINT', 'BUCKET', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY'):
        monkeypatch.setenv('TREG_ARCHIVE_OBJECT_STORE_' + name, '')
    smoke = runpy.run_path('scripts/smoke_archive_r2.py')
    await smoke['run']()
    output = capsys.readouterr().out
    assert output.startswith('SKIP:')
    assert 'TREG_ARCHIVE_OBJECT_STORE_ACCESS_KEY_ID' in output


async def test_dev_smoke_refuses_production_bucket(monkeypatch):
    import runpy
    for name, value in {'ENDPOINT': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
                        'BUCKET': 'treg-archive', 'ACCESS_KEY_ID': 'fake',
                        'SECRET_ACCESS_KEY': 'fake'}.items():
        monkeypatch.setenv('TREG_ARCHIVE_OBJECT_STORE_' + name, value)
    smoke = runpy.run_path('scripts/smoke_archive_r2.py')
    with pytest.raises(SystemExit, match='Refusing'):
        await smoke['run']()


async def test_pruner_keeps_legacy_carrier_referenced_by_both_row(clients, r2, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'db')
    await clients.get(URL)
    await archive.drain()
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'both')
    await clients.get(URL, headers={'Cache-Control': 'no-cache'})
    await archive.drain()
    async with db.session_maker() as s:
        rows = (await s.execute(select(ArchiveSnapshot).order_by(ArchiveSnapshot.version))).scalars().all()
        assert rows[1].body_of == rows[0].id and rows[1].body_storage == 'both'
        for row in rows:
            row.fetched_at -= timedelta(days=40)
            s.add(row)
        key = (await s.execute(select(ArchiveKey))).scalar_one()
        key.ttl_s = archive.TTL_NEVER
        key.last_requested_at -= timedelta(days=40)
        s.add(key)
        await s.commit()
    assert await archive.prune_once() == 0
    assert (await snapshots())[0].body is not None


@pytest.mark.parametrize('write_mode', ['both', 'r2'])
async def test_result_admission_retains_decisive_r2_snapshot(clients, r2, monkeypatch, write_mode):
    from tests.test_cache_result_admission import FOUND, EMPTY, _call, _key
    s = get_settings()
    monkeypatch.setattr(s, 'platform_providers', 'hunter')
    monkeypatch.setattr(s, 'platform_key_hunter', 'test-platform-key')
    monkeypatch.setattr(s, 'archive_serve_endpoints', 'hunter.companies.emails')
    monkeypatch.setattr(s, 'archive_body_write', write_mode)
    monkeypatch.setattr(s, 'archive_body_read_lookup', 'r2-first')
    monkeypatch.setattr(s, 'archive_body_read_result', 'r2-first')
    await _call(clients, monkeypatch, FOUND)
    first = await _key()
    await _call(clients, monkeypatch, b'{}', live=True)
    uncertain = await _key()
    assert uncertain.result_snapshot_id == first.result_snapshot_id
    assert uncertain.result_observed_version == 2
    assert uncertain.result_state == 'found' and uncertain.ttl_s == first.ttl_s
    cached = await _call(clients, monkeypatch, EMPTY)
    assert cached.headers['x-treg-cache'] == 'hit' and cached.content == FOUND
    await _call(clients, monkeypatch, FOUND, live=True)
    assert (await _key()).stable_seen == 1  # compare hashes across an uncertain version
    await _call(clients, monkeypatch, EMPTY, live=True)
    empty = await _key()
    assert empty.result_state == 'empty' and empty.change_seen == 1
    miss = await _call(clients, monkeypatch, EMPTY)
    assert 'x-treg-cache' not in miss.headers and (await _key()).ttl_s == empty.ttl_s
    history = await archive.resolve_result(first.key_hash, archive.content_hash(FOUND))
    assert history['response']['body_text'] == FOUND.decode()
    await _call(clients, monkeypatch, FOUND)
    recovered = await _key()
    assert (recovered.result_state, recovered.stable_seen, recovered.change_seen) == ('found', 1, 2)
    if write_mode == 'r2':
        assert all(row.body_of is None for row in await snapshots())


@pytest.mark.parametrize('write_timeout,read_timeout', [(10.0, 2.0), (0.01, 2.0), (30.0, 0.01)])
async def test_obstore_factory_configuration_and_missing_objects(monkeypatch, write_timeout, read_timeout):
    from treg.infra.object_store import open_r2
    from tests.fake_object_store import MemoryObstoreSDK
    from obstore.store import S3Store
    import obstore.store
    from treg.config import Settings
    captured = {}
    class Missing(MemoryObstoreSDK):
        async def head_async(self, path):
            raise FileNotFoundError('absent')
        async def get_async(self, path, **kwargs):
            raise FileNotFoundError('absent')
    def factory(bucket, **kwargs):
        captured.update(kwargs)
        # Parse config with the real wheel, but perform no network I/O.
        S3Store(bucket, **kwargs)
        return Missing()
    monkeypatch.setattr(obstore.store, 'S3Store', factory)
    settings = Settings(_env_file=None, archive_object_store_bucket='treg-dev',
                        archive_object_store_endpoint='https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
                        archive_object_store_access_key_id='fake', archive_object_store_secret_access_key='fake')
    settings.archive_r2_timeout_s = write_timeout
    settings.archive_r2_read_timeout_s = read_timeout
    async with open_r2(settings) as store:
        assert await store.head('0' * 64) is None
        assert await store.get('0' * 64) is None
    assert captured['config']['checksum_algorithm'] == 'SHA256'
    assert captured['config']['region'] == 'auto'
    assert captured['retry_config'] == {'max_retries': 0}
    transport_timeout = f'{max(write_timeout, read_timeout)}s'
    assert captured['client_options'] == {'timeout': transport_timeout, 'connect_timeout': transport_timeout}


@pytest.mark.parametrize('path', ['lookup', 'result', 'terminal'])
@pytest.mark.parametrize('reason,level', [('not_found', 'WARNING'), ('timeout', 'WARNING'),
                                         ('permission_denied', 'ERROR'), ('hash_mismatch', 'ERROR')])
async def test_read_fallback_reason_level_and_diagnostics(r2, monkeypatch, caplog, path, reason, level):
    from treg.infra.object_store import ObjectStoreError
    monkeypatch.setattr(get_settings(), 'archive_body_read_' + path, 'r2-first')
    monkeypatch.setattr(get_settings(), 'archive_r2_read_timeout_s', 0.01)
    monkeypatch.setattr(get_settings(), 'archive_r2_timeout_s', 30.0)
    async def fail(key):
        if reason == 'timeout':
            await asyncio.sleep(1)
        elif reason == 'not_found':
            return None
        else:
            raise ObjectStoreError(reason)
    monkeypatch.setattr(r2, 'get', fail)
    p = archive_bodies.BodyPointer(archive.content_hash(RAW), 'both', RAW, None)
    diagnostics = {}
    before = archive_bodies.outcomes['read_fallback_' + path]
    assert await archive_bodies.read(p, path, diagnostics=diagnostics) == RAW
    assert diagnostics['cache_body_source'] == 'db'
    assert diagnostics['cache_body_fallback_reason'] == reason
    assert 0 <= diagnostics['cache_r2_read_ms'] < 500
    assert archive_bodies.outcomes['read_fallback_' + path] == before + 1
    record = [r for r in caplog.records if 'archive R2 read fallback' in r.message][-1]
    assert record.levelname == level and f'path={path}' in record.message
    assert reason in record.message and RAW.decode() not in record.message
    assert p.content_hash not in record.message


@pytest.mark.parametrize('fallback', [False, True])
async def test_lookup_read_diagnostics_use_existing_tool_called(clients, r2, monkeypatch, fallback):
    monkeypatch.setattr(get_settings(), 'archive_body_read_lookup', 'r2-first')
    await clients.get(URL)
    await archive.drain()
    if fallback:
        r2.objects.clear()
    events = []
    monkeypatch.setattr(service.analytics, 'capture', lambda who, event, props, **kw: events.append((event, props)))
    response = await clients.get(URL)
    await archive.drain()
    assert response.headers['x-treg-cache'] == 'hit'
    called = [props for name, props in events if name == 'tool_called']
    assert len(called) == 1
    assert called[0]['cache_body_source'] == ('db' if fallback else 'r2')
    assert called[0]['cache_body_fallback_reason'] == ('not_found' if fallback else 'none')
    assert called[0]['cache_r2_read_ms'] >= 0


async def test_upload_does_not_use_read_timeout(clients, r2, monkeypatch):
    monkeypatch.setattr(get_settings(), 'archive_r2_read_timeout_s', 0.001)
    monkeypatch.setattr(get_settings(), 'archive_r2_timeout_s', 1.0)
    put = r2.put
    async def slow(body, **kw):
        await asyncio.sleep(0.02)
        return await put(body, **kw)
    monkeypatch.setattr(r2, 'put', slow)
    await clients.get(URL)
    await archive.drain()
    assert (await snapshots())[0].body_storage == 'both'


@pytest.mark.parametrize('failure,expected', [(PermissionError('secret-body'), 'permission_denied'),
    (RuntimeError('SignatureDoesNotMatch secret-body'), 'store_error'),
    (RuntimeError('request timed out secret-body'), 'store_error'),
    (RuntimeError('503 secret-body'), 'store_error')])
async def test_sdk_read_errors_are_sanitized(failure, expected):
    from treg.infra.object_store import R2ObjectStore, ObjectStoreError
    class SDK:
        async def get_async(self, path):
            raise failure
    store = R2ObjectStore(SDK(), 1000)
    with pytest.raises(ObjectStoreError) as exc:
        await store.get('0' * 64)
    assert exc.value.reason == expected and str(exc.value) == expected


@pytest.mark.parametrize('path', ['lookup', 'result', 'terminal'])
async def test_r2_only_missing_body_has_no_db_fallback(clients, r2, monkeypatch, path):
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'r2')
    monkeypatch.setattr(get_settings(), 'archive_body_read_' + path, 'r2-first')
    if path == 'terminal':
        await archive.store_terminal_response('terminal-test', 'tikhub', EP, 200, RAW)
    else:
        await clients.get(URL)
        await archive.drain()
        await audit.drain()
    r2.objects.clear()
    if path == 'lookup':
        calls = r2.put_calls
        response = await clients.get(URL)
        await archive.drain()
        assert 'x-treg-cache' not in response.headers and response.content == RAW
        assert r2.put_calls == calls + 1
    elif path == 'result':
        call = (await clients.get('/calls')).json()[0]
        result = (await clients.get(f"/calls/{call['id']}/result")).json()
        assert result['stored'] is False and result['response']['body_text'] is None
    else:
        assert await archive.load_terminal_responses([('terminal-test', EP)]) == {}


def test_retired_comparison_env_is_ignored(monkeypatch):
    from treg.config import Settings
    monkeypatch.setenv('TREG_ARCHIVE_COMPARISON_MODE', 'legacy_noise')
    assert not hasattr(Settings(_env_file=None), 'archive_comparison_mode')


def test_normalized_mode_and_r2_read_guard(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, 'archive_mode', 'typo')
    monkeypatch.setattr(s, 'archive_body_write', 'r2')
    assert archive_bodies.validate_configuration() is False
    monkeypatch.setattr(s, 'archive_mode', 'serve')
    with pytest.raises(RuntimeError, match='all read paths'):
        archive_bodies.validate_configuration()


@pytest.mark.parametrize('jurisdiction', ['', '.eu', '.fedramp'])
def test_r2_endpoint_jurisdictions(jurisdiction):
    from treg.infra.object_store import R2_ENDPOINT_RE
    assert R2_ENDPOINT_RE.fullmatch('https://' + 'a'*32 + jurisdiction + '.r2.cloudflarestorage.com')
    assert not R2_ENDPOINT_RE.fullmatch('https://' + 'a'*32 + '.evil.r2.cloudflarestorage.com')


async def test_terminal_upload_budget_leaves_time_for_db(clients, r2, monkeypatch, caplog):
    monkeypatch.setattr(archive, '_TERMINAL_UPLOAD_S', 0.02)
    r2.gate = asyncio.Event()
    await archive.store_terminal_response('bounded', 'tikhub', EP, 200, RAW)
    assert (await snapshots())[0].body_storage == 'db'
    assert 'upload deadline exceeded' in caplog.text


async def test_upload_wait_is_not_transfer_timeout(r2, monkeypatch):
    monkeypatch.setattr(get_settings(), 'archive_r2_timeout_s', 0.01)
    sem = archive_bodies._upload_sem()
    for _ in range(get_settings().archive_r2_upload_concurrency):
        await sem.acquire()
    observation = archive_bodies.StorageReport()
    task = asyncio.create_task(archive_bodies.prepare(RAW, archive.content_hash(RAW), mode="both", observation=observation))
    await asyncio.sleep(0.03)
    assert not task.done()
    for _ in range(get_settings().archive_r2_upload_concurrency):
        sem.release()
    assert (await task).storage == 'both'
    assert observation.props['archive_body_queue_wait_ms'] >= 20

async def test_terminal_reads_are_bounded_and_do_not_load_db_body(clients, r2, monkeypatch):
    from sqlalchemy import inspect
    monkeypatch.setattr(get_settings(), 'archive_body_read_terminal', 'r2-first')
    for i in range(17):
        await archive.store_terminal_response(str(i), 'tikhub', EP, 200, RAW)
    original_pointer = archive_bodies.pointer
    async def pointer(session, row, path):
        assert 'body' in inspect(row).unloaded
        return await original_pointer(session, row, path)
    monkeypatch.setattr(archive_bodies, 'pointer', pointer)
    async def no_db(pointer):
        pytest.fail('successful R2 read fetched DB fallback')
    monkeypatch.setattr(archive_bodies, '_db_fallback', no_db)
    original_get = r2.get
    active = peak = 0
    async def get(key):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.01)
            return await original_get(key)
        finally:
            active -= 1
    monkeypatch.setattr(r2, 'get', get)
    assert len(await archive.load_terminal_responses([(str(i), EP) for i in range(17)])) == 17
    assert peak == 8


async def test_r2_legacy_admission_restarts_unknown_baseline(clients, r2, monkeypatch):
    from tests.test_cache_result_admission import FOUND
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'r2')
    async def store():
        await archive._store(method='GET', endpoint_id='hunter.companies.emails', provider='hunter',
                             url='https://api.hunter.io/v2/domain-search?domain=example.com',
                             caller_body=b'', headers={}, status_code=200,
                             media_type='application/json', body=FOUND)
    await store()
    async with db.session_maker() as session:
        key = (await session.execute(select(ArchiveKey))).scalar_one()
        key.result_state = key.result_snapshot_id = key.result_observed_version = None
        session.add(key)
        await session.commit()
    async def no_get(key):
        pytest.fail('lazy write classification fetched R2 body')
    monkeypatch.setattr(r2, 'get', no_get)
    await store()
    async with db.session_maker() as session:
        key = (await session.execute(select(ArchiveKey))).scalar_one()
        assert key.result_state == 'found' and key.stable_seen == 0
    await store()
    async with db.session_maker() as session:
        key = (await session.execute(select(ArchiveKey))).scalar_one()
        assert key.result_state == 'found' and key.stable_seen == 1

async def test_db_deadline_reports_timeout_not_cancelled(clients, r2, monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), 'archive_body_write', 'db')
    monkeypatch.setattr(archive, '_STORE_TIMEOUT_S', .01)
    async def blocked(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(archive, '_store_locked', blocked)
    reports = []
    await archive._store(method='GET', endpoint_id=EP, provider='tikhub', url=URL,
                         caller_body=b'', headers={}, status_code=200,
                         media_type='application/json', body=RAW,
                         observation=archive_bodies.StorageReport(emit=reports.append))
    assert len(reports) == 1 and reports[0]['drop_reason'] == 'record_timeout'
    assert any('record_timeout' in record.message for record in caplog.records)


async def test_cancel_before_start_reports_once(clients, r2, monkeypatch):
    reports = []
    archive.record(method='GET', endpoint_id=EP, provider='tikhub', url=URL,
                   caller_body=b'', headers={}, status_code=200,
                   media_type='application/json', body=RAW,
                   observation=archive_bodies.StorageReport(emit=reports.append))
    for task in list(archive_bodies._pending):
        task.cancel()
    await archive.drain()
    assert len(reports) == 1 and reports[0]['drop_reason'] == 'cancelled'


@pytest.mark.parametrize('error,reason', [
    (PermissionError, 'permission_denied'), (TimeoutError, 'timeout'),
    (FileNotFoundError, 'not_found'), (RuntimeError, 'store_error')])
async def test_object_boundary_classifies_put_and_get_by_type(error, reason):
    from treg.infra.object_store import R2ObjectStore, ObjectStoreError
    class Broken:
        async def put_async(self, *args, **kwargs):
            raise error('SignatureDoesNotMatch secret text never forwarded')
        async def get_async(self, *args, **kwargs):
            raise error('SignatureDoesNotMatch secret text never forwarded')
    store = R2ObjectStore(Broken(), 1000)
    with pytest.raises(ObjectStoreError) as caught:
        await store.put(RAW, content_hash=archive.content_hash(RAW))
    assert str(caught.value) == reason
    if error is FileNotFoundError:
        assert await store.get(archive.content_hash(RAW)) is None
    else:
        with pytest.raises(ObjectStoreError) as caught:
            await store.get(archive.content_hash(RAW))
        assert str(caught.value) == reason
