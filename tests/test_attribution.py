"""First-touch traffic-source attribution: web/sitetrack.js → `treg_utm` cookie → Org.utm_*.

Why this exists: a paid sponsor link (`?utm_source=botdirectory.ai&utm_campaign=edge`) produced a
signup count of "unknown" — the landing page loaded no analytics and the signup doors persisted only
Google click ids. These pin the two halves that fix it: the cookie is read in BOTH signup doors, and
the script is served on every public page with the PostHog key templated in (or inert without one).
"""
from urllib.parse import quote

import pytest
from sqlmodel import select

from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import Org

COOKIE = quote("botdirectory.ai|sponsor|edge||p1|botdirectory.ai", safe="")


async def _org(org_id: int) -> Org:
    async with session_maker() as db:
        return (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()


async def test_signup_persists_the_utm_cookie(clients):
    r = await clients.post("/users", json={"email": "sponsor@example.com"}, cookies={"treg_utm": COOKIE})
    assert r.status_code == 200, r.text
    org = await _org(r.json()["org_id"])
    assert (org.utm_source, org.utm_medium, org.utm_campaign) == ("botdirectory.ai", "sponsor", "edge")
    assert org.utm_term is None  # empty slot → NULL, not ""
    assert org.utm_content == "p1"
    assert org.utm_referrer == "botdirectory.ai"


async def test_org_creation_persists_the_utm_cookie(clients):
    # The OTHER signup door: browser sign-in → mandatory first-team creation via /orgs.
    r = await clients.post("/orgs", json={"name": "sponsored team"}, cookies={"treg_utm": COOKIE})
    assert r.status_code == 200, r.text
    org = await _org(r.json()["org_id"])
    assert org.utm_source == "botdirectory.ai" and org.utm_campaign == "edge"


async def test_signup_without_the_cookie_leaves_source_null(clients):
    r = await clients.post("/users", json={"email": "organic@example.com"})
    assert r.status_code == 200, r.text
    org = await _org(r.json()["org_id"])
    assert org.utm_source is None and org.utm_referrer is None


async def test_referrer_only_cookie_records_just_the_host(clients):
    # A link with no utm tags still has a referrer — that alone is worth keeping.
    r = await clients.post("/users", json={"email": "ref@example.com"},
                           cookies={"treg_utm": quote("|||||news.ycombinator.com", safe="")})
    assert r.status_code == 200, r.text
    org = await _org(r.json()["org_id"])
    assert org.utm_source is None and org.utm_referrer == "news.ycombinator.com"


async def test_hostile_cookie_is_capped_and_never_errors(clients):
    r = await clients.post("/users", json={"email": "hostile@example.com"},
                           cookies={"treg_utm": "x" * 5000})
    assert r.status_code == 200, r.text
    org = await _org(r.json()["org_id"])
    assert org.utm_source == "x" * 100


async def test_sitetrack_is_inert_without_a_posthog_key(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "", raising=False)
    r = await clients.get("/sitetrack.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "var key = ''" in r.text  # analytics half off
    assert "treg_utm" in r.text     # cookie half always present
    assert "{POSTHOG_KEY}" not in r.text


async def test_sitetrack_templates_the_posthog_key(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test", raising=False)
    monkeypatch.setattr(get_settings(), "posthog_host", "https://eu.i.posthog.com/", raising=False)
    r = await clients.get("/sitetrack.js")
    assert "var key = 'phc_test'" in r.text
    assert "'https://eu.i.posthog.com'" in r.text
    assert "capture_pageview: true" in r.text  # the whole point: the first hop is recorded


@pytest.mark.parametrize("path", ["/", "/resources", "/tutorial", "/use-cases/seo-data-for-ai-agents"])
async def test_public_pages_load_sitetrack_before_the_ad_script(clients, path):
    r = await clients.get(path, follow_redirects=True)
    assert r.status_code == 200, (path, r.status_code)
    assert '<script src="/sitetrack.js"></script>' in r.text, path
