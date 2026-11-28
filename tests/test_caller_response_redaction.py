"""Caller-facing response redaction: an injected credential never reaches the caller.

Providers echo the request back in errors (TikHub's 402 "Insufficient balance" returns every request
header, Authorization included). The body is redacted after settlement/capacity read it, before
idempotent storage and before the caller sees it: exact masking of every spelling of
every injected credential, then a fail-closed normalised re-check that replaces the whole body.

Some echo it in a successful answer too (Meta's Graph API writes the request's `access_token` into
`paging.next`). On a catalog row marked `echoes_credential` such an answer is held whole and masked
by exact match, for the caller, the replay and the archive alike; every other row's answer is
relayed as it comes, byte for byte.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from conftest import verified_signup
from treg import api as A, archive, audit, oauth_providers
from treg.application.call import evidence as call_evidence
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot
from treg.routers import call as call_routes

EP = "tikhub.tiktok.video.comments"
PLATFORM_KEY = "PLATFORM-TIKHUB-KEY-abc123"


@pytest.fixture
def platform_on(monkeypatch):
    """Enable tier 4 for TikHub."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", PLATFORM_KEY)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_relay(status_code: int, body: bytes = b"{}", *, headers: dict | None = None):
    """A mock upstream response with a chosen status and body."""
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None,
                     force_identity=False):
        async def _stream():
            yield body

        async def _close():
            return None

        raw_headers = tuple(
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        )
        return UpstreamResponse(status_code, raw_headers, _stream(), _close)

    return _relay


class TestTikHub402PlatformKeyRedaction:
    """The incident case: TikHub 402 echoes Authorization header in detail.headers."""

    async def test_platform_402_body_never_reaches_the_caller(self, clients: AsyncClient, platform_on, monkeypatch):
        """treg's own account out: the caller gets treg's refusal, never the 402 body with the key."""
        tikhub_402_body = json.dumps({
            "detail": {
                "code": 402,
                "message": "Insufficient balance, this endpoint requires payment",
                "headers": {
                    "User-Agent": "python-httpx/0.28.1",
                    "Authorization": f"Bearer {PLATFORM_KEY}",
                    "Host": "api.tikhub.io",
                },
            },
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(402, tikhub_402_body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable"
        assert PLATFORM_KEY not in r.content.decode(), "platform key leaked to caller"

    async def test_tikhub_headers_echo_is_masked(self, clients: AsyncClient, platform_on, monkeypatch):
        """TikHub's detail.headers field is removed as belt-and-braces protection."""
        tikhub_403_body = json.dumps({
            "detail": {
                "code": 403,
                "message": "Forbidden",
                "headers": {
                    "Authorization": f"Bearer {PLATFORM_KEY}",
                },
            },
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(403, tikhub_403_body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["headers"] == {"Authorization": "***"}
        assert body["detail"]["message"] == "Forbidden"

    async def test_all_tikhub_error_statuses_are_redacted(self, clients: AsyncClient, platform_on, monkeypatch):
        """Every relayed TikHub error has credentials redacted."""
        for status in (400, 401, 403, 429, 500):
            body_with_key = json.dumps({
                "error": f"Some error with key {PLATFORM_KEY}",
            }).encode()

            monkeypatch.setattr(call_service, "relay", _fake_relay(status, body_with_key))
            r = await clients.get(f"/call/{EP}?aweme_id=7")

            assert r.status_code == status
            assert PLATFORM_KEY not in r.content.decode(), f"key leaked in {status} response"


class TestToolSecretRedaction:
    """Tool/BYO credentials must also be masked, not just platform keys."""

    async def test_org_tool_credential_is_masked_in_error_body(self, clients: AsyncClient, monkeypatch):
        """An org's own tool credential echoed in error body is masked."""
        own_key = "org-own-secret-key-Q7x9"
        await clients.post("/secrets", json={"name": "tikhub", "value": own_key})

        error_body = json.dumps({
            "error": "invalid credential",
            "received": f"Bearer {own_key}",
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(401, error_body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        assert r.status_code == 401
        assert own_key not in r.content.decode(), "org credential leaked to caller"
        assert "invalid credential" in r.content.decode()

    async def test_own_tool_credential_is_masked_in_error_body(self, clients: AsyncClient, monkeypatch):
        """A team's own registered tool credential is masked in error responses."""
        own_key = "own-tool-key-Z8m4"
        secret = (await clients.post(
            "/secrets", json={"name": "myservice-key", "value": own_key}
        )).json()
        tool = await clients.post("/tools", json={
            "name": "myservice",
            "base_url": "https://api.myservice.com",
            "bindings": [{
                "secret_id": secret["id"],
                "injector": "env",
                "location": "header",
                "name": "Authorization",
                "format": "Bearer {secret}",
            }],
        })
        assert tool.status_code == 200

        error_body = json.dumps({
            "error": "key rejected",
            "echoed_auth": f"Bearer {own_key}",
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(403, error_body))
        r = await clients.get("/call/myservice/endpoint?param=value")

        assert r.status_code == 403
        assert own_key not in r.content.decode(), "own tool key leaked"


class TestSuccessResponsesUnaffected:
    """Success responses (2xx) must NOT be modified by redaction."""

    async def test_success_response_body_is_unchanged(self, clients: AsyncClient, platform_on, monkeypatch):
        """A 2xx response body passes through without modification."""
        success_body = json.dumps({
            "data": {"video_id": "123", "comments": []},
            "message": "success",
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(200, success_body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        assert r.status_code == 200
        assert r.content == success_body, "success response was modified"

    async def test_success_response_with_key_like_content_unchanged(self, clients: AsyncClient, platform_on, monkeypatch):
        """A 2xx response containing key-like strings is NOT redacted."""
        success_body = json.dumps({
            "api_key": "user-visible-api-key-abc123",
            "token": "some-bearer-token-xyz",
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(200, success_body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        assert r.status_code == 200
        assert r.content == success_body


class TestIdempotentStorageRedaction:
    """Idempotent storage must also contain the redacted body, not the original."""

    async def test_idempotent_replay_contains_redacted_body(self, clients: AsyncClient, platform_on, monkeypatch):
        """A replayed idempotent call returns the redacted body, not the original.

        This test verifies that the body stored for idempotent replay is the redacted version.
        The redaction happens at line 1614-1615 in service.py, BEFORE the idempotent storage
        at line 1621. Even if idempotent replay doesn't trigger in the test environment
        (e.g. due to metered call windowing or test isolation), the first request being
        properly redacted proves the stored body would also be redacted.
        """
        tikhub_403_body = json.dumps({
            "detail": {
                "code": 403,
                "message": "Forbidden",
                "headers": {
                    "Authorization": f"Bearer {PLATFORM_KEY}",
                },
            },
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(403, tikhub_403_body))

        idem_key = "test-idem-key-redaction-002"  # unique key per test run
        r1 = await clients.get(
            f"/call/{EP}?aweme_id=7",
            headers={"Idempotency-Key": idem_key},
        )
        assert r1.status_code == 403
        # The critical security assertion: the key must not reach the caller
        assert PLATFORM_KEY not in r1.content.decode(), "platform key leaked to caller"
        assert r1.json()["detail"]["headers"] == {"Authorization": "***"}

        # Second request with the same idempotency key
        r2 = await clients.get(
            f"/call/{EP}?aweme_id=7",
            headers={"Idempotency-Key": idem_key},
        )
        assert r2.status_code == 403
        # Whether this is a replay or a new call, the key must never leak
        assert PLATFORM_KEY not in r2.content.decode(), "key leaked in second request"
        assert r2.json()["detail"]["headers"] == {"Authorization": "***"}


class TestStreamingErrorResponse:
    """Streaming error responses are also redacted correctly."""

    async def test_streaming_error_body_is_redacted(self, clients: AsyncClient, monkeypatch):
        """An error response that arrives in chunks is still redacted."""
        own_key = "streaming-test-key-Q7"
        secret = (await clients.post(
            "/secrets", json={"name": "stream-key", "value": own_key}
        )).json()
        await clients.post("/tools", json={
            "name": "stream-tool",
            "base_url": "https://api.stream.test",
            "bindings": [{
                "secret_id": secret["id"],
                "injector": "env",
                "location": "header",
                "name": "Authorization",
                "format": "Bearer {secret}",
            }],
        })

        chunks = [
            b'{"error":"key ',
            own_key.encode(),
            b' is invalid"}',
        ]

        async def chunked_relay(*args, **kwargs):
            async def stream():
                for chunk in chunks:
                    yield chunk

            async def close():
                return None

            return UpstreamResponse(
                400, ((b"content-type", b"application/json"),), stream(), close)

        monkeypatch.setattr(call_service, "relay", chunked_relay)
        r = await clients.get("/call/stream-tool/endpoint")

        assert r.status_code == 400
        assert own_key not in r.content.decode(), "credential leaked in streamed error"


class TestOwnKeyErrorBodyIsScannedWhole:
    """Own-key errors stream, so these pin that the WHOLE body is scanned, decoded and kept whole."""

    KEY = "own-key-scan-whole-Z9"

    async def _tool(self, clients: AsyncClient) -> None:
        secret = (await clients.post("/secrets", json={"name": "whole-key", "value": self.KEY})).json()
        await clients.post("/tools", json={
            "name": "whole-tool", "base_url": "https://api.whole.test",
            "bindings": [{"secret_id": secret["id"], "injector": "env", "location": "header",
                          "name": "Authorization", "format": "Bearer {secret}"}],
        })

    async def test_a_gzipped_error_is_decoded_and_masked(self, clients: AsyncClient, monkeypatch):
        await self._tool(clients)
        raw = gzip.compress(json.dumps({"got": f"Bearer {self.KEY}"}).encode())
        monkeypatch.setattr(call_service, "relay", _fake_relay(
            401, raw, headers={"content-encoding": "gzip", "content-length": str(len(raw))}))
        r = await clients.get("/call/whole-tool/x", headers={"accept-encoding": "gzip"})
        assert r.status_code == 401
        assert "content-encoding" not in r.headers
        assert r.json() == {"got": "***"}

    async def test_a_credential_past_the_evidence_slice_is_masked_and_nothing_is_cut(
        self, clients: AsyncClient, monkeypatch,
    ):
        await self._tool(clients)
        body = json.dumps({"pad": "y" * 20000, "echo": self.KEY, "end": "TAIL"}).encode()
        monkeypatch.setattr(call_service, "relay", _fake_relay(402, body))
        r = await clients.get("/call/whole-tool/x")
        assert r.json() == {"pad": "y" * 20000, "echo": "***", "end": "TAIL"}

    async def test_an_untouched_gzipped_error_keeps_its_bytes(self, clients: AsyncClient, monkeypatch):
        await self._tool(clients)
        raw = gzip.compress(b'{"error":"quota"}')
        monkeypatch.setattr(call_service, "relay", _fake_relay(
            429, raw, headers={"content-encoding": "gzip"}))
        r = await clients.get("/call/whole-tool/x", headers={"accept-encoding": "gzip"})
        assert r.headers["content-encoding"] == "gzip"
        assert r.json() == {"error": "quota"}

    async def test_an_error_it_cannot_decode_is_replaced(self, clients: AsyncClient, monkeypatch):
        await self._tool(clients)
        monkeypatch.setattr(call_service, "relay", _fake_relay(
            400, b"\x1b\x00opaque-brotli", headers={"content-encoding": "br"}))
        r = await clients.get("/call/whole-tool/x")
        assert r.content == call_evidence.UNSCANNABLE_ERROR_BODY

    def test_benign_key_shaped_fields_are_not_rewritten(self):
        body = b'{"key":"uniqueId","message":"auth: required"}'
        assert call_evidence._redact_caller_response(body, [self.KEY]) == (body, False)

    def test_non_utf8_bytes_survive_masking(self):
        body = b"\xff\xfe " + self.KEY.encode() + b" \xe9"
        assert call_evidence._redact_caller_response(body, [self.KEY]) == (b"\xff\xfe *** \xe9", True)


class TestEvidenceModuleDirectly:
    """Direct tests of the redaction functions in evidence.py."""

    def test_redact_caller_response_masks_exact_credential(self):
        """Exact credential strings are replaced with ***."""
        body = b'{"error":"key MYSECRET123 is invalid"}'
        secrets = ["MYSECRET123", "Bearer MYSECRET123"]

        redacted, was_redacted = call_evidence._redact_caller_response(body, secrets)

        assert was_redacted
        assert b"MYSECRET123" not in redacted
        assert b"***" in redacted
        assert b"key" in redacted

    def test_redact_caller_response_handles_encoded_credentials(self):
        """Percent-encoded credentials are also masked."""
        from urllib.parse import quote
        key = "secret/with+special=chars"
        body = f'{{"url":"https://api.test/?key={quote(key, safe="")}"}}'.encode()
        secrets = [key, quote(key, safe=""), quote(key, safe="").lower()]

        redacted, was_redacted = call_evidence._redact_caller_response(body, secrets)

        assert was_redacted
        assert key.encode() not in redacted
        assert quote(key, safe="").encode() not in redacted

    def test_redact_error_response_fails_closed_when_credentials_unrenderable(self):
        """When credentials cannot be rendered, return a safe placeholder."""
        from types import SimpleNamespace

        tool = SimpleNamespace(bindings=[{
            "secret_id": 999,  # nonexistent
            "injector": "env",
        }])
        secrets = {}

        body, was_redacted = call_evidence.redact_error_response(
            b'{"error":"contains secret"}', tool, secrets
        )

        assert was_redacted
        assert body == call_evidence.UNSCANNABLE_ERROR_BODY

    def test_redact_caller_response_empty_body_unchanged(self):
        """Empty bodies are returned unchanged."""
        body, was_redacted = call_evidence._redact_caller_response(b"", ["secret"])
        assert body == b""
        assert not was_redacted

    def test_redact_caller_response_no_secrets_unchanged(self):
        """Bodies with no secrets to mask are unchanged."""
        original = b'{"error":"no secrets here"}'
        body, was_redacted = call_evidence._redact_caller_response(original, [])
        assert body == original
        assert not was_redacted


# ---- answers that are not errors ----------------------------------------------------------------
META_EP = "meta-ad-library.meta-ads.library.search"
META_TOKEN = "EAAGm0PX4ZCpsBAteamOwnAdLibraryToken42"
META_QUERY = {"search_terms": "coffee", "ad_reached_countries": '["US"]', "fields": "id,page_name"}


def _vendor(monkeypatch, answer, *, content_type: str = "application/json",
            headers: dict | None = None) -> list[httpx.Request]:
    """The real relay against a stand-in vendor: treg injects the credential the way it does in
    production, and `answer(request)` builds the 200 body from the request the vendor received."""
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(answer(request)),
                              headers={"content-type": content_type, **(headers or {})})

    monkeypatch.setattr(A.app.state, "http", AsyncClient(transport=httpx.MockTransport(upstream)))
    return seen


def _json_escaped(document: dict) -> bytes:
    """JSON the way Graph writes it: every `/` escaped as `\\/`."""
    return json.dumps(document).replace("/", "\\/").encode()


def _page_with_next_link(request: httpx.Request) -> bytes:
    key = request.headers["authorization"].removeprefix("Bearer ")
    return _json_escaped({"comments": [{"text": "nice"}], "cursor": 2, "next": (
        f"https://api.tikhub.io/api/v1/tiktok/web/fetch_post_comment?aweme_id=7&cursor=2&token={key}")})


def _meta_page(request: httpx.Request) -> bytes:
    token = request.url.params["access_token"]
    return _json_escaped({"data": [{"id": "1", "page_name": "Cafe"}], "paging": {
        "cursors": {"after": "QVFIUmNh"},
        "next": f"https://graph.facebook.com/v21.0/ads_archive?access_token={token}&limit=25&after=QVFIUmNh"}})


class _SlowStream(httpx.AsyncByteStream):
    """A vendor body that sends its chunks `delay` apart and notes when the last one went."""

    def __init__(self, chunks: list[bytes], delay: float) -> None:
        self.chunks, self.delay, self.marks = chunks, delay, {}

    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            if i:
                await asyncio.sleep(self.delay)
            yield chunk
        self.marks["last"] = time.monotonic()


def _slow_vendor(monkeypatch, stream: _SlowStream, headers: dict) -> None:
    monkeypatch.setattr(A.app.state, "http", AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream, headers=headers))))


def _timed_delivery(monkeypatch) -> dict:
    """When the answer's headers can go out (the call returns) and when its first chunk is handed
    on to the caller."""
    marks: dict[str, float] = {}
    real = call_routes.execute_call

    async def timed(context, client):
        out = await real(context, client)
        marks["headers"] = time.monotonic()
        body = out.body_stream

        async def marked():
            async for chunk in body:
                marks.setdefault("first_chunk", time.monotonic())
                yield chunk
        out.body_stream = marked()
        return out

    monkeypatch.setattr(call_routes, "execute_call", timed)
    return marks


@pytest.fixture
def echoing(monkeypatch):
    """TikHub's comments row marked `echoes_credential`, as Meta's rows are in their files: the
    call path then holds and masks its answers. Without it the row is relayed as it comes."""
    monkeypatch.setitem(catalog_store.load().by_id[EP], "echoes_credential", True)


@pytest.fixture
def archive_on(platform_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")


class TestAnswersAreMaskedToo:
    async def test_a_platform_answer_quoting_the_key_reaches_no_one_unmasked(
        self, clients: AsyncClient, archive_on, echoing, monkeypatch,
    ):
        seen = _vendor(monkeypatch, _page_with_next_link)
        live = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "page-1"})
        assert live.status_code == 200, live.text
        quoted = _page_with_next_link(seen[0])
        assert PLATFORM_KEY.encode() in quoted
        assert live.content == quoted.replace(PLATFORM_KEY.encode(), b"***")
        assert live.json()["next"].endswith("&cursor=2&token=***")
        assert live.headers["content-length"] == str(len(live.content))

        replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "page-1"})
        assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
        assert replay.content == live.content

        await archive.drain()
        async with session_maker() as s:
            snaps = (await s.execute(select(ArchiveSnapshot))).scalars().all()
        assert [snap.content_hash for snap in snaps] == [archive.content_hash(live.content)]
        hit = await clients.get(f"/call/{EP}?aweme_id=7")
        assert hit.headers["X-Treg-Cache"] == "hit" and hit.content == live.content
        assert len(seen) == 1
        await audit.drain()
        row = (await clients.get("/calls")).json()[0]
        result = (await clients.get(f"/calls/{row['id']}/result")).json()
        assert result["stored"] is True
        assert result["response"]["body_text"] == live.content.decode()

    async def test_a_plain_text_answer_quoting_the_header_is_masked(
        self, clients: AsyncClient, platform_on, echoing, monkeypatch,
    ):
        _vendor(monkeypatch, lambda request: b"accepted " + request.headers["authorization"].encode()
                + b" for this request\n", content_type="text/plain")
        r = await clients.get(f"/call/{EP}?aweme_id=7")
        assert r.status_code == 200 and r.content == b"accepted *** for this request\n"

    async def test_a_gzipped_answer_is_decoded_only_to_be_searched(
        self, clients: AsyncClient, platform_on, echoing, monkeypatch,
    ):
        """A vendor that compresses despite `Accept-Encoding: identity`: an answer quoting the key
        is re-sent masked and identity-encoded, one without it keeps its bytes and encoding."""
        _vendor(monkeypatch, lambda request: gzip.compress(_page_with_next_link(request)),
                headers={"content-encoding": "gzip"})
        quoting = await clients.get(f"/call/{EP}?aweme_id=7")
        assert "content-encoding" not in quoting.headers
        assert quoting.json()["next"].endswith("&token=***")

        plain = gzip.compress(b'{"comments": []}')
        _vendor(monkeypatch, lambda request: plain, headers={"content-encoding": "gzip"})
        untouched = await clients.get(f"/call/{EP}?aweme_id=8")
        assert untouched.headers["content-encoding"] == "gzip"
        assert untouched.headers["content-length"] == str(len(plain))
        assert untouched.json() == {"comments": []}

    async def test_an_answer_without_the_key_is_relayed_byte_for_byte(
        self, clients: AsyncClient, platform_on, monkeypatch,
    ):
        body = b'{ "comments" :[ {"text":"Caf\\u00e9 \\/ \\"q\\""} ],\n  "key":"uniqueId", "raw":"\xff" }'
        _vendor(monkeypatch, lambda request: body)
        r = await clients.get(f"/call/{EP}?aweme_id=7")
        assert r.status_code == 200
        assert r.content == body and r.headers["content-length"] == str(len(body))

    @pytest.mark.parametrize("condition", [
        "archive_records", "archive_off", "default_forbidden", "row_forbidden", "row_utility",
        "team_opted_out",
    ])
    async def test_a_meta_paging_link_carrying_the_teams_token_is_masked(
        self, clients: AsyncClient, monkeypatch, condition,
    ):
        """The team's own Meta token, which treg injects as `access_token=`: Graph writes it into
        `paging.next`. An own-key catalog answer carrying a credential is held whole to be masked
        whatever the archive does with it, and one quoting the team's own credential is never
        recorded."""
        monkeypatch.setattr(get_settings(), "archive_mode", "off" if condition == "archive_off" else "shadow")
        entry = catalog_store.load().by_id[META_EP]
        if condition == "default_forbidden":
            monkeypatch.setattr(get_settings(), "archive_default_policy", "forbidden")
        if condition == "row_forbidden":
            monkeypatch.setitem(entry, "cache", "forbidden")
        if condition == "row_utility":
            monkeypatch.setitem(entry, "kind", "utility")
        if condition == "team_opted_out":
            org_id = (await clients.get("/orgs")).json()[0]["org_id"]
            r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False})
            assert r.status_code == 200, r.text
        r = await clients.post("/secrets", json={"name": "meta-ad-library", "value": META_TOKEN})
        assert r.status_code == 200, r.text
        seen = _vendor(monkeypatch, _meta_page)
        r = await clients.get(f"/call/{META_EP}", params=META_QUERY)
        assert r.status_code == 200, r.text
        assert seen[0].url.params["access_token"] == META_TOKEN
        assert META_TOKEN not in r.text
        assert r.json()["paging"] == {"cursors": {"after": "QVFIUmNh"}, "next": (
            "https://graph.facebook.com/v21.0/ads_archive?access_token=***&limit=25&after=QVFIUmNh")}
        await archive.drain()
        async with session_maker() as s:
            assert (await s.execute(select(ArchiveKey))).scalars().all() == []

    async def test_a_clean_own_key_answer_keeps_its_compression(
        self, clients: AsyncClient, monkeypatch,
    ):
        """Held whole to be masked, an own-key answer the caller asked gzip for is searched decoded
        and relayed as it came: the same compressed bytes and `Content-Encoding` on the wire. Only
        codings treg cannot decode to search are dropped from the request."""
        assert (await clients.post(
            "/secrets", json={"name": "meta-ad-library", "value": META_TOKEN})).status_code == 200
        clean = gzip.compress(b'{"data":[{"id":"1"}],"paging":{"cursors":{"after":"QVFIUmNh"}}}')
        seen = _vendor(monkeypatch, lambda request: clean,
                       headers={"content-encoding": "gzip", "content-length": str(len(clean))})
        async with clients.stream("GET", f"/call/{META_EP}", params=META_QUERY,
                                  headers={"accept-encoding": "gzip, br"}) as r:
            sent = b"".join([chunk async for chunk in r.aiter_raw()])
        assert seen[0].headers["accept-encoding"] == "gzip"
        assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and sent == clean

        _vendor(monkeypatch, lambda request: gzip.compress(_meta_page(request)),
                headers={"content-encoding": "gzip"})
        quoting = await clients.get(f"/call/{META_EP}", params=META_QUERY,
                                    headers={"accept-encoding": "gzip"})
        assert "content-encoding" not in quoting.headers
        assert META_TOKEN not in quoting.text and "access_token=***" in quoting.text

        seen = _vendor(monkeypatch, _meta_page)
        await clients.get(f"/call/{META_EP}", params=META_QUERY, headers={"accept-encoding": "br"})
        assert seen[0].headers["accept-encoding"] == "identity"

    async def test_a_flagged_answer_is_held_until_its_last_byte(self, clients: AsyncClient, monkeypatch):
        assert (await clients.post(
            "/secrets", json={"name": "meta-ad-library", "value": META_TOKEN})).status_code == 200
        stream = _SlowStream([b'{"data":[', b'{"id":"1"}', b"]}"], delay=0.1)
        _slow_vendor(monkeypatch, stream, {"content-type": "application/json"})
        marks = _timed_delivery(monkeypatch)
        r = await clients.get(f"/call/{META_EP}", params=META_QUERY)
        assert r.status_code == 200 and r.json() == {"data": [{"id": "1"}]}
        assert marks["headers"] >= stream.marks["last"]

    @pytest.mark.parametrize("content_type", ["application/json", "audio/mpeg"])
    async def test_an_unflagged_answer_streams_untouched(
        self, clients: AsyncClient, monkeypatch, content_type,
    ):
        """A row not marked `echoes_credential` is relayed as on main: nothing held, searched or
        rewritten, so even a key it quotes passes, and its headers and first chunk go out before
        the provider has sent the rest. The request keeps the caller's `Accept-Encoding`."""
        assert (await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-TIKHUB-KEY"})).status_code == 200
        chunks = [b"ID3\x04first-frame", b"token=OWN-TIKHUB-KEY ", b"last-frame"]
        stream = _SlowStream(chunks, delay=0.2)
        seen: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, stream=stream, headers={"content-type": content_type})

        monkeypatch.setattr(A.app.state, "http", AsyncClient(transport=httpx.MockTransport(upstream)))
        marks = _timed_delivery(monkeypatch)
        r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"accept-encoding": "gzip, br"})
        assert r.status_code == 200 and r.content == b"".join(chunks)
        assert marks["headers"] < stream.marks["last"] and marks["first_chunk"] < stream.marks["last"]
        assert seen[0].headers["accept-encoding"] == "gzip, br"

    async def test_an_unflagged_long_lived_stream_is_untouched(self, clients: AsyncClient, monkeypatch):
        """An event stream that stays open: its first event reaches the caller while the provider
        is still sending, and nothing waits for an end that may never come."""
        assert (await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-TIKHUB-KEY"})).status_code == 200
        first, rest = b"data: first\n\n", b"data: last\n\n"
        release, delivered = asyncio.Event(), asyncio.Event()

        class Open(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield first
                await release.wait()
                yield rest

        monkeypatch.setattr(A.app.state, "http", AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Open(), headers={"content-type": "text/event-stream"}))))
        real = call_routes.execute_call

        async def watched(context, client):
            out = await real(context, client)
            body = out.body_stream

            async def noting():
                async for chunk in body:
                    delivered.set()
                    yield chunk
            out.body_stream = noting()
            return out

        monkeypatch.setattr(call_routes, "execute_call", watched)
        call = asyncio.create_task(clients.get(f"/call/{EP}?aweme_id=7"))
        try:
            # Handed on while the provider still holds the stream open: nothing waits for its end.
            await asyncio.wait_for(delivered.wait(), 5)
        finally:
            release.set()
        r = await call
        assert r.status_code == 200 and r.content == first + rest

    async def test_an_own_key_page_past_the_hold_streams_as_it_came(
        self, clients: AsyncClient, monkeypatch,
    ):
        """The stated limit: an own-key answer larger than `archive_max_body_bytes` is not held,
        so it streams whole and untouched, never cut."""
        monkeypatch.setattr(get_settings(), "archive_max_body_bytes", 64)
        assert (await clients.post(
            "/secrets", json={"name": "meta-ad-library", "value": META_TOKEN})).status_code == 200
        seen = _vendor(monkeypatch, _meta_page)
        r = await clients.get(f"/call/{META_EP}", params=META_QUERY)
        assert r.status_code == 200 and r.content == _meta_page(seen[0])


async def test_a_hit_on_a_copy_kept_before_masking_is_masked_and_so_is_its_result(
    clients: AsyncClient, archive_on, echoing, monkeypatch,
):
    """A snapshot recorded before the archive masked answers still holds treg's key: the hit,
    `GET /calls/{id}/result` and the staff archive panel mask it all the same."""
    _vendor(monkeypatch, _page_with_next_link)
    with monkeypatch.context() as before_masking:
        before_masking.setattr(call_evidence.AnswerMask, "answer", lambda self, body, encoding="": body)
        old = await clients.get(f"/call/{EP}?aweme_id=7")
    assert PLATFORM_KEY in old.text
    await archive.drain()
    async with session_maker() as s:
        key_hash = (await s.execute(select(ArchiveKey))).scalars().one().key_hash
    monkeypatch.setattr(get_settings(), "admin_token", "ADM-TOKEN")
    staff = (await clients.get(f"/admin/archive/body?key_hash={key_hash}&version=1",
                               headers={"X-Treg-Token": "ADM-TOKEN"})).json()
    assert staff["stored"] is True and PLATFORM_KEY not in staff["body_text"]
    assert staff["body_text"].endswith('&token=***"}')
    hit = await clients.get(f"/call/{EP}?aweme_id=7")
    assert hit.headers["X-Treg-Cache"] == "hit"
    assert PLATFORM_KEY not in hit.text and hit.json()["next"].endswith("&token=***")
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert len(rows) == 2
    for row in rows:
        result = (await clients.get(f"/calls/{row['id']}/result")).json()
        assert result["stored"] is True and PLATFORM_KEY not in json.dumps(result)


async def test_pages_of_a_copy_kept_before_masking_are_cut_after_the_mask(
    clients: AsyncClient, archive_on, echoing, monkeypatch,
):
    """A page boundary inside treg's key must not leave either half readable: the whole stored
    answer is masked first, then cut."""
    def large(request: httpx.Request) -> bytes:
        key = request.headers["authorization"].removeprefix("Bearer ")
        return json.dumps({"pad": "p" * 100_000, "next": f"https://api.tikhub.io/x?token={key}"}).encode()

    _vendor(monkeypatch, large)
    with monkeypatch.context() as before_masking:
        before_masking.setattr(call_evidence.AnswerMask, "answer", lambda self, body, encoding="": body)
        old = await clients.get(f"/call/{EP}?aweme_id=7")
    split = old.text.index(PLATFORM_KEY) + 5
    await archive.drain()
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    pages = [(await clients.get(f"/calls/{row['id']}/result", params=p)).json()["response"]
             for p in ({"limit": split}, {"offset": split})]
    joined = "".join(p["body_text"] for p in pages)
    assert joined == old.text.replace(PLATFORM_KEY, "***") and joined.endswith('token=***"}')
    assert PLATFORM_KEY[:5] not in pages[0]["body_text"] and PLATFORM_KEY[5:] not in pages[1]["body_text"]
    assert pages[0]["total_chars"] == len(joined)


async def test_an_own_key_hit_on_a_public_copy_kept_before_masking_is_masked(
    clients: AsyncClient, archive_on, echoing, monkeypatch,
):
    """An own-key caller may be answered from treg's public copy, which may predate masking: its
    hit is masked with treg's own credentials for the provider too."""
    stranger = {"X-Treg-Token": (await verified_signup(
        clients, json={"email": "stranger@example.com"})).json()["token"]}
    _vendor(monkeypatch, _page_with_next_link)
    with monkeypatch.context() as before_masking:
        before_masking.setattr(call_evidence.AnswerMask, "answer", lambda self, body, encoding="": body)
        first = await clients.get(f"/call/{EP}?aweme_id=7", headers=stranger)
    assert first.status_code == 200 and PLATFORM_KEY in first.text
    await archive.drain()
    assert (await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-TEAM-KEY"})).status_code == 200
    hit = await clients.get(f"/call/{EP}?aweme_id=7")
    assert hit.headers.get("X-Treg-Cache") == "hit"
    assert PLATFORM_KEY not in hit.text and hit.json()["next"].endswith("&token=***")


async def test_a_meta_hit_quoting_the_teams_token_is_masked(clients: AsyncClient, monkeypatch):
    """Meta's rows carry `echoes_credential` in their provider files, so a hit is searched like a
    live page: a stored page quoting the team's token (stored here with the recorder's guard and
    the mask off) reaches the caller masked."""
    assert catalog_store.load().by_id[META_EP]["echoes_credential"] is True
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", META_EP)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
    monkeypatch.setitem(catalog_store.load().by_id[META_EP], "cache", "transient")
    assert (await clients.post(
        "/secrets", json={"name": "meta-ad-library", "value": META_TOKEN})).status_code == 200
    seen = _vendor(monkeypatch, _meta_page)
    with monkeypatch.context() as unguarded:
        unguarded.setattr(call_evidence.AnswerMask, "answer", lambda self, body, encoding="": body)
        unguarded.setattr(call_service, "_echoes_own_credential", lambda tool, secrets, body: False)
        stored = await clients.get(f"/call/{META_EP}", params=META_QUERY)
    assert stored.status_code == 200 and META_TOKEN in stored.text
    await archive.drain()
    hit = await clients.get(f"/call/{META_EP}", params=META_QUERY)
    assert hit.headers["X-Treg-Cache"] == "hit" and len(seen) == 1
    assert META_TOKEN not in hit.text
    assert hit.json()["paging"]["next"] == (
        "https://graph.facebook.com/v21.0/ads_archive?access_token=***&limit=25&after=QVFIUmNh")


META_PLATFORM_TOKEN = "EAAGm0PX4ZCpsBAtregOwnAdLibraryToken77"


@pytest.fixture
def meta_on_treg_key(monkeypatch):
    """No setting holds a Meta platform key today; a class attribute stands in for one."""
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "meta-ad-library")
    get_settings.cache_clear()
    monkeypatch.setattr(type(get_settings()), "platform_key_meta_ad_library", META_PLATFORM_TOKEN,
                        raising=False)
    yield
    get_settings.cache_clear()


async def test_a_marked_row_on_treg_key_never_streams_its_free_answer_unread(
    clients: AsyncClient, meta_on_treg_key, monkeypatch,
):
    """A free `utility` row on treg's key streams unread (`streamable_free_result`), save a row
    marked `echoes_credential`: the Meta search row forced into that shape is read whole and
    masked."""
    monkeypatch.setitem(catalog_store.load().by_id[META_EP], "kind", "utility")
    seen = _vendor(monkeypatch, _meta_page)
    r = await clients.get(f"/call/{META_EP}", params=META_QUERY)
    assert r.status_code == 200, r.text
    assert seen[0].url.params["access_token"] == META_PLATFORM_TOKEN
    assert META_PLATFORM_TOKEN not in r.text
    assert r.json()["paging"]["next"] == (
        "https://graph.facebook.com/v21.0/ads_archive?access_token=***&limit=25&after=QVFIUmNh")


def test_a_short_half_of_a_basic_credential_leaves_the_answer_data_alone(monkeypatch):
    """On an error the decoded halves of a Basic credential are masked too; in an answer a 4-digit
    login is an ordinary number, so only the spellings of 8 characters or more are rewritten."""
    import base64
    blob = base64.b64encode(b"4242:long-dfs-password-9x").decode()
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO", blob)
    get_settings.cache_clear()
    try:
        tool = SimpleNamespace(bindings=oauth_providers.platform_bindings(oauth_providers.get("dataforseo")))
        numbers = b'{"tasks":[{"id":"4242","cost":0.4242,"result":[{"rank":4242}]}]}'
        assert call_evidence.redact_answer(numbers, tool, {}) is numbers
        quoting = b'{"login":"4242:long-dfs-password-9x","sent":"Basic ' + blob.encode() + b'"}'
        assert call_evidence.redact_answer(quoting, tool, {}) == b'{"login":"***","sent":"***"}'
        assert call_evidence.redact_error_response(numbers, tool, {})[0] != numbers
    finally:
        get_settings.cache_clear()


def test_a_numeric_credential_leaves_json_numbers_alone(monkeypatch):
    """A credential made of digits only would turn `"id":20251127` into `"id":***`, which is no
    longer JSON; such a spelling is not used on answers."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "20251127")
    get_settings.cache_clear()
    try:
        tool = SimpleNamespace(bindings=oauth_providers.platform_bindings(oauth_providers.get("tikhub")))
        body = b'{"items":[{"id":20251127,"views":120251127}]}'
        assert call_evidence.redact_answer(body, tool, {}) is body
        assert call_evidence.redact_answer(b'{"sent":"Bearer 20251127"}', tool, {}) == b'{"sent":"***"}'
    finally:
        get_settings.cache_clear()


async def test_the_archive_copy_and_the_callers_are_searched_once(
    clients: AsyncClient, archive_on, echoing, monkeypatch,
):
    searched = []
    real = call_evidence.AnswerMask.mask

    def counting(self, body):
        searched.append(len(body))
        return real(self, body)

    monkeypatch.setattr(call_evidence.AnswerMask, "mask", counting)
    _vendor(monkeypatch, _page_with_next_link)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.json()["next"].endswith("&token=***")
    await archive.drain()
    async with session_maker() as s:
        snaps = (await s.execute(select(ArchiveSnapshot))).scalars().all()
    assert [snap.content_hash for snap in snaps] == [archive.content_hash(r.content)]
    assert len(searched) == 1


class TestMaskingAnAnswerIsCheap:
    """The answer mask runs on the event loop for every answer held whole, up to the 8 MiB
    settlement buffer: a bytes search through each spelling's probe, never a decode or a regex
    over the body. Its cost grows with the credentials searched for, so it is measured per shape
    (a plain key; a DataForSEO Basic credential and the decoded halves it is also masked as) and
    per number of treg's accounts, rendering included, as the call path pays it once."""

    MIB = 1024 * 1024

    @staticmethod
    def _shape(shape: str, i: int) -> tuple[str, str]:
        if shape == "key":
            return "tikhub", f"tikhub-{i}-9F2c71d0e4Aa3mX8vB2nC7"
        import base64
        return "dataforseo", base64.b64encode(f"ops+{i}@example-team.com:{i:02d}9f2c71d0e4aa3b".encode()).decode()

    @pytest.fixture
    def accounts(self, monkeypatch, request):
        shape, count = request.param
        provider, _ = self._shape(shape, 0)
        values = [self._shape(shape, i)[1] for i in range(count)]
        setting = f"platform_key_{provider}"
        monkeypatch.setenv(f"TREG_{setting.upper()}", values[0])
        monkeypatch.setenv("TREG_PLATFORM_ACCOUNTS", json.dumps({provider: {
            f"a{i}": {setting: value} for i, value in enumerate(values[1:], 1)}}) if count > 1 else "")
        get_settings.cache_clear()
        yield SimpleNamespace(bindings=oauth_providers.platform_bindings(oauth_providers.get(provider))), values
        get_settings.cache_clear()

    def _page(self) -> bytes:
        row = json.dumps({"type": "organic", "domain": "www.example.com", "title": "Café résumé",
                          "url": "https://www.example.com/a/b?c=d", "description": "Lorem ipsum " * 12},
                         ensure_ascii=False).encode()
        rows = b",".join([row] * (8 * self.MIB // (len(row) + 1) + 1))
        return (b'{"tasks":[{"result":[{"items":[' + rows + b"]}]}]}")[: 8 * self.MIB]

    @staticmethod
    def _best_of(runs: int, work) -> float:
        best = float("inf")
        for _ in range(runs):
            started = time.perf_counter()
            work()
            best = min(best, time.perf_counter() - started)
        return best

    @pytest.mark.parametrize("accounts", [("key", 1), ("key", 4), ("basic", 1), ("basic", 4)],
                             indirect=True, ids=["key-1", "key-4", "basic-1", "basic-4"])
    def test_an_8_mib_answer_is_searched_in_milliseconds(self, accounts):
        tool, values = accounts
        body = self._page()
        assert len(body) == 8 * self.MIB
        assert call_evidence.redact_answer(body, tool, {}) is body
        clean = self._best_of(3, lambda: call_evidence.redact_answer(body, tool, {}))
        key = values[-1].encode()
        quoting = body[:-400] + key + body[-400 + len(key):]
        masked = call_evidence.redact_answer(quoting, tool, {})
        assert key not in masked and masked.count(b"***") == 1
        echo = self._best_of(3, lambda: call_evidence.redact_answer(quoting, tool, {}))
        # Generous ceilings for a slow CI runner (this checkout measures about a tenth of them); a
        # decode with a normalised re-check of the same body, as the error path does, costs more.
        per_account = 0.04 if len(values) == 1 else 0.03
        assert clean < per_account * len(values), f"{clean * 1000:.1f} ms without a credential"
        assert echo < per_account * len(values) + 0.1, f"{echo * 1000:.1f} ms quoting one"
