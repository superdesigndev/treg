"""The public provider pages (/tools/<service>), /pricing, and the signed-out marketplace redirect.

These routes render everything from the catalog, so the tests read their expectations off the
same census (`_provider_rows`) rather than naming a vendor that could be retired. The route-shape
tests exist because `/tools` is also the authed team-tools API: a public page must never shadow
it (it once did, and 59 tests went red), and a signed-out visit must land on a page that answers.
"""

from __future__ import annotations

import json
import re

from httpx import AsyncClient

from treg.routers import web as web_routes


def _ld(html: str) -> list[dict]:
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _first_provider() -> str:
    rows = web_routes._provider_rows()
    assert rows, "catalog has no providers"
    return rows[0]["service"]


async def test_every_provider_page_renders(clients: AsyncClient):
    for row in web_routes._provider_rows():
        r = await clients.get(f"/tools/{row['service']}")
        assert r.status_code == 200, (row["service"], r.status_code)
        assert f'href="/tools/{row["service"]}"' in r.text or "canonical" in r.text


async def test_unknown_provider_is_404_and_api_paths_are_not_shadowed(clients: AsyncClient):
    assert (await clients.get("/tools/nope")).status_code == 404
    # The API's own GETs on this prefix still answer as the API (JSON), never as a page.
    r = await clients.get("/tools")
    assert r.headers["content-type"].startswith("application/json"), r.headers["content-type"]
    r = await clients.get("/tools/by-name/anything")
    assert r.headers["content-type"].startswith("application/json"), r.headers["content-type"]


async def test_signed_out_marketplace_redirects_to_public_page(clients: AsyncClient):
    service = _first_provider()
    r = await clients.get(f"/app/marketplace/{service}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/tools/{service}"
    # An unknown service 404s here rather than redirecting into a 404.
    r = await clients.get("/app/marketplace/nope", follow_redirects=False)
    assert r.status_code == 404


async def test_provider_page_reads_the_observation_reader_not_the_session(clients, caplog):
    """`/tools/{service}` once handed `_observed_or_empty` the request's AsyncSession instead of the
    app's observation reader; the measured line silently came up empty and every page view logged
    an AttributeError traceback (prod, 2026-09-06)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="treg.catalog"):
        r = await clients.get("/tools/dataforseo")
    assert r.status_code == 200
    assert "endpoint stats unavailable" not in caplog.text




async def test_a_provider_page_offers_treg_s_key_only_where_this_deploy_holds_one(clients, monkeypatch):
    """A priceable row is not a platform offer on its own: without an enabled treg key the call
    refuses, so the page must not say treg serves it (an Atly page once claimed 4 platform tools)."""
    from treg.config import get_settings
    monkeypatch.delenv("TREG_PLATFORM_KEY_TRYKITT", raising=False)
    get_settings.cache_clear()
    keyless = (await clients.get("/tools/trykitt")).text
    assert "Platform + BYOK" not in keyless and "NO SIGNUP" not in keyless

    monkeypatch.setenv("TREG_PLATFORM_KEY_TRYKITT", "TEST-KITT-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "trykitt")
    get_settings.cache_clear()
    try:
        served = (await clients.get("/tools/trykitt")).text
    finally:
        get_settings.cache_clear()
    assert "Platform + BYOK" in served or "NO SIGNUP" in served


async def test_a_public_provider_page_never_asks_for_an_account(clients: AsyncClient, monkeypatch):
    """A keyless provider has nothing to connect, enabled on this server or not; once enabled its
    anonymous rows are served free with no key."""
    import dataclasses
    from treg import oauth_providers
    from treg.config import get_settings
    from treg.domain.catalog import store as catalog_store
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public"))
    # a public provider has only keyless rows (the validator's rule): retire the keyed ones here
    for e in catalog_store.load().endpoints:
        if e["provider"] == "financialdatasets" and e.get("platform_auth") != "anonymous":
            monkeypatch.setitem(e, "status", "retired")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "")
    monkeypatch.setattr(get_settings(), "platform_providers", "")
    off = await clients.get("/tools/financialdatasets")
    assert off.status_code == 200
    assert "connect your own account" not in off.text and "Own connection required" not in off.text
    assert "Nothing to connect" in off.text and "Not enabled on this server" in off.text
    assert "your own account · never metered" not in off.text
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    on = await clients.get("/tools/financialdatasets")
    # Nothing is metered and no credential is injected: the metered-provider copy must not appear.
    for page in (off.text, on.text):
        for stale in (" · metered per call", "run on treg.to&#x27;s key", "run on treg.to's key",
                      "metered per call from a prepaid balance", "Platform calls are charged per call",
                      "that key always wins", "those calls are never metered", "billed per call",
                      "credential is injected server-side", "its own metered proxy",
                      "pricing at the provider&#x27;s own rate", "Price before the call"):
            assert stale not in page, stale
    assert "tools · free · $0.000 markup" in on.text and "Free, no key" in on.text
    assert "your own account · never metered" not in on.text
    assert "public API with no key: there is nothing to connect" in on.text
    assert "Nothing to sign up for" not in on.text and "free treg.to account" in on.text


async def test_a_key_providers_keyless_rows_are_never_called_treg_key(clients: AsyncClient, monkeypatch):
    """A key provider with keyless rows, allow-listed but with no key on this server: those rows run
    with no key at all, so the page labels them free and never counts them as treg's platform key."""
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "")
    page = (await clients.get("/tools/financialdatasets")).text
    assert "Free, no key" in page
    assert "Platform + BYOK" not in page and "support treg's platform key" not in page
    # no treg key exists here, and most of its tools need the team's own: never describe treg's key
    for false in ("run on treg.to's key", "metered per call", "Eligible tools run on treg.to",
                  "No. Eligible tools"):
        assert false not in page, false


async def test_a_public_provider_not_enabled_never_promises_free_calls(clients: AsyncClient, monkeypatch):
    """Nothing to connect, but this server refuses its calls: the page must not say they are free."""
    import dataclasses
    from treg import oauth_providers
    from treg.config import get_settings
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public"))
    monkeypatch.setattr(get_settings(), "platform_providers", "")
    page = (await clients.get("/tools/financialdatasets")).text
    assert "not enabled on this server" in page.lower() and "Nothing to connect" in page
    for false in ("No charge per call", "Nothing. treg.to does not charge", "Its tools are free",
                  "free, no key, nothing to connect", "connect your own account", "tools · free",
                  "✓ 200", "a live-verified call", "What your agent can now do", "<i>✓</i>",
                  "Show me the price first", "<p>A free treg.to account is all it takes"):
        assert false not in page, false


async def test_a_key_provider_with_its_key_counts_keyless_rows_apart(clients: AsyncClient, monkeypatch):
    """With its key set, a key provider's keyed rows are platform + BYOK and its keyless rows are free:
    never one count that swallows the other (a negative BYOK-only count)."""
    import re
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "test-key")
    page = (await clients.get("/tools/financialdatasets")).text
    assert "Free, no key" in page and "Platform + BYOK" in page
    assert not re.search(r"-\d+ BYOK only", page), "keyless rows counted as platform rows"


async def test_a_mixed_key_providers_kicker_counts_its_free_rows(clients: AsyncClient, monkeypatch):
    """Keyed rows on treg's key, keyed rows that need the team's own key, and keyless rows: three
    counts that add up to the tools on the page."""
    import re
    from treg.config import get_settings
    from treg.domain.catalog import store as catalog_store
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "test-key")
    cat = catalog_store.load()
    keyed = next(e["id"] for e in cat.endpoints
                 if e["provider"] == "financialdatasets" and e.get("platform_auth") != "anonymous")
    # an own-account row is never served on treg's key: it needs the team's own key
    monkeypatch.setitem(cat.by_id[keyed], "scope", "own_account")
    page = (await clients.get("/tools/financialdatasets")).text
    m = re.search(r"(\d+) tools · (\d+) platform \+ BYOK · (\d+) BYOK only · (\d+) free, no key", page)
    assert m, re.findall(r"\d+ tools · [^<]{0,90}", page)[:2]
    total, platform, byok, free = map(int, m.groups())
    assert platform + byok + free == total and byok >= 1 and free >= 1


async def test_the_provider_tools_record_marks_a_public_provider(clients: AsyncClient, monkeypatch):
    """The dashboard's provider page reads a public provider from /catalog/providers/<service>: it is
    not in the Connect listing, so the record must say it is public."""
    import dataclasses
    from treg import oauth_providers
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public"))
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "platform_providers", "")
    rec = (await clients.get("/catalog/providers/financialdatasets")).json()["provider"]
    assert rec["auth_kind"] == "public" and rec["display_name"] and rec["enabled"] is False
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    assert (await clients.get("/catalog/providers/financialdatasets")).json()["provider"]["enabled"] is True
    keyed = (await clients.get("/catalog/providers/hunter")).json()["provider"]
    assert "auth_kind" not in keyed


async def test_the_platform_payload_says_whether_this_server_serves_each_provider(
        clients: AsyncClient, monkeypatch):
    """The dashboard labels a provider's keyless tools on a platform page from this payload's
    `enabled`: without it a tool this server refuses would read as free."""
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "platform_providers", "")
    off = (await clients.get("/catalog/platforms/stocks")).json()["providers"]["financialdatasets"]
    assert off["enabled"] is False
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    on = (await clients.get("/catalog/platforms/stocks")).json()["providers"]["financialdatasets"]
    assert on["enabled"] is True


async def test_a_key_provider_s_served_keyless_rows_are_counted_beside_its_own_account_rows(clients: AsyncClient, monkeypatch):
    """Keyless rows are served with no key, so a key provider whose keyed rows treg cannot serve still
    has a served surface: the page must count it, not read as own-account only."""
    from treg.config import get_settings
    from treg.domain.catalog import store as catalog_store
    cat = catalog_store.load()
    assert any(e["provider"] == "financialdatasets" and e.get("platform_auth") == "anonymous" for e in cat.endpoints)
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "k")
    real = type(cat).platform_eligible
    monkeypatch.setattr(type(cat), "platform_eligible",
                        lambda self, e: False if e.get("provider") == "financialdatasets"
                        and e.get("platform_auth") != "anonymous" else real(self, e))  # keyed rows only
    page = await clients.get("/tools/financialdatasets")
    assert page.status_code == 200
    # the keyed rows need the team's own account; the served keyless rows are counted beside it
    keyless = sum(1 for e in cat.endpoints if e["provider"] == "financialdatasets"
                  and e.get("platform_auth") == "anonymous" and not e.get("status") and e.get("kind") != "routed")
    import re as _re
    assert f"your own account · never metered · {keyless} free, no key" in page.text, _re.findall(r"kicker\">([^<]*)", page.text)
