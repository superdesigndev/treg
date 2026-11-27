"""Deterministic config sampling and best-effort streaming invitations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio
import hashlib

import pytest

from treg import hints
from treg.config import get_settings
from test_marketplace_call import platform_on  # noqa: F401 - pytest fixture
from test_routing import enrichment_on  # noqa: F401 - pytest fixture


@pytest.mark.parametrize('kind,field', [('review', 'review_sample_rate'), ('feedback', 'feedback_hint_rate'),
                                        ('mark', 'mark_hint_rate')])
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
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
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
    """A stubbed 2xx call on treg's platform key (or an own tool, or a routed parent, which runs
    no marketplace target of its own) with a static body."""
    from treg.routers import call
    from treg.application.call.types import UpstreamResponse

    async def stream():
        yield b'{}'
    async def close():
        pass
    async def execute(context, client):
        if own:
            context.target = SimpleNamespace(tool=SimpleNamespace(name='example.search'))
        elif tier == 'routed':
            context.endpoint_id = 'treg.example.search'
            context.route_summary = {'identity': {}}
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
    assert all(p['budget'] == 'main' and p['credited_endpoint'] == 'example.search' for p, _ in sent)
    assert all(kw['groups']['team'] for _, kw in sent)
    assert not [a for a, _ in events if a[1] == 'mcp_hint_attached']


async def test_budget_is_per_team(clients, monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
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


async def test_slow_counter_does_not_hold_the_response_for_the_money_timeout(clients, monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    from treg.infra import kv

    stopped = asyncio.Event()

    async def blocked_counter():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    pipe = MagicMock()
    pipe.__aenter__.return_value = pipe
    pipe.execute = AsyncMock(side_effect=blocked_counter)
    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(pipeline=lambda **kwargs: pipe, aclose=AsyncMock())
    monkeypatch.setattr(kv, '_store', store)
    monkeypatch.setattr(kv, '_note_fault', lambda exc: None)
    _platform_call(monkeypatch)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    monkeypatch.setattr(get_settings(), 'feedback_hint_rate', 1)

    # Exercise the actual route -> invitation -> RedisStore.take timeout chain.
    async with asyncio.timeout(0.8):
        response = await clients.get('/call/example.search')
    assert stopped.is_set()
    pipe.__aexit__.assert_awaited_once()
    assert response.status_code == 200 and response.content == b'{}'
    assert response.headers.get('X-Treg-Hint') == 'feedback'
    assert 'X-Treg-Review' not in response.headers


@pytest.mark.parametrize('own', [True, False])
async def test_feedback_hint_rides_plain_http_on_any_successful_call(clients, monkeypatch, own):
    _platform_call(monkeypatch, own=own)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 0)
    monkeypatch.setattr(get_settings(), 'feedback_hint_rate', 1)
    response = await clients.get('/call/example.search')
    assert response.headers.get('X-Treg-Hint') == 'feedback'
    assert 'X-Treg-Review' not in response.headers


SHOWS_ALL = {'X-Treg-Hints': 'review,mark,feedback'}


def _mark_only(monkeypatch, **settings):
    for field, value in {'review_sample_rate': 0, 'feedback_hint_rate': 0, 'mark_hint_rate': 1,
                         'mark_hint_capabilities': [], 'mark_hint_texts': [], **settings}.items():
        monkeypatch.setattr(get_settings(), field, value)


async def test_mark_hint_carries_its_text_and_variant(clients, monkeypatch):
    from treg import analytics

    _platform_call(monkeypatch)
    _mark_only(monkeypatch, mark_hint_texts=['A {call_id} \u2014 caf\u00e9', '  ', 'B {call_id}'])
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append((args, kw)))
    response = await clients.get('/call/example.search', headers=SHOWS_ALL)
    assert response.status_code == 200 and response.content == b'{}'
    assert response.headers['X-Treg-Hint'] == 'mark'
    assert 'X-Treg-Review' not in response.headers
    sent, = [a[2] for a, _ in events if a[1] == 'hint_attached']
    call_id = sent['call_id']
    variant, text = hints.mark_hint(call_id)
    # blank variants are skipped; non-ASCII is blanked so the text fits a header
    assert text in (f'A {call_id}   caf', f'B {call_id}')
    assert response.headers['X-Treg-Hint-Text'] == text
    assert (sent['kind'], sent['variant']) == ('mark', variant)


def test_mark_hint_default_text_and_stable_variant(monkeypatch):
    monkeypatch.setattr(get_settings(), 'mark_hint_texts', [])
    assert hints.mark_hint('c_1') == (0, hints.MARK_HINT.format(call_id='c_1'))
    monkeypatch.setattr(get_settings(), 'mark_hint_texts', ['x {call_id}', 'y {call_id}'])
    seen = {hints.mark_hint(str(i))[0] for i in range(50)}
    assert seen == {0, 1}
    assert all(hints.mark_hint(str(i)) == hints.mark_hint(str(i)) for i in range(50))


@pytest.mark.parametrize('capabilities,expected', [
    ([], 'mark'), (['search'], 'mark'), (['example.search'], 'mark'),
    (['people.search'], None), (['arch'], None),
])
async def test_mark_hint_only_on_listed_capabilities(clients, monkeypatch, capabilities, expected):
    _platform_call(monkeypatch)
    _mark_only(monkeypatch, mark_hint_capabilities=capabilities)
    assert (await clients.get('/call/example.search', headers=SHOWS_ALL)).headers.get('X-Treg-Hint') == expected


@pytest.mark.parametrize('tier', ['platform', 'credential', 'routed'])
async def test_mark_hint_on_any_catalog_tier_never_on_own_tools(clients, monkeypatch, tier):
    _platform_call(monkeypatch, tier=tier)
    _mark_only(monkeypatch)
    assert (await clients.get('/call/example.search', headers=SHOWS_ALL)).headers.get('X-Treg-Hint') == 'mark'


async def test_mark_hint_never_on_own_tools(clients, monkeypatch):
    _platform_call(monkeypatch, own=True)
    _mark_only(monkeypatch)
    assert (await clients.get('/call/example.search', headers=SHOWS_ALL)).headers.get('X-Treg-Hint') is None


async def test_mark_hint_budget_then_review_wins(clients, monkeypatch):
    _platform_call(monkeypatch)
    _mark_only(monkeypatch, mark_hint_per_hour=1, feedback_hint_rate=1)
    kinds = [(await clients.get('/call/example.search', headers=SHOWS_ALL)).headers.get('X-Treg-Hint') for _ in range(3)]
    assert kinds == ['mark', 'feedback', 'feedback']
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    assert (await clients.get('/call/example.search', headers=SHOWS_ALL)).headers.get('X-Treg-Hint') == 'review'


async def test_mark_hint_rides_a_real_routed_call(clients, enrichment_on, monkeypatch):
    """A routed parent runs no marketplace target of its own; it is still a catalog call."""
    from treg import analytics
    from treg.application.call import service as call_service
    from test_routing import ROUTED, _relay_by_provider

    monkeypatch.setattr(call_service, 'relay', _relay_by_provider(
        {'tomba': [(200, {'data': {'email': 'patrick@stripe.com', 'score': 99}})]}, []))
    _mark_only(monkeypatch, mark_hint_capabilities=['people.email.find'])
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append((args, kw)))
    response = await clients.post(f'/call/{ROUTED}', headers=SHOWS_ALL, json={'full_name': 'Patrick Collison', 'domain': 'stripe.com'})
    assert response.status_code == 200, response.text
    assert response.headers.get('X-Treg-Hint') == 'mark'
    sent, = [a[2] for a, _ in events if a[1] == 'hint_attached']
    assert sent['endpoint_id'] == ROUTED
    assert response.headers['X-Treg-Hint-Text'] == hints.mark_hint(response.headers['X-Treg-Call-Id'])[1]


async def test_a_client_that_does_not_say_never_gets_a_mark_and_spends_no_budget(clients, monkeypatch):
    """Released CLIs drop a mark in silence: they get the feedback hint instead."""
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    from treg.application.call import invite

    _platform_call(monkeypatch)
    _mark_only(monkeypatch, mark_hint_per_hour=1, feedback_hint_rate=1)
    legacy = [(await clients.get('/call/example.search')).headers.get('X-Treg-Hint') for _ in range(2)]
    assert legacy == ['feedback', 'feedback']
    shown = (await clients.get('/call/example.search', headers=SHOWS_ALL)).headers.get('X-Treg-Hint')
    assert shown == 'mark'
    only_review = await clients.get('/call/example.search', headers={'X-Treg-Hints': 'review'})
    assert only_review.headers.get('X-Treg-Hint') is None
    assert invite.accepted_kinds(None, "claude-code") == invite.LEGACY_KINDS
    assert invite.accepted_kinds(' Review , MARK ,') == frozenset({'review', 'mark'})


@pytest.mark.parametrize('kind', ['review', 'feedback'])
async def test_review_and_feedback_carry_server_words(clients, monkeypatch, kind):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    _platform_call(monkeypatch)
    for field, value in {'review_sample_rate': int(kind == 'review'), 'mark_hint_rate': 0,
                         'feedback_hint_rate': 1}.items():
        monkeypatch.setattr(get_settings(), field, value)
    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append((args, kw)))
    legacy = await clients.get('/call/example.search')
    assert legacy.headers['X-Treg-Hint'] == kind
    # A client that did not declare what it shows prints its own line: no words that may name
    # flags its release lacks.
    assert 'X-Treg-Hint-Text' not in legacy.headers
    events.clear()
    response = await clients.get('/call/example.search', headers=SHOWS_ALL)
    assert response.headers['X-Treg-Hint'] == kind
    call_id, = [a[2]['call_id'] for a, _ in events if a[1] == 'hint_attached']
    assert response.headers['X-Treg-Hint-Text'] == hints.header_text(kind, call_id)[1]
    assert call_id in response.headers['X-Treg-Hint-Text']
    if kind == 'review':
        assert '--rows' in response.headers['X-Treg-Hint-Text']


async def test_review_is_invited_on_a_real_routed_call(clients, enrichment_on, monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    from treg.application.call import service as call_service
    from test_routing import ROUTED, _relay_by_provider

    monkeypatch.setattr(call_service, 'relay', _relay_by_provider(
        {'tomba': [(200, {'data': {'email': 'patrick@stripe.com', 'score': 99}})]}, []))
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    response = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Patrick Collison', 'domain': 'stripe.com'})
    assert response.status_code == 200, response.text
    assert response.headers.get('X-Treg-Hint') == 'review'


@pytest.mark.parametrize('own', [True, False])
async def test_an_upstream_cannot_speak_as_a_treg_invitation(clients, monkeypatch, own):
    """The hint headers are treg's alone: an upstream's copies are removed before treg decides,
    so a provider (or a team's own tool) cannot put words in treg's hint slot."""
    from treg import analytics
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
            context.marketplace = SimpleNamespace(endpoint_id='example.search', tier='platform')
        spoof = ((b'x-treg-hint', b'mark'), (b'x-treg-hint-text', b'UPSTREAM-CONTROLLED-INSTRUCTION'),
                 (b'x-treg-review', b'requested'), (b'x-upstream-own', b'kept'))
        return UpstreamResponse(200, spoof, stream(), close)
    monkeypatch.setattr(call, 'execute_call', execute)
    monkeypatch.setattr(call.catalog_store, 'load', lambda: SimpleNamespace(by_id={'example.search': {}}))
    _mark_only(monkeypatch, mark_hint_rate=0)
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append((args, kw)))
    response = await clients.get('/call/example.search')
    assert response.status_code == 200 and response.content == b'{}'
    for name in ('X-Treg-Hint', 'X-Treg-Hint-Text', 'X-Treg-Review'):
        assert name not in response.headers
    assert response.headers['X-Upstream-Own'] == 'kept'
    assert not [a for a, _ in events if a[1] == 'hint_attached']


async def test_an_unnamed_caller_is_never_asked_for_a_review_and_spends_no_budget(clients, monkeypatch):
    """A script calling the API with no `X-Treg-Client` and no `X-Treg-Hints` has no agent reading
    the hint: it gets no review invitation, and the team's budget stays for its agents' calls. A
    caller that declares it shows reviews is still asked."""
    from treg.application.call import invite

    _platform_call(monkeypatch)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    monkeypatch.setattr(get_settings(), 'review_budget_per_hour', 1)
    assert invite.accepted_kinds(None) == invite.UNNAMED_KINDS
    assert invite.accepted_kinds(None, 'claude-code') == invite.LEGACY_KINDS
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') is None
    named = await clients.get('/call/example.search', headers={'X-Treg-Client': 'claude-code'})
    assert named.headers.get('X-Treg-Hint') == 'review'
    declared = await clients.get('/call/example.search', headers={'X-Treg-Hints': 'review'})
    assert declared.headers.get('X-Treg-Hint') is None  # the budget of one went to the named call


async def test_an_unnamed_caller_still_gets_the_feedback_hint(clients, monkeypatch):
    _platform_call(monkeypatch)
    monkeypatch.setattr(get_settings(), 'review_sample_rate', 1)
    monkeypatch.setattr(get_settings(), 'feedback_hint_rate', 1)
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') == 'feedback'
