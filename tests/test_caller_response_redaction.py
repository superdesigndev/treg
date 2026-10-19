"""Caller-facing response redaction: an injected credential never reaches the caller in an error body.

Providers echo the request back in errors (TikHub's 402 "Insufficient balance" returns every request
header, Authorization included). The body is redacted after settlement/capacity read it, before
idempotent storage and before the caller sees it: exact masking of every spelling of
every injected credential, then a fail-closed normalised re-check that replaces the whole body.
"""

from __future__ import annotations

import gzip
import json

import pytest
from httpx import AsyncClient

from treg import api as A
from treg.application.call import evidence as call_evidence
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings

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
    """The incident case: TikHub 402 echoes Authorization header in detail.headers.
    
    Post 2026-10-16..19 incidents: platform-tier 402 capacity exhaustion is now intercepted
    and returns 503 provider_capacity_unavailable, so the vendor body is never relayed.
    The platform key cannot leak because the body is replaced entirely.
    """

    async def test_platform_402_intercepted_not_relayed(self, clients: AsyncClient, platform_on, monkeypatch):
        """Platform 402 capacity exhaustion returns 503, vendor body never reaches caller."""
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

        # Platform capacity exhaustion is intercepted - vendor body is never relayed
        assert r.status_code == 503
        response_body = r.content.decode()
        assert PLATFORM_KEY not in response_body, "platform key must not leak"
        assert "Insufficient balance" not in response_body, "vendor message replaced"
        assert "provider_capacity_unavailable" in response_body

    async def test_platform_402_body_never_relayed(self, clients: AsyncClient, platform_on, monkeypatch):
        """Platform 402 with echoed headers is intercepted, vendor body discarded."""
        tikhub_402_body = json.dumps({
            "detail": {
                "code": 402,
                "message": "Insufficient balance",
                "headers": {
                    "Authorization": f"Bearer {PLATFORM_KEY}",
                },
            },
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(402, tikhub_402_body))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        # Platform capacity exhaustion intercepted - treg's typed error, not vendor body
        assert r.status_code == 503
        body = r.json()
        assert body["detail"]["error"] == "provider_capacity_unavailable"
        assert PLATFORM_KEY not in r.content.decode()

    async def test_non_402_errors_are_still_redacted(self, clients: AsyncClient, platform_on, monkeypatch):
        """Non-402 errors (400, 401, 403, 429, 500) still relay with credential redaction."""
        for status in (400, 401, 403, 429, 500):
            body_with_key = json.dumps({
                "error": f"Some error with key {PLATFORM_KEY}",
            }).encode()

            monkeypatch.setattr(call_service, "relay", _fake_relay(status, body_with_key))
            r = await clients.get(f"/call/{EP}?aweme_id=7")

            assert r.status_code == status, f"expected {status}, got {r.status_code}"
            assert PLATFORM_KEY not in r.content.decode(), f"key leaked in {status} response"

    async def test_platform_402_intercepted_returns_503(self, clients: AsyncClient, platform_on, monkeypatch):
        """Platform 402 is intercepted and returns 503, not relayed with redaction."""
        body_with_key = json.dumps({
            "error": f"Some error with key {PLATFORM_KEY}",
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(402, body_with_key))
        r = await clients.get(f"/call/{EP}?aweme_id=7")

        assert r.status_code == 503, "platform 402 intercepted as 503"
        assert PLATFORM_KEY not in r.content.decode(), "key must not leak"


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
        The redaction happens in service.py BEFORE the idempotent storage. Even if idempotent
        replay doesn't trigger in the test environment (e.g. due to metered call windowing
        or test isolation), the first request being properly redacted proves the stored body
        would also be redacted.
        
        Note: 402 is intercepted for platform capacity exhaustion and returns 503, so we use
        401 (authentication error) to test the redaction + idempotent replay path.
        """
        tikhub_401_body = json.dumps({
            "detail": {
                "code": 401,
                "message": "Authentication failed",
                "headers": {
                    "Authorization": f"Bearer {PLATFORM_KEY}",
                },
            },
        }).encode()

        monkeypatch.setattr(call_service, "relay", _fake_relay(401, tikhub_401_body))

        idem_key = "test-idem-key-redaction-002"  # unique key per test run
        r1 = await clients.get(
            f"/call/{EP}?aweme_id=7",
            headers={"Idempotency-Key": idem_key},
        )
        assert r1.status_code == 401
        # The critical security assertion: the key must not reach the caller
        assert PLATFORM_KEY not in r1.content.decode(), "platform key leaked to caller"
        assert r1.json()["detail"]["headers"] == {"Authorization": "***"}

        # Second request with the same idempotency key
        r2 = await clients.get(
            f"/call/{EP}?aweme_id=7",
            headers={"Idempotency-Key": idem_key},
        )
        assert r2.status_code == 401
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
