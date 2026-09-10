"""Deterministic config sampling and best-effort streaming invitations."""
from types import SimpleNamespace
import hashlib

import pytest
from pydantic import ValidationError

from treg import hints
from treg.config import Settings, get_settings


@pytest.mark.parametrize('kind,field', [('review', 'review_sample_rate'), ('feedback', 'feedback_hint_rate')])
def test_sampling_boundaries_stability_and_salt(monkeypatch, kind, field):
    settings = get_settings()
    monkeypatch.setattr(settings, field, 0)
    assert not hints.sampled(kind, 'call')
    monkeypatch.setattr(settings, field, 1)
    assert hints.sampled(kind, 'call')
    monkeypatch.setattr(settings, field, 0.5)
    for i in range(100):
        ref = str(i)
        expected = int.from_bytes(hashlib.sha256(f'{kind}:{ref}'.encode()).digest()[:8], 'big') < 2**63
        assert hints.sampled(kind, ref) == expected
        assert hints.sampled(kind, ref) == expected


@pytest.mark.parametrize('field', ['review_sample_rate', 'feedback_hint_rate'])
@pytest.mark.parametrize('rate', [-0.1, 1.1, float('nan'), float('inf')])
def test_invalid_rates(field, rate):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: rate})


@pytest.mark.parametrize('catalog,status,headers,sample,expected', [
    ('direct', 200, {}, True, True), ('routed', 201, {}, True, False),
    ('credential', 200, {}, True, False), ('tool', 200, {}, True, False),
    ('own', 200, {}, True, False), ('direct', 200, {}, False, False),
    ('direct', 199, {}, True, False), ('direct', 300, {}, True, False),
    ('direct', 503, {}, True, False),
    ('direct', 200, {'X-Treg-Idempotent-Replay': 'true'}, True, False),
    ('cached', 200, {}, True, False),
    ('direct', 200, {}, 'exception', False),
])
async def test_header_before_stream_without_changing_response(
    clients, monkeypatch, catalog, status, headers, sample, expected,
):
    from treg.routers import call
    from treg.application.call.types import UpstreamResponse

    consumed = []
    async def stream():
        consumed.append('body')
        yield b'exact upstream bytes'
    async def close():
        consumed.append('close')
    async def execute(context, client):
        if catalog in ('direct', 'cached', 'credential', 'tool'):
            context.cached = catalog == 'cached'
            context.marketplace = SimpleNamespace(
                endpoint_id='example.search',
                tier=catalog if catalog in ('credential', 'tool') else 'platform',
            )
        elif catalog == 'own':
            context.target = SimpleNamespace(tool=SimpleNamespace(name='example.search'))
        return UpstreamResponse(status, tuple((k.lower().encode(), v.encode()) for k, v in headers.items()),
                                stream(), close)
    def sampled(kind, ref):
        assert consumed == []  # decided before the body streams
        assert ref
        if sample == 'exception':
            raise RuntimeError('optional hook broke')
        return sample if kind == 'review' else False
    monkeypatch.setattr(call, 'execute_call', execute)
    monkeypatch.setattr(call.catalog_store, 'load', lambda: SimpleNamespace(by_id={'example.search': {}}))
    monkeypatch.setattr(hints, 'sampled', sampled)
    response = await clients.get('/call/example.search')
    assert response.status_code == status
    assert response.content == b'exact upstream bytes'
    assert (response.headers.get('X-Treg-Hint') == 'review') is expected
    assert (response.headers.get('X-Treg-Review') == 'requested') is expected  # CLI <= 0.18
    assert consumed == ['body', 'close']


def _platform_call(monkeypatch, *, tier='platform', own=False):
    """A stubbed 2xx call on treg's platform key (or an own tool) with a static body."""
    from treg.routers import call
    from treg.application.call.types import UpstreamResponse

    async def stream():
        yield b'{}'
    async def close():
        pass
    async def execute(context, client):
        if own:
            context.target = SimpleNamespace(tool=SimpleNamespace(name='example.search'))
        else:
            context.marketplace = SimpleNamespace(endpoint_id='example.search', tier=tier)
        return UpstreamResponse(200, (), stream(), close)
    monkeypatch.setattr(call, 'execute_call', execute)
    monkeypatch.setattr(call.catalog_store, 'load', lambda: SimpleNamespace(by_id={'example.search': {}}))


async def test_review_budget_caps_a_team_per_hour_and_every_invitation_is_an_event(clients, monkeypatch):
    from treg import analytics

    _platform_call(monkeypatch)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    monkeypatch.setattr(get_settings(), 'feedback_hint_rate', 0)
    monkeypatch.setattr(get_settings(), 'review_budget_per_hour', 2)
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append((args, kw)))
    kinds = []
    for _ in range(4):
        response = await clients.get('/call/example.search', headers={'X-Treg-Client': 'claude-code'})
        assert response.status_code == 200
        kinds.append(response.headers.get('X-Treg-Hint'))
    assert kinds == ['review', 'review', None, None]
    sent = [(a[2], kw) for a, kw in events if a[1] == 'hint_attached']
    assert [p['kind'] for p, _ in sent] == ['review', 'review']
    assert all(p['client'] == 'claude-code' and p['endpoint_id'] == 'example.search' and p['call_id']
               for p, _ in sent)
    assert all(kw['groups']['team'] for _, kw in sent)
    assert not [a for a, _ in events if a[1] == 'mcp_hint_attached']


async def test_budget_is_per_team(clients, monkeypatch):
    from treg.application.call import invite
    from treg.infra import kv

    _platform_call(monkeypatch)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    monkeypatch.setattr(get_settings(), 'review_budget_per_hour', 1)
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') == 'review'
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') is None
    # Another team's budget is untouched: its key has never been taken.
    assert await kv.store().take(invite.budget_key(999), 1, 60)


async def test_unavailable_store_withholds_the_review_but_not_the_answer(clients, monkeypatch):
    from treg.infra import kv

    class Down:
        async def take(self, key, limit, ttl_s):
            return False  # what RedisStore returns on any fault: a budget that cannot be checked is spent
        async def ping(self):
            return False
        async def aclose(self):
            pass
    _platform_call(monkeypatch)
    monkeypatch.setattr(kv, '_store', Down())
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    monkeypatch.setattr(get_settings(), 'feedback_hint_rate', 1)
    response = await clients.get('/call/example.search')
    assert response.status_code == 200 and response.content == b'{}'
    assert response.headers.get('X-Treg-Hint') == 'feedback'  # the lighter hint still rides along
    assert 'X-Treg-Review' not in response.headers


@pytest.mark.parametrize('own', [True, False])
async def test_feedback_hint_rides_plain_http_on_any_successful_call(clients, monkeypatch, own):
    _platform_call(monkeypatch, own=own)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 0)
    monkeypatch.setattr(get_settings(), 'feedback_hint_rate', 1)
    response = await clients.get('/call/example.search')
    assert response.headers.get('X-Treg-Hint') == 'feedback'
    assert 'X-Treg-Review' not in response.headers
