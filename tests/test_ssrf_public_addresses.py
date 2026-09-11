"""Public-address checks at registration and on the actual HTTP call path."""
import socket

import pytest

from treg.config import get_settings
from treg.infra.upstream.ssrf import safe_webhook_url


PUBLIC_ADDRESSES = [
    ("100.64.0.1", False),
    ("64:ff9b::7f00:1", False),
    ("224.0.0.1", False),
    ("ff02::1", False),
    ("8.8.8.8", True),
]


@pytest.mark.parametrize("address,public", PUBLIC_ADDRESSES)
def test_literal_target(address, public):
    host = f"[{address}]" if ":" in address else address
    assert safe_webhook_url(f"https://{host}/") is public


@pytest.mark.parametrize("address,public", PUBLIC_ADDRESSES)
async def test_call_rechecks_dns(clients, monkeypatch, fake_getaddrinfo, address, public):
    registered = await clients.post("/tools", json={
        "name": "rebound", "base_url": "https://rebound.example",
    })
    assert registered.status_code == 200
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
    fake_getaddrinfo({"rebound.example": [address]})
    response = await clients.get("/call/rebound/echo")
    assert response.status_code == (200 if public else 502), response.text
    if not public:
        assert "non-public address" in response.text


def test_fake_dns_preserves_unlisted_hosts(fake_getaddrinfo):
    # A new Postgres connection may resolve while a call-path test has the fixture installed.
    expected = socket.getaddrinfo("127.0.0.1", 5432, type=socket.SOCK_STREAM)
    fake_getaddrinfo({"rebound.example": ["100.64.0.1"]})
    assert socket.getaddrinfo("127.0.0.1", 5432, type=socket.SOCK_STREAM) == expected
