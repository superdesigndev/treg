"""API-key providers (auth_kind="key") — the marketplace's paste-a-key connect flow.

These share Slack's bring-your-own-credential path (verify → store → auto-provision), differing only
in the header or query param the key rides in. The upstream is the shared in-process ASGI app from
conftest (`/whoami` echoes; `/units` and `/units-bad` model Semrush's plain-text balance responses).
"""

from __future__ import annotations

import httpx
from treg.api import app
from treg.config import Settings
from treg.domain.catalog import store as catalog_store
import dataclasses

from httpx import AsyncClient

from treg import oauth_providers as P


# ---- registry shape ----------------------------------------------------------------------
def test_key_providers_are_offerable_without_deployment_credentials():
    """The user brings the key, so treg holds no app of its own — a key provider must be offerable,
    not shown as 'not configured' the way an unset OAuth provider is."""
    for svc in ("apollo", "pdl", "akta", "hunter", "importyeti", "sumble", "quickenrich", "contactout", "millionverifier", "trykitt", "crunchbase", "tikhub", "brightdata", "semrush",
                "justoneapi", "dataforseo", "seranking", "moz", "majestic", "serpstat", "exa",
                "cloro",
                "lusha", "coresignal", "diffbot", "thecompaniesapi", "leadmagic", "fiber-ai",
                "companyenrich", "oceanio", "tomba", "predictleads", "findymail", "branddev",
                "icypeas", "leadsforge", "influencersclub", "crustdata", "aviato",
                "spyfu", "apify", "meta-ad-library", "serpapi",
                "coingecko", "polygon", "finnhub", "twelvedata", "fmp", "eodhd", "marketstack",
                "tiingo"):
        p = P.get(svc)
        assert p is not None, svc
        assert p.auth_kind == "key", svc
        assert p.uses_pasted_secret is True, svc
        assert p.is_token_kind is False, f"{svc}: an API key is not a Slack bot token"
        assert P.is_configured(p) is True, svc


def test_key_providers_appear_in_the_marketplace_listing():
    listing = {row["service"]: row for row in P.listing()}
    assert listing["apollo"]["category"] == "Enrichment"
    assert listing["apollo"]["auth_kind"] == "key"
    assert listing["semrush"]["category"] == "SEO"
    assert listing["tikhub"]["category"] == "Social media"
    assert listing["coingecko"]["category"] == "Market data"
    assert listing["minimax"]["category"] == "AI generation"
    assert listing["openrouter"]["auth_kind"] == "token"
    assert listing["replicate"]["base_url"] == "https://api.replicate.com/v1"
    assert "Enrichment" in P.CATEGORY_ORDER
    assert "Market data" in P.CATEGORY_ORDER


def test_aigc_token_providers_are_offerable_without_deployment_credentials():
    for service in ("minimax", "openrouter", "replicate"):
        provider = P.get(service)
        assert provider is not None
        assert provider.auth_kind == "token"
        assert provider.uses_pasted_secret is True
        assert P.is_configured(provider) is True
    assert P.get("minimax").probe_method == "POST"
    assert P.get("minimax").probe_json == {}
    assert P.get("minimax").probe_reject_statuses == (401, 403)


# ---- connect-by-key ----------------------------------------------------------------------
async def test_key_connect_provisions_a_header_binding(clients: AsyncClient, monkeypatch):
    """A header key (Apollo's X-Api-Key) is a plain string injected as an env header — never an
    oauth blob with an access_token field that isn't there."""
    # token_verify_field cleared: the generic echo stub doesn't model Apollo's is_logged_in body;
    # this test is about the binding shape, not Apollo's body check (covered separately below).
    monkeypatch.setitem(P.REGISTRY, "apollo", dataclasses.replace(
        P.REGISTRY["apollo"], base_url="http://upstream", probe_path="/whoami", token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "apollo", "token": "sk-apollo"})
    assert r.status_code == 200, r.text
    assert r.json()["health"] == "ok", "a verified key is known-good, not 'unknown'"

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "apollo")
    b = tool["bindings"][0]
    assert b["injector"] == "env" and b["location"] == "header"
    assert b["name"] == "X-Api-Key" and b["format"] == "{secret}"
    assert "secret_field" not in b or b.get("secret_field") in (None, "")


async def test_required_provider_header_is_probed_bound_and_caller_proof(clients: AsyncClient, monkeypatch):
    """A protocol header is provider metadata, not proxy behavior. It must be present during the
    connect probe and become a constant binding that overwrites a caller's stale version."""
    monkeypatch.setitem(P.REGISTRY, "crustdata", dataclasses.replace(
        P.REGISTRY["crustdata"], base_url="http://upstream", probe_path="/requires-version"))
    r = await clients.post("/connections/token", json={"provider": "crustdata", "token": "cr-key"})
    assert r.status_code == 200, r.text

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "crustdata")
    assert tool["bindings"] == [
        {"secret_id": tool["bindings"][0]["secret_id"], "injector": "env", "location": "header",
         "name": "Authorization", "format": "Bearer {secret}"},
        {"secret_id": tool["bindings"][0]["secret_id"], "injector": "env", "location": "header",
         "name": "x-api-version", "format": "2025-11-01"},
    ]
    called = await clients.get(
        "/call/crustdata/requires-version", headers={"x-api-version": "stale-version"})
    assert called.status_code == 200, called.text
    assert called.json()["version"] == "2025-11-01"


async def test_key_connect_supports_a_query_param_key(clients: AsyncClient, monkeypatch):
    """Semrush authenticates the classic API with ?key=… and answers the balance check in PLAIN
    TEXT — the probe must not JSON-parse it, and the tool must bind the key as a query param."""
    monkeypatch.setitem(P.REGISTRY, "semrush", dataclasses.replace(
        P.REGISTRY["semrush"], base_url="http://upstream", probe_url="", probe_path="/units"))
    r = await clients.post("/connections/token", json={"provider": "semrush", "token": "sr-key"})
    assert r.status_code == 200, r.text

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "semrush")
    b = tool["bindings"][0]
    assert b["injector"] == "env" and b["location"] == "query"
    assert b["name"] == "key" and b["format"] == "{secret}"


async def test_a_plain_text_error_body_is_rejected(clients: AsyncClient, monkeypatch):
    """Semrush signals a bad key with HTTP 200 + an "ERROR ..." text body. Storing it anyway just
    moves the failure to the first real report call, after the user has left the setup screen."""
    monkeypatch.setitem(P.REGISTRY, "semrush", dataclasses.replace(
        P.REGISTRY["semrush"], base_url="http://upstream", probe_url="", probe_path="/units-bad"))
    r = await clients.post("/connections/token", json={"provider": "semrush", "token": "sr-bad"})
    assert r.status_code == 422, r.text
    assert "ERROR" in r.text
    assert not [c for c in (await clients.get("/connections")).json() if c["provider"] == "semrush"]


async def test_a_200_with_a_false_verify_field_is_rejected(clients: AsyncClient, monkeypatch):
    """Apollo answers HTTP 200 even for a bad key and signals validity in is_logged_in. token_verify_field
    makes us read that field: a false one is rejected, a true one connects. Verified live against Apollo."""
    apollo = dataclasses.replace(
        P.REGISTRY["apollo"], base_url="http://upstream", probe_path="/verify-field",
        token_verify_field="is_logged_in")
    monkeypatch.setitem(P.REGISTRY, "apollo", apollo)

    bad = await clients.post("/connections/token", json={"provider": "apollo", "token": "sk-bad"})
    assert bad.status_code == 422 and "is_logged_in" in bad.text
    assert not [c for c in (await clients.get("/connections")).json() if c["provider"] == "apollo"]

    ok = await clients.post("/connections/token", json={"provider": "apollo", "token": "sk-good"})
    assert ok.status_code == 200, ok.text


async def test_probe_url_overrides_base_url_for_verification(clients: AsyncClient, monkeypatch):
    """The cheapest key-check can live on a different host than the data API (Semrush's balance is on
    www.semrush.com). probe_url must win: point it at a passing endpoint while probe_path would fail."""
    monkeypatch.setitem(P.REGISTRY, "semrush", dataclasses.replace(
        P.REGISTRY["semrush"], base_url="http://upstream",
        probe_url="http://upstream/units", probe_path="/units-bad"))
    r = await clients.post("/connections/token", json={"provider": "semrush", "token": "sr-key"})
    assert r.status_code == 200, "probe_url must be used, not base_url + probe_path"


async def test_key_connection_lists_and_revokes(clients: AsyncClient, monkeypatch):
    """A key connection is a real connection — it must be visible in the list and revocable by id."""
    monkeypatch.setitem(P.REGISTRY, "tikhub", dataclasses.replace(
        P.REGISTRY["tikhub"], base_url="http://upstream", probe_path="/whoami"))
    r = await clients.post("/connections/token", json={"provider": "tikhub", "token": "th-key"})
    assert r.status_code == 200, r.text

    listed = [c for c in (await clients.get("/connections")).json() if c["provider"] == "tikhub"]
    assert len(listed) == 1 and listed[0]["kind"] == "env"
    assert (await clients.delete(f"/connections/{listed[0]['id']}")).status_code == 200


# ---- HTTP Basic providers: paste a raw pair OR a ready-made Base64 blob ----------------------
async def test_basic_provider_encodes_a_raw_login_password_once(clients: AsyncClient, monkeypatch):
    """DataForSEO/Moz take HTTP Basic. Pasting the raw `login:password`, treg Base64s it once and the
    upstream sees `Basic <b64(login:password)>`."""
    import base64
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    r = await clients.post("/connections/token", json={"provider": "dataforseo", "token": "login:pw"})
    assert r.status_code == 200, r.text
    echoed = (await clients.get("/call/dataforseo/whoami")).json()["auth"]
    assert echoed == "Basic " + base64.b64encode(b"login:pw").decode()


async def test_basic_provider_accepts_a_ready_made_base64_blob(clients: AsyncClient, monkeypatch):
    """The DataForSEO and Moz dashboards ALSO hand out a ready-made Base64 credential, and users paste
    that at least as often as the raw pair. treg must NOT Base64 it a second time — the upstream has to
    receive exactly `Basic <the pasted blob>`, decoding back to the original `login:password`."""
    import base64
    blob = base64.b64encode(b"login:pw").decode()
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    r = await clients.post("/connections/token", json={"provider": "dataforseo", "token": blob})
    assert r.status_code == 200, r.text
    echoed = (await clients.get("/call/dataforseo/whoami")).json()["auth"]
    assert echoed == "Basic " + blob, "a pasted Base64 blob must not be double-encoded"


# ---- corrected probe shapes (regression guards for the 2026-08-13 connect-flow fixes) --------
def test_brightdata_probe_is_a_real_route():
    """The old /datasets/v3/datasets 404'd even for a valid token, refusing every real key. /status is
    the free account check that answers 200 (valid) / 401 (bad)."""
    assert P.get("brightdata").probe_path == "/status"


def test_justoneapi_probe_uses_camelcase_uniqueid():
    """snake_case unique_id made the API answer HTTP 400 ('must input one of them (uniqueId or
    secUid)') and a VALID token was refused. The param is camelCase."""
    assert "uniqueId=" in P.get("justoneapi").probe_path
    assert "unique_id=" not in P.get("justoneapi").probe_path


async def test_probe_parses_json_body_labelled_text_plain(clients: AsyncClient, monkeypatch):
    """ScrapeCreators answers HTTP 200 with a JSON body but a text/plain content-type; validity lives
    in creditCount. Gating the parse on application/json left the field unread and refused a good key."""
    monkeypatch.setitem(P.REGISTRY, "scrapecreators", dataclasses.replace(
        P.REGISTRY["scrapecreators"], base_url="http://upstream",
        probe_path="/credit-json-as-text", token_verify_field="creditCount"))
    r = await clients.post("/connections/token", json={"provider": "scrapecreators", "token": "sc-key"})
    assert r.status_code == 200, r.text


async def test_probe_keeps_a_query_string_baked_into_probe_path(clients: AsyncClient, monkeypatch):
    """A probe_path like `/autocomplete?field=title` must reach the upstream WITH that query — httpx
    drops a URL's own query when params= is passed, which used to 400 the probe and refuse the key."""
    monkeypatch.setitem(P.REGISTRY, "pdl", dataclasses.replace(
        P.REGISTRY["pdl"], base_url="http://upstream",
        probe_path="/needs-query?field=title&text=data", token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "pdl", "token": "pdl-key"})
    assert r.status_code == 200, r.text


async def test_query_token_survives_alongside_a_probe_path_query(clients: AsyncClient, monkeypatch):
    """A query-credential provider (SpyFu: ?api_key=) whose probe_path ALSO carries a required query
    (?domain=) must send both — the merge keeps the path's params and adds the credential on top."""
    monkeypatch.setitem(P.REGISTRY, "spyfu", dataclasses.replace(
        P.REGISTRY["spyfu"], base_url="http://upstream",
        probe_path="/needs-query?field=title", token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "spyfu", "token": "spyfu-secret"})
    assert r.status_code == 200, r.text


async def test_millionverifier_connect_checks_body_and_injects_query(clients, monkeypatch):
    """The live bad-key response is HTTP 200; zero credits must not reject a valid key."""
    import httpx
    from treg.api import app

    def probe(request):
        assert request.url.path == "/api/v3/credits"
        assert "authorization" not in request.headers
        if request.url.params["api"] == "bad-key":
            return httpx.Response(200, json={"result": "error", "error": "apikey_not_found"})
        return httpx.Response(200, json={"credits": 0, "bulk_credits": 0, "renewing_credits": 0, "plan": 4})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "millionverifier", "token": "bad-key"})
        assert bad.status_code == 422, bad.text
        assert "apikey_not_found" in bad.text
        assert not (await clients.get("/tools")).json()
        good = await clients.post("/connections/token", json={"provider": "millionverifier", "token": "good-key"})
        assert good.status_code == 200, good.text
        tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "millionverifier")
        binding = tool["bindings"][0]
        assert binding["location"] == "query" and binding["name"] == "api"
        assert binding["format"] == "{secret}"


def test_millionverifier_platform_key_configuration(monkeypatch):
    from treg.config import Settings
    monkeypatch.setenv("TREG_PLATFORM_KEY_MILLIONVERIFIER", "platform-test-key")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "millionverifier")
    settings = Settings(_env_file=None)
    assert settings.platform_key_for("millionverifier") == "platform-test-key"
    assert P.platform_bindings(P.get("millionverifier")) == [
        {"platform_setting": "platform_key_millionverifier", "injector": "env",
         "location": "query", "name": "api", "format": "{secret}"}]


async def test_quickenrich_connect_uses_free_authenticated_discovery(clients, monkeypatch):
    import httpx
    import json
    from treg.api import app

    def probe(request):
        assert request.method == 'POST'
        assert request.url.host == 'app.quickenrich.io'
        assert request.url.path == '/api/employees/contact-finder'
        assert json.loads(request.content)['per_page'] == 1
        if request.headers['authorization'] == 'Bearer bad-key':
            return httpx.Response(401, json={'success': False, 'message': 'Invalid or inactive API key'})
        return httpx.Response(200, json={'success': True, 'data': [], 'meta': {'credits_used': 0, 'remaining_credits': 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        bad = await clients.post('/connections/token', json={'provider': 'quickenrich', 'token': 'bad-key'})
        assert bad.status_code == 422
        good = await clients.post('/connections/token', json={'provider': 'quickenrich', 'token': 'good-key'})
        assert good.status_code == 200, good.text
        tool = next(t for t in (await clients.get('/tools')).json() if t['name'] == 'quickenrich')
        binding = tool['bindings'][0]
        assert binding['location'] == 'header' and binding['name'] == 'Authorization'
        assert binding['format'] == 'Bearer {secret}'


def test_quickenrich_platform_key_configuration(monkeypatch):
    from treg.config import Settings
    monkeypatch.setenv('TREG_PLATFORM_KEY_QUICKENRICH', 'platform-test-key')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'quickenrich')
    settings = Settings(_env_file=None)
    assert settings.platform_key_for('quickenrich') == 'platform-test-key'
    assert P.platform_bindings(P.get('quickenrich')) == [
        {'platform_setting': 'platform_key_quickenrich', 'injector': 'env',
         'location': 'header', 'name': 'Authorization', 'format': 'Bearer {secret}'}]


# ---- ContactOut ----

async def test_contactout_connect_rejects_garbage_and_accepts_zero_pools(clients, monkeypatch):
    def reply(request):
        assert request.url.path == "/v1/stats"
        assert request.headers["token"] in ("garbage", "valid-test")
        if request.headers["token"] == "garbage":
            return httpx.Response(
                401, json={"status_code": 401, "message": "Bad credentials"}
            )
        return httpx.Response(
            200,
            json={
                "status_code": 200,
                "usage": {"quota": 0, "phone_quota": 0, "search_quota": 0},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "contactout", "token": "garbage"}
        )
        assert bad.status_code == 422
        assert not (await clients.get("/tools")).json()
        good = await clients.post(
            "/connections/token", json={"provider": "contactout", "token": "valid-test"}
        )
        assert good.status_code == 200, good.text
        binding = (await clients.get("/tools")).json()[0]["bindings"][0]
        assert binding["name"] == "token" and binding["format"] == "{secret}"


def test_contactout_platform_binding(contactout_platform):
    assert Settings(_env_file=None).platform_key_for("contactout") == "PLATFORM-TEST"
    assert P.platform_bindings(P.get("contactout")) == [
        {
            "platform_setting": "platform_key_contactout",
            "injector": "env",
            "location": "header",
            "name": "token",
            "format": "{secret}",
        }
    ]
    assert "contactout.account.usage" not in catalog_store.load().by_id
