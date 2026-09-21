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
import pytest

from httpx import AsyncClient

from treg import oauth_providers as P


# ---- registry shape ----------------------------------------------------------------------
def test_key_providers_are_offerable_without_deployment_credentials():
    """The user brings the key, so treg holds no app of its own — a key provider must be offerable,
    not shown as 'not configured' the way an unset OAuth provider is."""
    for svc in ("anyapi", "apollo", "pdl", "akta", "hunter", "sumble", "moltsets", "openmart", "harvestapi", "dropleads", "quickenrich", "prospeo", "aiark", "wiza", "limadata", "getleadsio", "scrubby", "zerobounce", "datagma", "contactout", "millionverifier", "bounceban", "trykitt", "crunchbase", "openhandle", "tikhub", "brightdata", "semrush",
                "justoneapi", "dataforseo", "seranking", "moz", "majestic", "serpstat", "exa",
                "cloro",
                "lusha", "coresignal", "diffbot", "thecompaniesapi", "leadmagic", "fiber-ai",
                "companyenrich", "oceanio", "tomba", "predictleads", "findymail", "branddev",
                "icypeas", "leadsforge", "influencersclub", "crustdata", "aviato",
                "spyfu", "apify", "meta-ad-library", "serpapi",
                "coingecko", "polygon", "finnhub", "twelvedata", "fmp", "eodhd", "marketstack",
                "tiingo", "financialdatasets"):
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
    assert listing["financialdatasets"]["category"] == "Market data"
    assert listing["bounceban"]["category"] == "Enrichment"
    assert listing["bounceban"]["auth_kind"] == "key"
    assert listing["zerobounce"]["category"] == "Enrichment"
    assert listing["zerobounce"]["auth_kind"] == "key"
    assert listing["minimax"]["category"] == "AI generation"
    assert listing["minimax"]["summary"] == "Generate voice, images, and videos from text or source images."
    assert listing["openrouter"]["auth_kind"] == "token"
    assert listing["replicate"]["base_url"] == "https://api.replicate.com/v1"
    assert "Enrichment" in P.CATEGORY_ORDER
    assert "Market data" in P.CATEGORY_ORDER


def test_openmart_registry_uses_the_free_balance_probe_and_bearer_key(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_OPENMART", "PLATFORM-OPENMART")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "openmart")
    provider = P.get("openmart")
    settings = Settings(_env_file=None)
    assert provider.base_url == "https://api.openmart.ai"
    assert provider.probe_path == "/api/v2/credit-balance"
    assert provider.probe_method == "GET"
    assert settings.platform_key_for("openmart") == "PLATFORM-OPENMART"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_openmart",
        "injector": "env",
        "location": "header",
        "name": "Authorization",
        "format": "Bearer {secret}",
    }]


async def test_openmart_connect_rejects_bad_key_and_accepts_valid_key(clients, monkeypatch):
    def probe(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/credit-balance"
        if request.headers["authorization"] == "Bearer bad":
            return httpx.Response(401, json={"detail": "Invalid API Key"})
        return httpx.Response(200, json={"balance": 4800})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "openmart", "token": "bad"})
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "openmart", "token": "own-key"})
        assert good.status_code == 200, good.text


def test_zerobounce_registry_uses_internal_usage_probe_and_query_key(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ZEROBOUNCE", "PLATFORM-ZEROBOUNCE")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "zerobounce")
    provider = P.get("zerobounce")
    settings = Settings(_env_file=None)
    assert provider.base_url == "https://api.zerobounce.net"
    assert provider.probe_path.startswith("/v2/getapiusage?")
    assert settings.platform_key_for("zerobounce") == "PLATFORM-ZEROBOUNCE"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_zerobounce",
        "injector": "env",
        "location": "query",
        "name": "api_key",
        "format": "{secret}",
    }]


async def test_zerobounce_connect_rejects_bad_key_and_accepts_valid_key(clients, monkeypatch):
    def probe(request):
        assert request.url.path == "/v2/getapiusage"
        assert request.url.params["start_date"] == "2026-01-01"
        assert request.url.params["end_date"] == "2026-12-31"
        key = request.url.params["api_key"]
        if key == "bad-key":
            return httpx.Response(403, json={"error": "invalid api key"})
        return httpx.Response(200, json={"total": 0, "status_valid": 0})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "zerobounce", "token": "bad-key"})
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "zerobounce", "token": "own-key"})
        assert good.status_code == 200, good.text


def test_dropleads_registry_uses_the_standard_key_provider_paths(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DROPLEADS", "PLATFORM-DROPLEADS")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dropleads")
    settings = Settings(_env_file=None)
    provider = P.get("dropleads")
    assert provider.base_url == "https://prime.dropleads.io"
    assert provider.probe_path == "/api/v2/prime-db/credits/balance"
    assert provider.catalog_targets[0].host == "api.dropleads.io"
    assert provider.extra_tools[0]["suffix"] == "contact"
    assert settings.platform_key_for("dropleads") == "PLATFORM-DROPLEADS"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_dropleads",
        "injector": "env",
        "location": "header",
        "name": "X-API-Key",
        "format": "{secret}",
    }]


async def test_dropleads_connect_provisions_both_approved_hosts(clients, monkeypatch):
    def probe(request):
        assert request.url.host == "prime.dropleads.io"
        assert request.url.path == "/api/v2/prime-db/credits/balance"
        assert request.headers["x-api-key"] in ("bad", "own-key")
        if request.headers["x-api-key"] == "bad":
            return httpx.Response(401, json={"message": "Invalid API key"})
        return httpx.Response(
            200, json={"success": True, "credits": {"totalAvailable": 0}}
        )

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "dropleads", "token": "bad"}
        )
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "dropleads", "token": "own-key"}
        )
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"dropleads", "dropleads-contact"}
    assert tools["dropleads"]["base_url"] == "https://prime.dropleads.io"
    assert tools["dropleads-contact"]["base_url"] == "https://api.dropleads.io"
    assert tools["dropleads"]["bindings"] == tools["dropleads-contact"]["bindings"]


def test_prospeo_registry_uses_account_information_without_exposing_it(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_PROSPEO", "PLATFORM-PROSPEO")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "prospeo")
    settings = Settings(_env_file=None)
    provider = P.get("prospeo")
    assert provider.base_url == "https://api.prospeo.io"
    assert provider.probe_path == "/account-information"
    assert provider.probe_method == "GET"
    assert provider.token_header == "X-KEY"
    assert settings.platform_key_for("prospeo") == "PLATFORM-PROSPEO"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_prospeo",
        "injector": "env",
        "location": "header",
        "name": "X-KEY",
        "format": "{secret}",
    }]


async def test_prospeo_connect_provisions_a_single_catalog_host(clients, monkeypatch):
    def probe(request):
        assert request.url.host == "api.prospeo.io"
        assert request.url.path == "/account-information"
        key = request.headers["x-key"]
        if key == "bad":
            return httpx.Response(400, json={"error": True, "error_code": "INVALID_API_KEY"})
        return httpx.Response(200, json={
            "error": False,
            "response": {"current_plan": "STARTER", "remaining_credits": 10},
        })

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "prospeo", "token": "bad"}
        )
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "prospeo", "token": "own-key"}
        )
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"prospeo"}
    assert tools["prospeo"]["base_url"] == "https://api.prospeo.io"


def test_aiark_registry_uses_credits_probe_and_x_token(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_AIARK", "PLATFORM-AIARK")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "aiark")
    settings = Settings(_env_file=None)
    provider = P.get("aiark")
    assert provider.base_url == "https://api.ai-ark.com/api/developer-portal"
    assert provider.probe_path == "/v1/payments/credits"
    assert provider.probe_method == "GET"
    assert settings.platform_key_for("aiark") == "PLATFORM-AIARK"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_aiark",
        "injector": "env",
        "location": "header",
        "name": "X-TOKEN",
        "format": "{secret}",
    }]


async def test_aiark_connect_rejects_a_bad_key_and_provisions_the_catalog_host(
        clients, monkeypatch):
    def probe(request):
        assert request.url.path == "/api/developer-portal/v1/payments/credits"
        key = request.headers["x-token"]
        if key == "bad":
            return httpx.Response(401, json={"error": "Unauthorized"})
        return httpx.Response(200, json={"total": 15000})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "aiark", "token": "bad"}
        )
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "aiark", "token": "own-key"}
        )
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"aiark"}
    assert tools["aiark"]["base_url"] == "https://api.ai-ark.com/api/developer-portal"


def test_limadata_registry_uses_the_free_validation_probe_and_x_api_key(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_LIMADATA", "PLATFORM-LIMADATA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "limadata")
    settings = Settings(_env_file=None)
    provider = P.get("limadata")
    assert provider.base_url == "https://api.limadata.com"
    assert provider.probe_path == "/api/v1/search/web"
    assert provider.probe_method == "POST"
    assert provider.probe_json == {}
    assert provider.probe_reject_statuses == (401, 403)
    assert settings.platform_key_for("limadata") == "PLATFORM-LIMADATA"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_limadata",
        "injector": "env",
        "location": "header",
        "name": "x-api-key",
        "format": "{secret}",
    }]


async def test_limadata_connect_rejects_bad_key_and_accepts_validation_error(
        clients, monkeypatch):
    def probe(request):
        assert request.url == "https://api.limadata.com/api/v1/search/web"
        assert request.content == b"{}"
        if request.headers["x-api-key"] == "bad":
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(400, json={"message": "query is required"})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "limadata", "token": "bad"}
        )
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "limadata", "token": "own-key"}
        )
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"limadata"}
    assert tools["limadata"]["base_url"] == "https://api.limadata.com"


def test_getleadsio_registry_uses_bearer_and_the_free_usage_probe(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_GETLEADSIO", "PLATFORM-GETLEADSIO")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "getleadsio")
    settings = Settings(_env_file=None)
    provider = P.get("getleadsio")
    assert provider.base_url == "https://app.getleads.io"
    assert provider.probe_path == "/api/v1/usage/fair-use"
    assert provider.probe_method == "GET"
    assert provider.token_header == "Authorization"
    assert provider.token_format == "Bearer {secret}"
    assert settings.platform_key_for("getleadsio") == "PLATFORM-GETLEADSIO"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_getleadsio",
        "injector": "env",
        "location": "header",
        "name": "Authorization",
        "format": "Bearer {secret}",
    }]


async def test_getleadsio_connect_rejects_a_bad_key_and_provisions_the_catalog_host(
        clients, monkeypatch):
    def probe(request):
        assert request.url.host == "app.getleads.io"
        assert request.url.path == "/api/v1/usage/fair-use"
        key = request.headers["authorization"]
        if key == "Bearer bad":
            return httpx.Response(401, json={"ok": False, "message": "Invalid API key"})
        return httpx.Response(200, json={"ok": True, "credits_remaining": 997})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "getleadsio", "token": "bad"}
        )
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "getleadsio", "token": "own-key"}
        )
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"getleadsio"}
    assert tools["getleadsio"]["base_url"] == "https://app.getleads.io"
    binding = tools["getleadsio"]["bindings"][0]
    assert binding["name"] == "Authorization"
    assert binding["format"] == "Bearer {secret}"


def test_aigc_token_providers_are_offerable_without_deployment_credentials():
    for service in ("minimax", "openrouter", "replicate", "reapi"):
        provider = P.get(service)
        assert provider is not None
        assert provider.auth_kind == "token"
        assert provider.uses_pasted_secret is True
        assert P.is_configured(provider) is True
    assert P.get("minimax").probe_method == "POST"
    assert P.get("minimax").probe_json == {}
    assert P.get("minimax").probe_reject_statuses == (401, 403)
    # reAPI has no free account route: an unknown task id is 404 on a valid key, 401 on a bad one.
    assert P.get("reapi").probe_path == "/tasks/probe"
    assert P.get("reapi").probe_reject_statuses == (401, 403)
    assert P.get("piapi").auth_kind == "key" and P.get("piapi").token_header == "X-API-Key"


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


async def test_secret_add_raw_basic_credential_encodes_at_injection(clients: AsyncClient, monkeypatch):
    """Secrets added via `treg secret add dataforseo` bypass the connect flow and store the raw value.
    The injector must detect and Base64-encode a raw `login:password` to produce a valid Basic header.

    Regression test for feedback #294 / #276 / #280-282: DataForSEO Lighthouse returned 40100
    (401 Unauthorized) through treg while the same credential worked via direct curl. The cause
    was that `treg secret add dataforseo --env-var` stored raw `login:password`, but the injector
    used it verbatim as `Basic login:password` instead of `Basic <base64(login:password)>`.
    """
    import base64
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    # Add a raw secret directly, bypassing the /connections/token flow that would Base64-encode it
    r = await clients.post("/secrets", json={"name": "dataforseo", "value": "login:pw"})
    assert r.status_code == 200, r.text
    # Call a DataForSEO catalog endpoint to trigger marketplace resolution with the named secret
    # (the named-tool path won't find a tool; this exercises _marketplace_secret -> _provider_bindings)
    echoed = (await clients.get("/call/dataforseo.account.usage")).json()["auth"]
    expected = "Basic " + base64.b64encode(b"login:pw").decode()
    assert echoed == expected, f"raw secret must be Base64-encoded at injection time, got {echoed!r}"


async def test_secret_add_already_encoded_basic_credential_not_double_encoded(clients: AsyncClient, monkeypatch):
    """Secrets added with an already-encoded Base64 blob must not be double-encoded by the injector."""
    import base64
    blob = base64.b64encode(b"login:pw").decode()
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    # Add an already-encoded secret directly
    r = await clients.post("/secrets", json={"name": "dataforseo", "value": blob})
    assert r.status_code == 200, r.text
    # Call a DataForSEO catalog endpoint to trigger marketplace resolution with the named secret
    echoed = (await clients.get("/call/dataforseo.account.usage")).json()["auth"]
    assert echoed == "Basic " + blob, f"already-encoded secret must not be double-encoded, got {echoed!r}"


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


def test_bounceban_registry_and_platform_key_configuration(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_BOUNCEBAN", "platform-test-key")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "bounceban")
    settings = Settings(_env_file=None)
    provider = P.get("bounceban")
    assert provider.base_url == "https://api.bounceban.com"
    assert provider.probe_path == "/v1/account"
    assert provider.token_header == "Authorization"
    assert provider.token_format == "{secret}"
    assert provider.catalog_targets[0].host == "api-waterfall.bounceban.com"
    assert settings.platform_key_for("bounceban") == "platform-test-key"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_bounceban",
        "injector": "env",
        "location": "header",
        "name": "Authorization",
        "format": "{secret}",
    }]


async def test_bounceban_connect_rejects_bad_key_and_provisions_both_hosts(clients, monkeypatch):
    def probe(request):
        assert request.url.host == "api.bounceban.com"
        assert request.url.path == "/v1/account"
        key = request.headers["authorization"]
        if key == "bad-key":
            return httpx.Response(401, json={"msg": "Invalid API key"})
        return httpx.Response(200, json={"available_credits": 0, "rate_limit": []})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "bounceban", "token": "bad-key"})
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "bounceban", "token": "own-key"})
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"bounceban", "bounceban-waterfall"}
    assert tools["bounceban"]["base_url"] == "https://api.bounceban.com"
    assert tools["bounceban-waterfall"]["base_url"] == "https://api-waterfall.bounceban.com"
    assert tools["bounceban"]["bindings"] == tools["bounceban-waterfall"]["bindings"]


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


@pytest.mark.parametrize("status", [200, 402])
async def test_financialdatasets_connect_accepts_only_valid_key_outcomes_without_health_probe(
    clients, monkeypatch, status,
):
    """200 and 402 prove the key reached the prepaid account.

    The absolute connect-only probe must also stay out of the saved Tool health metadata: replaying
    a paid data request from recurring health would consume Credits.
    """
    def probe(request):
        assert request.url.path == "/prices/snapshot"
        assert request.url.params["ticker"] == "AAPL"
        assert request.headers["X-API-KEY"] == "valid-empty-key"
        return httpx.Response(status, json={"detail": "Insufficient credits"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token",
            json={"provider": "financialdatasets", "token": "valid-empty-key"},
        )
        assert response.status_code == 200, response.text
        tool = next(t for t in (await clients.get("/tools")).json()
                    if t["name"] == "financialdatasets")
        assert tool["health_check"] is None
        assert tool["bindings"][0]["name"] == "X-API-KEY"


@pytest.mark.parametrize("status", [201, 301, 400, 401, 403, 404, 409, 422, 429, 500, 503])
async def test_financialdatasets_connect_rejects_any_other_status(clients, monkeypatch, status):
    def probe(request):
        assert request.headers["X-API-KEY"] == "invalid-key"
        return httpx.Response(status, json={"detail": "Invalid API key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token",
            json={"provider": "financialdatasets", "token": "invalid-key"},
        )
        assert response.status_code == 422, response.text
        assert not [t for t in (await clients.get("/tools")).json()
                    if t["name"] == "financialdatasets"]


def test_financialdatasets_registry_and_platform_key_configuration(monkeypatch):
    provider = P.get("financialdatasets")
    assert provider.base_url == "https://api.financialdatasets.ai"
    assert provider.token_header == "X-API-KEY"
    assert provider.probe_url == (
        "https://api.financialdatasets.ai/prices/snapshot?ticker=AAPL"
    )
    assert provider.probe_path == ""
    assert 200 not in provider.probe_reject_statuses
    assert 402 not in provider.probe_reject_statuses
    assert all(
        status in provider.probe_reject_statuses
        for status in (201, 301, 400, 401, 403, 404, 409, 422, 429, 500, 503)
    )

    monkeypatch.setenv("TREG_PLATFORM_KEY_FINANCIALDATASETS", "platform-test-key")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "financialdatasets")
    settings = Settings(_env_file=None)
    assert settings.platform_key_for("financialdatasets") == "platform-test-key"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_financialdatasets",
        "injector": "env",
        "location": "header",
        "name": "X-API-KEY",
        "format": "{secret}",
    }]
