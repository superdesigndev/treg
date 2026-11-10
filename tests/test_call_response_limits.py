"""Complete free result downloads and bounded settlement evidence over real HTTP.

Both HTTP hops use loopback sockets. No customer data or upstream credentials are used.
Run with a unique TREG_TEST_DB_URL; conftest resets that database.
"""

import asyncio
import hashlib
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import pytest
import uvicorn
from sqlmodel import select

from treg.api import app
from treg import audit, archive
from treg.config import get_settings
from treg.domain.catalog import store
from treg.infra.db import session_maker
from treg.models import (
    ArchiveSnapshot, AsyncResourceRecord, AsyncTaskRecord, CallRecord, Hold, IdempotentCall, LedgerEntry, Org,
)
from treg.timeutil import utcnow_naive

LIMIT = 8 * 1024 * 1024
BINARY = bytes(range(256)) * (10 * 1024 * 1024 // 256)
JSON_BODY = b'{"padding":"' + b'x' * (10 * 1024 * 1024) + b'","cost":0.000001}'
FETCH = 'openrouter.video-gen.result.retrieve'
POLL = 'openrouter.video-gen.task.status'


@pytest.fixture
async def wire(clients, monkeypatch):
    state = {'body': BINARY, 'type': 'video/mp4', 'status': 200, 'hits': 0,
             'sizes': [], 'closes': 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['hits'] += 1
            if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                while True:
                    size = int(self.rfile.readline().strip().split(b';')[0], 16)
                    if not size:
                        self.rfile.readline()
                        break
                    self.rfile.read(size)
                    self.rfile.read(2)
            else:
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
            body = state['body']
            self.send_response(state['status'])
            if state['type']:
                self.send_header('Content-Type', state['type'])
            self.send_header('Content-Length', str(len(body) + state.get('missing_tail', 0)))
            if state['status'] == 206:
                self.send_header('Content-Range', f'bytes 0-{len(body)-1}/{len(body)}')
            self.send_header('ETag', '"synthetic-full-body"')
            self.end_headers()
            try:
                if state.get('pause'):
                    self.wfile.write(body[:1024])
                    self.wfile.flush()
                    state['pause'].wait(10)
                    self.wfile.write(body[1024:])
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

        do_POST = do_GET

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()

    class RecordedStream(httpx.AsyncByteStream):
        def __init__(self, source):
            self.source = source

        async def __aiter__(self):
            async for chunk in self.source:
                state['sizes'].append(len(chunk))
                yield chunk

        async def aclose(self):
            state['closes'] += 1
            await self.source.aclose()

    class LoopbackOnly(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            request.url = request.url.copy_with(scheme='http', host='127.0.0.1',
                                                port=upstream.server_port)
            response = await super().handle_async_request(request)
            response.stream = RecordedStream(response.stream)
            return response

    original = app.state.http
    app.state.http = httpx.AsyncClient(transport=LoopbackOnly(), trust_env=False)
    monkeypatch.setenv('TREG_PLATFORM_KEY_OPENROUTER', 'synthetic-key')
    monkeypatch.setenv('TREG_PLATFORM_KEY_TIKHUB', 'synthetic-key')
    monkeypatch.setenv('TREG_PLATFORM_KEY_DATAFORSEO', 'synthetic-key')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'openrouter,tikhub,dataforseo')
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), 'archive_mode', 'off')
    async with session_maker() as db:
        db.add(AsyncResourceRecord(org_id=1, provider='openrouter',
               resource_kind=f'fetch:{FETCH}', resource_id='synthetic-task',
               source_call_id='synthetic-submission'))
        descriptor = store.load().by_id['openrouter.video-gen.wan-3-0.from_text']['async']
        db.add(AsyncTaskRecord(call_id='synthetic-submission', org_id=1, provider='openrouter',
               endpoint_id='openrouter.video-gen.wan-3-0.from_text', task_id='synthetic-task',
               reserved_micro=0, status='settled', descriptor=descriptor,
               next_check_at=utcnow_naive()))
        await db.commit()

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    port = sock.getsockname()[1]
    # log_config=None: uvicorn's default dictConfig and log_level would reconfigure the process's
    # `uvicorn` loggers for every later test (uvicorn.error left at CRITICAL hides server faults).
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_config=None, access_log=False,
                                           ws='none'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                     headers=clients.headers, trust_env=False) as client:
            yield client, state
    finally:
        if state.get('pause'):
            state['pause'].set()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        await app.state.http.aclose()
        app.state.http = original
        await asyncio.to_thread(upstream.shutdown)
        upstream.server_close()
        thread.join(timeout=2)
        get_settings.cache_clear()


@pytest.mark.parametrize('status', [200, 206])
async def test_free_result_streams_complete_bytes_and_retries_live(wire, status, monkeypatch):
    client, state = wire
    state['status'] = status
    monkeypatch.setattr(get_settings(), 'archive_mode', 'shadow')
    for _ in range(2):
        response = await client.get(f'/call/{FETCH}?id=synthetic-task',
                                    headers={'Idempotency-Key': 'synthetic-retry', 'Range': 'bytes=0-'})
        assert response.status_code == status
        assert hashlib.sha256(response.content).digest() == hashlib.sha256(BINARY).digest()
        assert response.headers['etag'] == '"synthetic-full-body"'
        assert response.headers['x-treg-cost-micro'] == '0'
        assert 'x-treg-idempotent-replay' not in response.headers
        if status == 206:
            assert response.headers['content-range'] == f'bytes 0-{len(BINARY)-1}/{len(BINARY)}'
        await audit.drain()
        async with session_maker() as db:
            entries = (await db.execute(select(LedgerEntry).where(
                LedgerEntry.call_id == response.headers['x-treg-call-id']))).scalars().all()
            assert sorted(e.kind for e in entries) == ['reserve', 'settle']
            assert all(e.amount_micro == 0 for e in entries)
            row = (await db.execute(select(CallRecord).where(
                CallRecord.call_ref == response.headers['x-treg-call-id']))).scalar_one()
            assert row.response_bytes is None
            assert not (await db.execute(select(Hold))).scalars().all()
            assert not (await db.execute(select(IdempotentCall))).scalars().all()
    await archive.drain()
    async with session_maker() as db:
        assert not (await db.execute(select(ArchiveSnapshot))).scalars().all()
        original = await db.get(AsyncTaskRecord, 'synthetic-submission')
        assert original.status == 'settled'
    assert state['hits'] == state['closes'] == 2


@pytest.mark.parametrize('path,free_poll', [
    (f'{POLL}?id=synthetic-task', True),
    ('tikhub.tiktok.video.comments?aweme_id=synthetic', False),
])
@pytest.mark.parametrize('media', ['application/json', 'video/mp4'])
async def test_oversized_evidence_fails_without_charge_archive_or_replay(wire, path, free_poll, media, monkeypatch):
    client, state = wire
    state['type'] = media
    monkeypatch.setattr(get_settings(), 'archive_mode', 'shadow')
    async with session_maker() as db:
        balance = (await db.get(Org, 1)).balance_micro
    for _ in range(2):
        response = await client.get('/call/' + path, headers={'Idempotency-Key': 'synthetic-retry'})
        assert response.status_code == 502
        assert response.json()['detail']['error'] == 'response_buffer_limit'
        assert response.headers['x-treg-cost-micro'] == '0'
        async with session_maker() as db:
            entries = (await db.execute(select(LedgerEntry).where(
                LedgerEntry.call_id == response.headers['x-treg-call-id']))).scalars().all()
            assert sorted(e.kind for e in entries) == ([] if free_poll else ['release', 'reserve'])
            assert (await db.get(Org, 1)).balance_micro == balance
            assert not (await db.execute(select(Hold))).scalars().all()
            assert not (await db.execute(select(IdempotentCall))).scalars().all()
    await archive.drain()
    async with session_maker() as db:
        assert not (await db.execute(select(ArchiveSnapshot))).scalars().all()
    assert state['hits'] == state['closes'] == 2


@pytest.mark.parametrize('sizes', [[LIMIT], [LIMIT, 1], [LIMIT - 1, 2], [LIMIT + 1]])
async def test_buffer_boundaries_close_once_and_never_return_a_prefix(sizes):
    from treg.application.call.settle import _buffer_response
    from treg.application.call.types import GatewayFailed, UpstreamResponse
    closed = 0

    async def stream():
        for size in sizes:
            yield b'x' * size

    async def close():
        nonlocal closed
        closed += 1

    response = UpstreamResponse(200, (), stream(), close)
    if sum(sizes) > LIMIT:
        with pytest.raises(GatewayFailed) as failure:
            await _buffer_response(response)
        assert failure.value.kind == 'response_buffer_limit'
    else:
        result, body = await _buffer_response(response)
        assert len(body) == LIMIT
        assert b''.join([chunk async for chunk in result.body_stream]) == body
    assert closed == 1


async def test_short_read_closes_response_and_releases_hold(wire):
    client, state = wire
    state['body'] = b'synthetic'
    state['missing_tail'] = 1024
    response = await client.get('/call/tikhub.tiktok.video.comments?aweme_id=synthetic',
                                headers={'Idempotency-Key': 'synthetic-failure'})
    assert response.status_code == 502
    assert state['closes'] == 1
    async with session_maker() as db:
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == response.headers['x-treg-call-id']))).scalars().all()
        assert sorted(e.kind for e in entries) == ['release', 'reserve']
        assert not (await db.execute(select(Hold))).scalars().all()
        assert not (await db.execute(select(IdempotentCall))).scalars().all()


async def test_download_starts_before_eof_and_disconnect_closes_upstream(wire):
    client, state = wire
    state['pause'] = Event()
    async with asyncio.timeout(5):
        async with client.stream('GET', f'/call/{FETCH}?id=synthetic-task',
                                 headers={'Idempotency-Key': 'synthetic-disconnect'}) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_raw():
                assert chunk
                break
        while state['closes'] != 1:
            await asyncio.sleep(.01)
    async with session_maker() as db:
        assert not (await db.execute(select(Hold))).scalars().all()
        assert not (await db.execute(select(IdempotentCall))).scalars().all()
    state['pause'].set()


async def test_unknown_result_is_denied_before_network(wire):
    client, state = wire
    response = await client.get(f'/call/{FETCH}?id=synthetic-unknown')
    assert response.status_code == 403 and state['hits'] == 0


@pytest.mark.parametrize('own_tool', [False, True])
async def test_own_credentials_keep_large_streams(wire, clients, own_tool):
    client, state = wire
    if own_tool:
        created = await clients.post('/tools', json={'name': 'synthetic-download',
            'base_url': 'https://download.example/file', 'bindings': []})
        target = 'synthetic-download'
    else:
        created = await clients.post('/secrets', json={'name': 'openrouter', 'value': 'synthetic-own'})
        target = FETCH + '?id=synthetic-task'
    assert created.status_code == 200
    response = await client.get('/call/' + target)
    assert response.status_code == 200 and response.content == BINARY
    assert 'x-treg-cost-micro' not in response.headers
    assert state['closes'] == 1


async def test_cli_download_is_exact_binary(wire):
    client, state = wire
    env = {**os.environ, 'TREG_URL': str(client.base_url).rstrip('/'),
           'TREG_TOKEN': client.headers['x-treg-token'], 'TREG_TELEMETRY': '0',
           'TREG_ORG': '', 'TREG_CONFIG': '/dev/null'}
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'treg.cli', 'call', FETCH, '-p', 'id=synthetic-task',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, stderr.decode(errors='replace')
    assert hashlib.sha256(stdout).digest() == hashlib.sha256(BINARY).digest()


@pytest.mark.parametrize('large', [False, True])
async def test_large_json_is_never_charged_from_missing_evidence(wire, large):
    client, state = wire
    state['type'] = 'application/json'
    state['body'] = JSON_BODY if large else b'{"cost":0.000001}'
    response = await client.post('/call/dataforseo.web.page.audit',
                                 json=[{'url': 'https://synthetic.example/page'}])
    assert response.status_code == (502 if large else 200)
    await audit.drain()
    async with session_maker() as db:
        row = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == response.headers['x-treg-call-id']))).scalar_one()
    assert row.cost_estimated_micro == 150
    assert row.cost_observed_micro == (None if large else 1)
    assert row.cost_charged_micro == (0 if large else 1)


@pytest.mark.parametrize('overrides', [
    {'tier': 'platform-overflow'}, {'tier': 'own'}, {'cost_type': 'per_call'},
    {'estimate_micro': 1}, {'billed_oauth': True},
    {'async_owner_call_id': 'original'}, {'async_descriptor': {'id_from': 'id'}},
    {'resource_ownership': {}},
    {'resource_ownership': {'requires': {'kind': 'poll:synthetic'}}},
    {'resource_ownership': {'requires': {'kind': 'fetch:synthetic'},
                            'produces': [{'kind': 'fetch:next', 'path': 'id'}]}},
])
def test_only_final_free_fetches_can_skip_evidence(overrides):
    from treg.application.call.resolve import MarketplaceCall
    from treg.models import Tool
    fields = dict(tool=Tool(org_id=1, name='synthetic', owner='test@example.invalid',
                            base_url='https://synthetic.example', host='synthetic.example'),
                  upstream='https://synthetic.example/file', consumed=set(),
                  endpoint_id='synthetic.fetch', provider='synthetic', tier='platform',
                  cost_type='free', resource_ownership={'requires': {'kind': 'fetch:synthetic'}})
    assert MarketplaceCall(**fields).streamable_free_result
    assert not MarketplaceCall(**(fields | overrides)).streamable_free_result


GEMINI = 'google-ai.image-gen.gemini-3-pro-image'
GEMINI_USAGE = {'promptTokenCount': 17, 'candidatesTokenCount': 2098, 'thoughtsTokenCount': 159,
                'candidatesTokensDetails': [{'modality': 'IMAGE', 'tokenCount': 2000}]}
# A 4K-shaped answer: inline base64 image plus a thought signature, far above the 8 MiB buffer.
GEMINI_BODY = (b'{"candidates":[{"content":{"parts":[{"inlineData":{"mimeType":"image/jpeg","data":"'
               + b'Q' * (11 * 1024 * 1024) + b'"},"thoughtSignature":"' + b'S' * (9 * 1024 * 1024)
               + b'"}],"role":"model"},"finishReason":"STOP"}],"usageMetadata":'
               + json.dumps(GEMINI_USAGE).encode() + b',"modelVersion":"gemini-3-pro-image"}')
GEMINI_REQUEST = {'contents': [{'parts': [{'text': 'synthetic prompt'}]}],
                  'generationConfig': {'responseModalities': ['IMAGE'],
                                       'imageConfig': {'imageSize': '4K'}}}


@pytest.fixture
async def gemini(wire, monkeypatch):
    from treg.application.call import settle
    client, state = wire
    monkeypatch.setenv('TREG_PLATFORM_KEY_GOOGLE_AI', 'synthetic-key')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'google-ai')
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), 'archive_mode', 'shadow')
    state.update(type='application/json', body=GEMINI_BODY)
    yield client, state
    # The /call relay closes the spool in a background task after the last byte, so the client
    # can hold the whole body a moment before the budget returns. Wait for that close, bounded.
    for _ in range(200):
        if settle._spool_in_use == 0:
            break
        await asyncio.sleep(0.01)
    assert settle._spool_in_use == 0  # every spool returned its disk budget


async def _gemini_call(client, **headers):
    return await client.post(f'/call/{GEMINI}?model=gemini-3-pro-image', json=GEMINI_REQUEST,
                             headers=headers)


async def test_inline_media_answer_relays_whole_and_settles_on_token_meters(gemini):
    from treg.domain import money
    client, state = gemini
    async with session_maker() as db:
        balance = (await db.get(Org, 1)).balance_micro
    response = await _gemini_call(client, **{'Idempotency-Key': 'synthetic-gemini'})
    assert response.status_code == 200, response.text[:500]
    assert hashlib.sha256(response.content).digest() == hashlib.sha256(GEMINI_BODY).digest()
    # 17*2 + (2098+159)*12 + 2000*108 = 243,118 micro-USD: exactly Google's token bill.
    charged = money.with_margin(243_118)
    assert response.headers['x-treg-cost-micro'] == str(charged)
    await audit.drain()
    await archive.drain()
    async with session_maker() as db:
        assert (await db.get(Org, 1)).balance_micro == balance - charged
        row = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == response.headers['x-treg-call-id']))).scalar_one()
        assert row.credential_tier == 'platform' and row.cost_charged_micro == charged
        assert row.cost_estimated_micro == 260_000  # the 4K row held
        assert row.response_bytes == len(GEMINI_BODY)
        assert not (await db.execute(select(Hold))).scalars().all()
        # A spooled body is neither archived nor kept for replay; the claim is released.
        assert not (await db.execute(select(ArchiveSnapshot))).scalars().all()
        assert not (await db.execute(select(IdempotentCall))).scalars().all()
    assert state['closes'] == 1


async def test_inline_media_answer_without_usage_settles_at_the_hold(gemini):
    from treg.domain import money
    client, state = gemini
    state['body'] = GEMINI_BODY.replace(b'"usageMetadata"', b'"somethingElse"')
    response = await _gemini_call(client)
    assert response.status_code == 200 and response.content == state['body']
    # Google answered, so the image exists: with no meters to read, the caller pays the hold.
    assert response.headers['x-treg-cost-micro'] == str(money.with_margin(260_000))


async def test_inline_media_error_stays_buffered_and_uncharged(gemini):
    client, state = gemini
    state.update(status=400, body=b'{"error":{"code":400,"message":"synthetic invalid argument",'
                                  b'"status":"INVALID_ARGUMENT"}}')
    response = await _gemini_call(client)
    assert response.status_code == 400
    assert response.headers['x-treg-cost-micro'] == '0'


async def test_inline_media_answer_over_the_spool_cap_fails_uncharged(gemini, monkeypatch):
    client, state = gemini
    monkeypatch.setattr(get_settings(), 'spool_max_bytes', 16 * 1024 * 1024)
    async with session_maker() as db:
        balance = (await db.get(Org, 1)).balance_micro
    response = await _gemini_call(client)
    assert response.status_code == 502
    assert response.json()['detail']['error'] == 'response_buffer_limit'
    assert response.headers['x-treg-cost-micro'] == '0'
    async with session_maker() as db:
        assert (await db.get(Org, 1)).balance_micro == balance
        assert not (await db.execute(select(Hold))).scalars().all()
    assert state['closes'] == 1


async def test_the_shared_key_serves_only_the_declared_models(gemini):
    """`model` is a path parameter: on treg's key it must be one the row prices, or another
    Gemini model would run on the image model's rates."""
    client, state = gemini
    response = await client.post(f'/call/{GEMINI}?model=gemini-2.5-pro', json=GEMINI_REQUEST)
    assert response.status_code == 400
    assert response.json()['detail']['parameter'] == 'pathParams.model'
    assert state['hits'] == 0
