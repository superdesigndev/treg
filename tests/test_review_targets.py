"""Targeted review invitations: endpoints short of reviews are invited more often."""
import asyncio
import itertools
from types import SimpleNamespace

import pytest

from treg.application import review_targets
from treg.application.call import invite
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallReview, Org
from test_hints import _platform_call
from test_marketplace_call import platform_on  # noqa: F401 - pytest fixture
from test_routing import enrichment_on  # noqa: F401 - pytest fixture
from test_route_verify import verifiers_on  # noqa: F401 - pytest fixture
from test_dropcontact_routing import dropcontact_platform  # noqa: F401 - pytest fixture
from test_reviews import rows, seed, submit


@pytest.fixture
def targeting(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'review_sample_rate', 0.2)
    monkeypatch.setattr(settings, 'review_target_reviews', 100)
    monkeypatch.setattr(settings, 'review_target_rate', 1.0)
    monkeypatch.setattr(settings, 'review_target_team_cap', 10)
    return settings


_team_no = itertools.count()


async def add_reviews(clients, endpoint_id: str, *, teams: int, each: int) -> list[int]:
    org_ids = []
    for _ in range(teams):
        async with session_maker() as db:
            n = next(_team_no)
            org = Org(name=f'Reviewer {endpoint_id} {n}', slug=f'rv-{endpoint_id.replace(".", "-")}-{n}')
            db.add(org)
            await db.flush()
            for i in range(each):
                db.add(CallReview(org_id=org.id, user_email='tim@superdesign.dev',
                                  call_id=f'{endpoint_id}-{n}-{i}', endpoint_id=endpoint_id,
                                  usefulness='useful'))
            await db.commit()
            org_ids.append(org.id)
    return org_ids


async def test_rate_climbs_as_an_endpoint_lacks_reviews_and_one_team_counts_at_most_its_cap(
        clients, targeting):
    await add_reviews(clients, 'busy.search', teams=10, each=12)  # 10 teams x cap 10 = 100
    await add_reviews(clients, 'one-team.search', teams=1, each=60)  # counts as 10
    await review_targets.warm()
    assert review_targets.rate('busy.search') == pytest.approx(0.2)
    assert review_targets.rate('one-team.search') == pytest.approx(0.2 + 0.8 * 0.9)
    assert review_targets.rate('unreviewed.search') == pytest.approx(1.0)
    assert review_targets.rate(None) == pytest.approx(0.2)


async def test_a_team_at_its_cap_is_not_invited_again_and_a_new_team_gets_the_floor(
        clients, targeting, monkeypatch):
    monkeypatch.setattr(targeting, 'review_target_new_team_rate', 0.6)
    full, = await add_reviews(clients, 'busy.search', teams=1, each=12)
    partial, = await add_reviews(clients, 'busy.search', teams=1, each=4)
    await add_reviews(clients, 'busy.search', teams=9, each=10)  # the endpoint is at its target
    await review_targets.warm()
    assert review_targets.rate('busy.search', full) == 0
    assert review_targets.rate('busy.search', partial) == pytest.approx(0.2)  # already counted
    assert review_targets.rate('busy.search', 10**9) == pytest.approx(0.6)  # a new team
    assert review_targets.rate('busy.search') == pytest.approx(0.2)  # no team known
    # Short endpoint: a new team takes the higher of the boost and the floor.
    assert review_targets.rate('unreviewed.search', 10**9) == pytest.approx(1.0)
    # The team at its cap on one endpoint is still invited on another.
    assert review_targets.rate('unreviewed.search', full) == pytest.approx(1.0)
    assert not any(review_targets.sampled(f'c{i}', 'busy.search', full) for i in range(200))


async def test_off_or_before_the_first_read_every_endpoint_gets_the_base_rate(clients, targeting,
                                                                               monkeypatch):
    assert review_targets.rate('unreviewed.search') == pytest.approx(0.2)  # nothing read yet
    monkeypatch.setattr(targeting, 'review_target_reviews', 0)
    await review_targets.warm()
    assert review_targets.rate('unreviewed.search') == pytest.approx(0.2)


async def test_a_short_endpoint_is_invited_and_a_full_one_falls_back_to_base(clients, targeting,
                                                                              monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    _platform_call(monkeypatch)
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_budget_per_hour', 100)
    await review_targets.warm()
    response = await clients.get('/call/example.search')
    assert response.headers.get('X-Treg-Hint') == 'review'
    await add_reviews(clients, 'example.search', teams=10, each=10)
    await review_targets.warm()
    response = await clients.get('/call/example.search')
    assert response.headers.get('X-Treg-Hint') is None


async def test_a_team_that_filled_an_endpoint_gets_no_invitation_there(clients, targeting,
                                                                        monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    _platform_call(monkeypatch)
    monkeypatch.setattr(targeting, 'review_sample_rate', 1.0)
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_budget_per_hour', 100)
    await review_targets.warm()
    response = await clients.get('/call/example.search')
    assert response.headers.get('X-Treg-Hint') == 'review'
    from sqlalchemy import select as _select
    async with session_maker() as db:  # the caller's team is the only one signed up
        org_id, = (await db.execute(_select(Org.id))).scalars().all()
    async with session_maker() as db:
        for i in range(10):
            db.add(CallReview(org_id=org_id, user_email='tim@superdesign.dev', call_id=f'own-{i}',
                              endpoint_id='example.search', usefulness='useful'))
        await db.commit()
    await review_targets.warm()
    response = await clients.get('/call/example.search')
    assert response.status_code == 200
    assert response.headers.get('X-Treg-Hint') is None


async def test_a_spent_budget_still_asks_about_a_short_endpoint_from_the_extra_one(
        clients, targeting, monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    _platform_call(monkeypatch)
    monkeypatch.setattr(targeting, 'review_sample_rate', 0.5)  # below the boost of 1.0
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_budget_per_hour', 1)
    monkeypatch.setattr(targeting, 'review_target_budget_per_hour', 1)
    await review_targets.warm()  # example.search holds no reviews: short of its target

    async def hint():
        response = await clients.get('/call/example.search')
        assert response.status_code == 200
        return response.headers.get('X-Treg-Hint')

    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append(args))
    assert await hint() == 'review'  # the team budget
    assert await hint() == 'review'  # spent: the extra one, for a short endpoint
    assert await hint() is None      # both spent
    assert [a[2]['budget'] for a in events if a[1] == 'hint_attached'] == ['main', 'extra']


async def test_the_extra_budget_is_not_spent_on_a_full_endpoint_or_when_off(clients, targeting,
                                                                            monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"
    _platform_call(monkeypatch)
    monkeypatch.setattr(targeting, 'review_sample_rate', 0.5)  # below the boost of 1.0
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_budget_per_hour', 1)
    await review_targets.warm()
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') == 'review'
    # Off (the default): a spent budget asks nothing more, even about a short endpoint.
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') is None
    monkeypatch.setattr(targeting, 'review_target_budget_per_hour', 5)
    await add_reviews(clients, 'example.search', teams=10, each=10)  # at its target
    await review_targets.warm()
    assert not review_targets.boosted('example.search')
    assert (await clients.get('/call/example.search')).headers.get('X-Treg-Hint') is None
    assert review_targets.boosted('unreviewed.search')


async def test_both_budgets_are_one_store_operation(clients, targeting, monkeypatch):
    from treg.infra import kv
    clients.headers["X-Treg-Client"] = "claude-code"
    _platform_call(monkeypatch)
    monkeypatch.setattr(targeting, 'review_sample_rate', 0.5)
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_target_budget_per_hour', 3)
    await review_targets.warm()  # example.search is short: boosted
    real = kv.store()
    seen = []

    class Counting:
        async def take(self, *args):
            seen.append('take')
            return await real.take(*args)

        async def take_first(self, budgets, ttl_s):
            seen.append(('take_first', [limit for _, limit in budgets]))
            return await real.take_first(budgets, ttl_s)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(kv, '_store', Counting())
    response = await clients.get('/call/example.search')
    assert response.headers.get('X-Treg-Hint') == 'review'
    assert seen == [('take_first', [targeting.review_budget_per_hour, 3])]


async def test_invited_is_recomputed_with_the_targeted_rate(clients, targeting, monkeypatch):
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    await review_targets.warm()
    org = await seed(clients, credential_tier='platform')
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    assert row.invited is True


def test_a_routed_call_is_targeted_by_the_child_its_review_is_credited_to():
    # With X-Treg-Route-Confirm the first hit is returned (served_by) and a later provider confirms
    # it; the review is credited to the latest successful child, so the invitation targets that one.
    routed = SimpleNamespace(marketplace=None, route_summary={
        'served_by': 'first.people.email.find', 'credited': 'confirmer.people.email.find'})
    direct = SimpleNamespace(marketplace=SimpleNamespace(endpoint_id='example.search'),
                             route_summary=None)
    assert invite.review_endpoint(routed) == 'confirmer.people.email.find'
    assert invite.review_endpoint(direct) == 'example.search'
    assert invite.review_endpoint(SimpleNamespace(marketplace=None, route_summary={})) is None


@pytest.mark.parametrize('child', [True, False])
async def test_a_routed_review_recomputes_invited_with_the_credited_child(clients, targeting,
                                                                          monkeypatch, child):
    from treg.models import CallRecord
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    await review_targets.warm()
    org = await seed(clients, endpoint_id='treg.search', provider='treg', credential_tier='routed')
    async with session_maker() as db:
        db.add(CallRecord(org_id=org['org_id'], user_email='tim@superdesign.dev', tool_name='example',
                          method='GET', path='/', call_ref='review-call:r0',
                          status_code=200 if child else 503, endpoint_id='winner.search',
                          provider='winner'))
        await db.commit()
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    # A short credited child is invited at the boosted rate; with no successful child the
    # invitation had no endpoint to target, so only the base rate (here 0) applies.
    assert row.invited is child


async def test_a_real_routed_call_is_targeted_by_its_successful_child(clients, enrichment_on,
                                                                      targeting, monkeypatch):
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    from treg.application.call import service as call_service
    from test_routing import ROUTED, _relay_by_provider

    monkeypatch.setattr(call_service, 'relay', _relay_by_provider(
        {'tomba': [(200, {'data': {'email': 'patrick@stripe.com', 'score': 99}})] * 2}, []))
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    await review_targets.warm()
    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda *args, **kw: events.append(args))
    ask = {'full_name': 'Patrick Collison', 'domain': 'stripe.com'}
    first = await clients.post(f'/call/{ROUTED}', json=ask)
    assert first.status_code == 200, first.text
    assert first.headers.get('X-Treg-Hint') == 'review'
    child = first.json()['_treg']['served_by']  # one hit, no confirm: also the credited child
    sent, = [a[2] for a in events if a[1] == 'hint_attached']
    assert (sent['endpoint_id'], sent['credited_endpoint']) == (ROUTED, child)
    await add_reviews(clients, child, teams=10, each=10)
    await review_targets.warm()
    second = await clients.post(f'/call/{ROUTED}', json=ask | {'full_name': 'John Collison'})
    assert second.status_code == 200, second.text
    assert second.headers.get('X-Treg-Hint') is None


async def test_a_confirmed_find_targets_and_credits_the_confirmer(clients, verifiers_on, targeting,
                                                                   monkeypatch):
    """Tomba's hit is returned and Hunter confirms it: the review is credited to Hunter, so the
    invitation targets Hunter too, and once Hunter is full the next call is not boosted."""
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    from treg.application.call import service as call_service
    from test_route_confirm import ON, _hunter
    from test_route_verify import ASK, FIND, TOMBA_HIT, _relay

    monkeypatch.setattr(call_service, 'relay', _relay(
        {'tomba': [TOMBA_HIT] * 2, 'hunter': [_hunter('Patrick@Stripe.COM')] * 2}, []))
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_target_reviews', 1)
    await review_targets.warm()
    first = await clients.post(f'/call/{FIND}', json=ASK, headers=ON)
    assert first.status_code == 200, first.text
    assert first.json()['_treg']['served_by'] == 'tomba.people.email.find'
    assert first.headers.get('X-Treg-Hint') == 'review'
    for _ in range(100):  # the call record is written after the answer; the API says retry
        review = await clients.post('/reviews', json={'call_id': first.headers['X-Treg-Call-Id'],
                                                      'usefulness': 'useful'})
        if review.status_code != 404:
            break
        await asyncio.sleep(0.02)
    assert review.status_code == 201, review.text
    row, = await rows()
    assert (row.endpoint_id, row.invited) == ('hunter.people.email.find', True)
    await review_targets.warm()
    second = await clients.post(f'/call/{FIND}', json=ASK, headers=ON)
    assert second.status_code == 200, second.text
    assert second.headers.get('X-Treg-Hint') is None


async def test_a_stuck_counts_read_never_holds_a_worker_and_leaves_the_base_rate(targeting,
                                                                                monkeypatch):
    import contextlib

    @contextlib.asynccontextmanager
    async def stuck():
        class Db:
            async def execute(self, *_):
                await asyncio.Event().wait()
        yield Db()

    monkeypatch.setattr(review_targets, 'session_maker', stuck)
    monkeypatch.setattr(review_targets, 'READ_TIMEOUT_S', 0.05)
    await asyncio.wait_for(review_targets.warm(), timeout=2)
    assert review_targets.rate('unreviewed.search') == pytest.approx(0.2)


async def test_an_async_find_credits_and_targets_the_find_not_its_poll(clients, dropcontact_platform,
                                                                       targeting, monkeypatch):
    """Dropcontact submits a job (people.email.find) and is polled (jobs.get): the review is about
    the find, so both the review and the invitation go to the find, and a full find stops the boost."""
    clients.headers["X-Treg-Client"] = "claude-code"  # an agent runtime: an unnamed caller is never asked
    from test_dropcontact_routing import _relay as dropcontact_relay

    dropcontact_relay(monkeypatch, {'success': True, 'data': [{
        'first_name': 'Peter', 'last_name': 'Jackson',
        'email': [{'email': 'peter.jackson@company.com', 'qualification': 'nominative@pro'}]}]}, [])
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    monkeypatch.setattr(targeting, 'feedback_hint_rate', 0)
    monkeypatch.setattr(targeting, 'review_target_reviews', 1)
    await review_targets.warm()
    ask = dict(json={'first_name': 'Peter', 'last_name': 'Jackson', 'domain': 'company.com'},
               headers={'X-Treg-Route-Prefer': 'dropcontact', 'X-Treg-Route-Waterfall': '0'})
    first = await clients.post('/call/treg.people.email.find', **ask)
    assert first.status_code == 200, first.text
    assert first.headers.get('X-Treg-Hint') == 'review'
    for _ in range(100):  # the call records are written after the answer; the API says retry
        review = await clients.post('/reviews', json={'call_id': first.headers['X-Treg-Call-Id'],
                                                      'usefulness': 'useful'})
        if review.status_code != 404:
            break
        await asyncio.sleep(0.02)
    assert review.status_code == 201, review.text
    row, = await rows()
    assert (row.endpoint_id, row.invited) == ('dropcontact.people.email.find', True)
    await review_targets.warm()
    second = await clients.post('/call/treg.people.email.find', **ask)
    assert second.status_code == 200, second.text
    assert second.headers.get('X-Treg-Hint') is None
