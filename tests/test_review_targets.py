"""Targeted review invitations: endpoints short of reviews are invited more often."""
from types import SimpleNamespace

import pytest

from treg.application import review_targets
from treg.application.call import invite
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallReview, Org
from test_hints import _platform_call
from test_reviews import rows, seed, submit


@pytest.fixture
def targeting(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'review_sample_rate', 0.2)
    monkeypatch.setattr(settings, 'review_target_reviews', 100)
    monkeypatch.setattr(settings, 'review_target_rate', 1.0)
    monkeypatch.setattr(settings, 'review_target_team_cap', 10)
    return settings


async def add_reviews(clients, endpoint_id: str, *, teams: int, each: int):
    for t in range(teams):
        async with session_maker() as db:
            org = Org(name=f'Reviewer {endpoint_id} {t}', slug=f'rv-{endpoint_id.replace(".", "-")}-{t}')
            db.add(org)
            await db.flush()
            for i in range(each):
                db.add(CallReview(org_id=org.id, user_email='tim@superdesign.dev',
                                  call_id=f'{endpoint_id}-{t}-{i}', endpoint_id=endpoint_id,
                                  usefulness='useful'))
            await db.commit()


async def test_rate_climbs_as_an_endpoint_lacks_reviews_and_one_team_counts_at_most_its_cap(
        clients, targeting):
    await add_reviews(clients, 'busy.search', teams=10, each=12)  # 10 teams x cap 10 = 100
    await add_reviews(clients, 'one-team.search', teams=1, each=60)  # counts as 10
    await review_targets.warm()
    assert review_targets.rate('busy.search') == pytest.approx(0.2)
    assert review_targets.rate('one-team.search') == pytest.approx(0.2 + 0.8 * 0.9)
    assert review_targets.rate('unreviewed.search') == pytest.approx(1.0)
    assert review_targets.rate(None) == pytest.approx(0.2)


async def test_off_or_before_the_first_read_every_endpoint_gets_the_base_rate(clients, targeting,
                                                                               monkeypatch):
    assert review_targets.rate('unreviewed.search') == pytest.approx(0.2)  # nothing read yet
    monkeypatch.setattr(targeting, 'review_target_reviews', 0)
    await review_targets.warm()
    assert review_targets.rate('unreviewed.search') == pytest.approx(0.2)


async def test_a_short_endpoint_is_invited_and_a_full_one_falls_back_to_base(clients, targeting,
                                                                              monkeypatch):
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


async def test_invited_is_recomputed_with_the_targeted_rate(clients, targeting, monkeypatch):
    monkeypatch.setattr(targeting, 'review_sample_rate', 0)
    await review_targets.warm()
    org = await seed(clients, credential_tier='platform')
    assert (await submit(clients, org)).status_code == 201
    row, = await rows()
    assert row.invited is True


def test_a_routed_call_is_targeted_by_the_child_that_answered():
    routed = SimpleNamespace(marketplace=None, route_summary={'served_by': 'winner.people.email.find'})
    direct = SimpleNamespace(marketplace=SimpleNamespace(endpoint_id='example.search'),
                             route_summary=None)
    assert invite.review_endpoint(routed) == 'winner.people.email.find'
    assert invite.review_endpoint(direct) == 'example.search'
    assert invite.review_endpoint(SimpleNamespace(marketplace=None, route_summary=None)) is None
