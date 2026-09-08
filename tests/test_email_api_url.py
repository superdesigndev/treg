from types import SimpleNamespace

import pytest

from treg import email as email_module
from treg.config import Settings


class _Response:
    status_code = 200
    text = "ok"


class _AsyncClient:
    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        _AsyncClient.last_url = url
        return _Response()


def test_transactional_email_api_url_defaults_to_resend(monkeypatch):
    monkeypatch.delenv("TREG_EMAIL_API_URL", raising=False)
    assert Settings(_env_file=None).email_api_url == "https://api.resend.com/emails"


def test_transactional_email_api_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("TREG_EMAIL_API_URL", "https://send.example.com/api/v1/emails")
    assert Settings(_env_file=None).email_api_url == "https://send.example.com/api/v1/emails"


@pytest.mark.asyncio
async def test_send_uses_configured_transactional_email_api_url(monkeypatch):
    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: SimpleNamespace(
            resend_api_key="test-key",
            email_from="treg@example.com",
            email_api_url="https://send.example.com/api/v1/emails",
        ),
    )
    monkeypatch.setattr(email_module.httpx, "AsyncClient", _AsyncClient)

    assert await email_module._send("person@example.com", "subject", "<p>hello</p>", "hello")
    assert _AsyncClient.last_url == "https://send.example.com/api/v1/emails"
